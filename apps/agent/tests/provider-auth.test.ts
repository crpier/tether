import type { AuthInteraction } from "@earendil-works/pi-ai";
import { describe, expect, test, vi } from "vitest";

import {
  runProviderAuthCli,
  runProviderAuthCommand,
} from "../src/provider-auth.js";

describe("provider auth helper protocol", () => {
  test("reports a stored refreshable credential as connected", async () => {
    const emit = vi.fn();
    const runtime = {
      getAuth: vi.fn().mockResolvedValue({ auth: { apiKey: "access-token" } }),
      listCredentials: vi
        .fn()
        .mockResolvedValue([{ providerId: "openai-codex", type: "oauth" }]),
      login: vi.fn(),
    };

    await runProviderAuthCommand(
      "status",
      runtime,
      emit,
      new AbortController(),
    );

    expect(emit).toHaveBeenCalledWith({ connected: true, type: "status" });
  });

  test("writes each protocol event as one JSON line", async () => {
    const output: string[] = [];
    const runtime = {
      getAuth: vi.fn().mockResolvedValue(undefined),
      listCredentials: vi.fn().mockResolvedValue([]),
      login: vi.fn(),
    };

    await runProviderAuthCli(["status"], {
      createRuntime: vi.fn().mockResolvedValue(runtime),
      write: (line) => output.push(line),
    });

    expect(output).toEqual(['{"connected":false,"type":"status"}\n']);
  });

  test("selects device login and emits only browser-safe authorization data", async () => {
    const emit = vi.fn();
    const runtime = {
      getAuth: vi.fn(),
      listCredentials: vi.fn(),
      login: vi.fn(
        async (
          _provider: string,
          _type: "oauth",
          interaction: AuthInteraction,
        ) => {
          expect(
            await interaction.prompt({
              message: "Select OpenAI Codex login method:",
              options: [
                { id: "browser", label: "Browser" },
                { id: "device_code", label: "Device" },
              ],
              type: "select",
            }),
          ).toBe("device_code");
          interaction.notify({
            expiresInSeconds: 900,
            intervalSeconds: 5,
            type: "device_code",
            userCode: "ABCD-EFGH",
            verificationUri: "https://auth.openai.com/codex/device",
          });
          return {
            access: "secret-access",
            expires: Date.now() + 900_000,
            refresh: "secret-refresh",
            type: "oauth" as const,
          };
        },
      ),
    };

    await runProviderAuthCommand("login", runtime, emit, new AbortController());

    expect(emit.mock.calls).toEqual([
      [
        {
          expires_in_seconds: 900,
          type: "device_code",
          user_code: "ABCD-EFGH",
          verification_uri: "https://auth.openai.com/codex/device",
        },
      ],
      [{ type: "complete" }],
    ]);
  });
});
