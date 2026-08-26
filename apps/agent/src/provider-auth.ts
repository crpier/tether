import { pathToFileURL } from "node:url";

import type {
  AuthInteraction,
  AuthResult,
  Credential,
  CredentialInfo,
} from "@earendil-works/pi-ai";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";

export type ProviderAuthCommand = "login" | "status";

export type ProviderAuthEvent =
  | { connected: boolean; type: "status" }
  | {
      expires_in_seconds?: number;
      type: "device_code";
      user_code: string;
      verification_uri: string;
    }
  | { type: "complete" };

export interface ProviderAuthRuntime {
  getAuth(providerId: string): Promise<AuthResult | undefined>;
  listCredentials(): Promise<readonly CredentialInfo[]>;
  login(
    providerId: string,
    type: "oauth",
    interaction: AuthInteraction,
  ): Promise<Credential>;
}

export interface ProviderAuthCliDependencies {
  createRuntime(): Promise<ProviderAuthRuntime>;
  write(line: string): void;
}

export async function runProviderAuthCommand(
  command: ProviderAuthCommand,
  runtime: ProviderAuthRuntime,
  emit: (event: ProviderAuthEvent) => void,
  controller: AbortController,
): Promise<void> {
  if (command === "login") {
    await runtime.login("openai-codex", "oauth", {
      notify(event) {
        if (event.type === "device_code") {
          emit({
            expires_in_seconds: event.expiresInSeconds,
            type: "device_code",
            user_code: event.userCode,
            verification_uri: event.verificationUri,
          });
        }
      },
      prompt(prompt) {
        if (
          prompt.type === "select" &&
          prompt.options.some((option) => option.id === "device_code")
        ) {
          return Promise.resolve("device_code");
        }
        return Promise.reject(
          new Error(`unexpected provider auth prompt: ${prompt.type}`),
        );
      },
      signal: controller.signal,
    });
    emit({ type: "complete" });
    return;
  }

  const credentials = await runtime.listCredentials();
  const stored = credentials.some(
    (credential) => credential.providerId === "openai-codex",
  );
  if (!stored) {
    emit({ connected: false, type: "status" });
    return;
  }
  try {
    const auth = await runtime.getAuth("openai-codex");
    emit({ connected: auth !== undefined, type: "status" });
  } catch {
    emit({ connected: false, type: "status" });
  }
}

export async function runProviderAuthCli(
  args: readonly string[],
  dependencies: ProviderAuthCliDependencies,
): Promise<void> {
  const command = args[0];
  if (command !== "login" && command !== "status") {
    throw new Error("usage: provider-auth <login|status>");
  }
  const controller = new AbortController();
  await runProviderAuthCommand(
    command,
    await dependencies.createRuntime(),
    (event) => dependencies.write(`${JSON.stringify(event)}\n`),
    controller,
  );
}

async function main(): Promise<void> {
  const abort = new AbortController();
  const cancel = () => abort.abort();
  process.once("SIGINT", cancel);
  process.once("SIGTERM", cancel);
  try {
    const command = process.argv[2];
    if (command !== "login" && command !== "status") {
      throw new Error("usage: provider-auth <login|status>");
    }
    await runProviderAuthCommand(
      command,
      await ModelRuntime.create({ allowModelNetwork: false, modelsPath: null }),
      (event) => {
        void process.stdout.write(`${JSON.stringify(event)}\n`);
      },
      abort,
    );
  } finally {
    process.off("SIGINT", cancel);
    process.off("SIGTERM", cancel);
  }
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(() => {
    void process.stderr.write("provider authorization failed\n");
    process.exitCode = 1;
  });
}
