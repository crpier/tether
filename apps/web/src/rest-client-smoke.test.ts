import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:net";

import { describe, expect, test } from "vitest";

import { createTetherClient } from "./generated";

const APP_PASSWORD = "correct horse battery staple";
const SESSION_SECRET = "stable-test-session-secret";

async function reservePort(): Promise<number> {
  const server = createServer();
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  if (address === null || typeof address === "string") {
    server.close();
    throw new Error("could not reserve a TCP port");
  }
  const { port } = address;
  await new Promise<void>((resolve, reject) => {
    server.close((error) => {
      if (error) {
        reject(error);
        return;
      }
      resolve();
    });
  });
  return port;
}

async function startHost(
  port: number,
  root: string,
): Promise<ChildProcessWithoutNullStreams> {
  const hostProcess = spawn(
    "uv",
    [
      "run",
      "python",
      "-m",
      "uvicorn",
      "tether.server:create_app_from_environment",
      "--factory",
      "--host",
      "127.0.0.1",
      "--port",
      String(port),
    ],
    {
      cwd: join(process.cwd(), "..", "host"),
      env: {
        ...process.env,
        TETHER_APP_PASSWORD: APP_PASSWORD,
        TETHER_DATABASE_PATH: join(root, "tether.sqlite3"),
        TETHER_KB_ROOT: join(root, "kb"),
        TETHER_LOGGING_LEVEL: "ERROR",
        TETHER_SESSION_SECRET: SESSION_SECRET,
        TETHER_STT_API_KEY: "test-stt-key",
      },
    },
  );
  let stderr = "";
  hostProcess.stderr.setEncoding("utf8");
  hostProcess.stderr.on("data", (chunk: string) => {
    stderr += chunk;
  });
  await waitForHost(
    `http://127.0.0.1:${String(port)}`,
    hostProcess,
    () => stderr,
  );
  return hostProcess;
}

async function waitForHost(
  baseUrl: string,
  hostProcess: ChildProcessWithoutNullStreams,
  stderr: () => string,
): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (hostProcess.exitCode !== null) {
      throw new Error(`host exited before readiness: ${stderr()}`);
    }
    try {
      const response = await fetch(`${baseUrl}/openapi.json`);
      if (response.ok) {
        return;
      }
    } catch {
      // Host is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`host did not become ready: ${stderr()}`);
}

async function stopHost(
  hostProcess: ChildProcessWithoutNullStreams,
): Promise<void> {
  if (hostProcess.exitCode !== null) {
    return;
  }
  hostProcess.kill();
  await once(hostProcess, "exit");
}

describe("generated REST client", () => {
  test("authenticates against the live host", async () => {
    const root = await mkdtemp(join(tmpdir(), "tether-web-smoke-"));
    const port = await reservePort();
    const hostProcess = await startHost(port, root);
    try {
      const client = createTetherClient({
        baseUrl: `http://127.0.0.1:${String(port)}`,
      });

      const login = await client.POST("/api/auth/login", {
        body: { password: APP_PASSWORD },
      });
      const cookie = login.response.headers.get("set-cookie")?.split(";")[0];
      const session = await client.GET("/api/auth/session", {
        headers: { cookie },
      });

      expect(login.response.status).toBe(204);
      expect(cookie).toContain("tether_session=");
      expect(session.data).toEqual({ authenticated: true });
    } finally {
      await stopHost(hostProcess);
      await rm(root, { force: true, recursive: true });
    }
  });
});
