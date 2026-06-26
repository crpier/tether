import {
  createServer,
  type IncomingMessage,
  type ServerResponse,
} from "node:http";
import { afterEach, describe, expect, test } from "vitest";

import { executeTetherTool, type TetherToolConfig } from "../src/runtime.js";

interface RecordedRequest {
  body: unknown;
  headers: IncomingMessage["headers"];
  method: string | undefined;
  url: string | undefined;
}

const servers: ReturnType<typeof createServer>[] = [];

async function readRequestBody(request: IncomingMessage): Promise<unknown> {
  let body = "";
  request.setEncoding("utf8");
  for await (const chunk of request) {
    body += String(chunk);
  }
  return JSON.parse(body) as unknown;
}

async function withToolServer(
  handler: (
    request: IncomingMessage,
    response: ServerResponse,
  ) => Promise<void> | void,
): Promise<{ baseUrl: string }> {
  const server = createServer((request, response) => {
    void Promise.resolve(handler(request, response)).catch((error: unknown) => {
      response.destroy(
        error instanceof Error ? error : new Error(String(error)),
      );
    });
  });
  servers.push(server);
  await new Promise<void>((resolve) => {
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  if (address === null || typeof address === "string") {
    throw new Error("test server did not bind to a TCP port");
  }
  return { baseUrl: `http://127.0.0.1:${String(address.port)}` };
}

function config(baseUrl: string): TetherToolConfig {
  return {
    baseUrl,
    secret: "secret-123",
    sessionId: "session-abc",
  };
}

afterEach(async () => {
  await Promise.all(
    servers.splice(0).map(
      (server) =>
        new Promise<void>((resolve, reject) => {
          server.close((error) => {
            if (error) reject(error);
            else resolve();
          });
        }),
    ),
  );
});

describe("executeTetherTool", () => {
  test("posts params with session identity and secret header", async () => {
    let recorded: RecordedRequest | undefined;
    const { baseUrl } = await withToolServer(async (request, response) => {
      recorded = {
        body: await readRequestBody(request),
        headers: request.headers,
        method: request.method,
        url: request.url,
      };
      response.writeHead(200, { "content-type": "application/json" });
      response.end(
        JSON.stringify({
          error: null,
          provenance: { kind: "manual" },
          quota: null,
          result: { id: "memory-1" },
          success: true,
        }),
      );
    });

    const result = await executeTetherTool(
      { endpoint: "/internal/tools/capture", name: "capture" },
      { content: "remember this" },
      undefined,
      config(baseUrl),
    );

    expect(recorded).toEqual({
      body: { content: "remember this", session_id: "session-abc" },
      headers: expect.objectContaining({
        "content-type": "application/json",
        "x-tether-tool-secret": "secret-123",
      }) as IncomingMessage["headers"],
      method: "POST",
      url: "/internal/tools/capture",
    });
    expect(result).toEqual({
      content: [{ type: "text", text: "capture succeeded" }],
      details: {
        provenance: { kind: "manual" },
        quota: null,
        result: { id: "memory-1" },
      },
    });
  });

  test("throws when a tool envelope reports failure", async () => {
    const { baseUrl } = await withToolServer((_request, response) => {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(
        JSON.stringify({
          error: { code: "conflict", message: "version changed" },
          provenance: null,
          quota: null,
          result: null,
          success: false,
        }),
      );
    });

    await expect(
      executeTetherTool(
        { endpoint: "/internal/tools/tether", name: "tether" },
        { memory_id: "018f0000-0000-7000-8000-000000000000", version: 1 },
        undefined,
        config(baseUrl),
      ),
    ).rejects.toThrow("conflict: version changed");
  });

  test("throws when the authorization gate rejects the request", async () => {
    const { baseUrl } = await withToolServer((_request, response) => {
      response.writeHead(401, { "content-type": "application/json" });
      response.end(JSON.stringify({ detail: "invalid tool secret" }));
    });

    await expect(
      executeTetherTool(
        { endpoint: "/internal/tools/capture", name: "capture" },
        { content: "remember this" },
        undefined,
        config(baseUrl),
      ),
    ).rejects.toThrow(
      'tool endpoint returned HTTP 401: {"detail":"invalid tool secret"}',
    );
  });
});
