import type {
  ExtensionAPI,
  ProviderModelConfig,
} from "@earendil-works/pi-coding-agent";
import {
  createFauxCore,
  fauxAssistantMessage,
  fauxToolCall,
} from "@earendil-works/pi-ai";
import type { Context, ToolResultMessage } from "@earendil-works/pi-ai";

const SCRIPTED_MODEL: ProviderModelConfig = {
  id: "tether-agent-loop-faux",
  name: "Tether Agent Loop Faux",
  reasoning: false,
  input: ["text"],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 128_000,
  maxTokens: 4_096,
};

interface TetherToolDetails {
  provenance?: unknown;
  quota?: unknown;
  result?: unknown;
}

interface MemoryResult {
  id: string;
  version: number;
}

function isMemoryResult(value: unknown): value is MemoryResult {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.id === "string" && typeof candidate.version === "number"
  );
}

function toolResults(context: Context): ToolResultMessage<TetherToolDetails>[] {
  return context.messages.filter(
    (message): message is ToolResultMessage<TetherToolDetails> =>
      message.role === "toolResult",
  );
}

function latestCapturedMemory(context: Context): MemoryResult {
  for (const message of toolResults(context).toReversed()) {
    if (message.toolName !== "capture" || message.isError) {
      continue;
    }
    const memory = message.details?.result;
    if (isMemoryResult(memory)) {
      return memory;
    }
  }
  throw new Error("capture result was not available to the faux model");
}

export default function registerFauxAgentLoopProvider(pi: ExtensionAPI): void {
  const core = createFauxCore({
    api: "anthropic-messages",
    provider: "faux",
    models: [SCRIPTED_MODEL],
    tokenSize: { min: 1, max: 1 },
  });

  core.setResponses([
    fauxAssistantMessage(
      fauxToolCall(
        "capture",
        { content: "agent loop needle memory" },
        { id: "call-capture" },
      ),
      { stopReason: "toolUse" },
    ),
    (context) => {
      const memory = latestCapturedMemory(context);
      return fauxAssistantMessage(
        fauxToolCall(
          "tether",
          { memory_id: memory.id, version: memory.version },
          { id: "call-tether" },
        ),
        { stopReason: "toolUse" },
      );
    },
    fauxAssistantMessage(
      fauxToolCall("search", { q: "needle", limit: 5 }, { id: "call-search" }),
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage("script complete"),
  ]);

  pi.registerProvider("faux", {
    api: "anthropic-messages",
    apiKey: "x",
    baseUrl: "http://localhost:0",
    models: [SCRIPTED_MODEL],
    streamSimple: core.streamSimple,
  });
}
