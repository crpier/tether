import type {
  ExtensionAPI,
  ProviderModelConfig,
} from "@earendil-works/pi-coding-agent";
import {
  createFauxCore,
  fauxAssistantMessage,
  type FauxResponseFactory,
} from "@earendil-works/pi-ai";

const CHEAP_MODEL: ProviderModelConfig = {
  id: "tether-chat-cheap-faux",
  name: "Tether Chat Cheap Faux",
  reasoning: false,
  input: ["text"],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 128_000,
  maxTokens: 4_096,
};

const SMART_MODEL: ProviderModelConfig = {
  id: "tether-chat-smart-faux",
  name: "Tether Chat Smart Faux",
  reasoning: false,
  input: ["text"],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 128_000,
  maxTokens: 4_096,
};

const echoModel: FauxResponseFactory = (_context, _options, _state, model) =>
  fauxAssistantMessage(model.id);

export default function registerModelEchoFauxProvider(pi: ExtensionAPI): void {
  const core = createFauxCore({
    api: "anthropic-messages",
    provider: "faux",
    models: [CHEAP_MODEL, SMART_MODEL],
    tokenSize: { min: 1, max: 1 },
  });

  core.setResponses([echoModel, echoModel, echoModel]);

  pi.registerProvider("faux", {
    api: "anthropic-messages",
    apiKey: "x",
    baseUrl: "http://localhost:0",
    models: [CHEAP_MODEL, SMART_MODEL],
    streamSimple: core.streamSimple,
  });
}
