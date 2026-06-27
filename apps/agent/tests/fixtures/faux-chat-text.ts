import type {
  ExtensionAPI,
  ProviderModelConfig,
} from "@earendil-works/pi-coding-agent";
import { createFauxCore, fauxAssistantMessage } from "@earendil-works/pi-ai";

const CHAT_MODEL: ProviderModelConfig = {
  id: "tether-chat-text-faux",
  name: "Tether Chat Text Faux",
  reasoning: false,
  input: ["text"],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 128_000,
  maxTokens: 4_096,
};

export default function registerFauxChatTextProvider(pi: ExtensionAPI): void {
  const core = createFauxCore({
    api: "anthropic-messages",
    provider: "faux",
    models: [CHAT_MODEL],
    tokenSize: { min: 1, max: 1 },
  });

  core.setResponses([fauxAssistantMessage("script complete")]);

  pi.registerProvider("faux", {
    api: "anthropic-messages",
    apiKey: "x",
    baseUrl: "http://localhost:0",
    models: [CHAT_MODEL],
    streamSimple: core.streamSimple,
  });
}
