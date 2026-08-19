import type { AgentMessage } from "@earendil-works/pi-agent-core";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { readTetherToolConfig, type TetherToolConfig } from "./runtime.js";

interface ContextMessage {
  content?: unknown;
  role: string;
}

interface InjectedMemoryMessage {
  content: string;
  role: "user";
  timestamp: number;
}

interface MemoryContextResponse {
  context?: unknown;
}

function endpointUrl(config: TetherToolConfig): URL {
  const baseUrl = config.baseUrl.endsWith("/")
    ? config.baseUrl.slice(0, -1)
    : config.baseUrl;
  return new URL("/internal/memory-context", `${baseUrl}/`);
}

function isTextPart(part: unknown): part is { text: string; type: "text" } {
  if (typeof part !== "object" || part === null) return false;
  const candidate = part as Record<string, unknown>;
  return candidate.type === "text" && typeof candidate.text === "string";
}

function textContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter(isTextPart)
    .map((part) => part.text)
    .join("\n");
}

export function injectMemoryContext<T extends ContextMessage>(
  messages: T[],
  context: string,
): (T | InjectedMemoryMessage)[] {
  const latestUserIndex = messages.findLastIndex(
    (message) => message.role === "user",
  );
  if (latestUserIndex < 0 || context.length === 0) return [...messages];
  return [
    ...messages.slice(0, latestUserIndex),
    { content: context, role: "user", timestamp: 0 },
    ...messages.slice(latestUserIndex),
  ];
}

export async function fetchMemoryContext(
  query: string,
  signal: AbortSignal | undefined,
  config: TetherToolConfig = readTetherToolConfig(),
): Promise<string> {
  const response = await fetch(endpointUrl(config), {
    body: JSON.stringify({ query, session_id: config.sessionId }),
    headers: {
      "content-type": "application/json",
      "x-tether-tool-secret": config.secret,
    },
    method: "POST",
    signal,
  });
  if (!response.ok) return "";
  const body = (await response.json()) as MemoryContextResponse;
  return typeof body.context === "string" ? body.context : "";
}

export default function memoryContextExtension(pi: ExtensionAPI): void {
  pi.on("context", async (event, context) => {
    const latestUser = event.messages.findLast(
      (message) => message.role === "user",
    );
    if (latestUser === undefined) return;
    try {
      const memory = await fetchMemoryContext(
        textContent(latestUser.content),
        context.signal,
      );
      if (memory.length === 0) return;
      return {
        messages: injectMemoryContext(event.messages, memory) as AgentMessage[],
      };
    } catch {
      return;
    }
  });
}
