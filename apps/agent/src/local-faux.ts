import type {
  ExtensionAPI,
  ProviderModelConfig,
} from "@earendil-works/pi-coding-agent";
import {
  createFauxCore,
  fauxAssistantMessage,
  type FauxResponseFactory,
} from "@earendil-works/pi-ai";

const LOCAL_MODEL: ProviderModelConfig = {
  id: "tether-local-faux",
  name: "Tether Local Faux",
  reasoning: false,
  input: ["text"],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 128_000,
  maxTokens: 4_096,
};

export default function registerLocalFauxProvider(pi: ExtensionAPI): void {
  const core = createFauxCore({
    api: "anthropic-messages",
    provider: "faux",
    models: [LOCAL_MODEL],
    tokenSize: { min: 1, max: 1 },
  });
  const respond: FauxResponseFactory = () => {
    core.appendResponses([respond]);
    return fauxAssistantMessage("Local development response.");
  };
  core.setResponses([respond]);

  pi.registerProvider("faux", {
    api: "anthropic-messages",
    apiKey: "local",
    baseUrl: "http://127.0.0.1:0",
    models: [LOCAL_MODEL],
    streamSimple: core.streamSimple,
  });
}
