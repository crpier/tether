import type {
  ExtensionAPI,
  ProviderModelConfig,
} from "@earendil-works/pi-coding-agent";
import {
  createFauxCore,
  fauxAssistantMessage,
  fauxToolCall,
} from "@earendil-works/pi-ai";

const SCRIPTED_MODEL: ProviderModelConfig = {
  id: "tether-review-digest-faux",
  name: "Tether Review Digest Faux",
  reasoning: false,
  input: ["text"],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 128_000,
  maxTokens: 4_096,
};

export default function registerFauxReviewDigestProvider(
  pi: ExtensionAPI,
): void {
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
        { content: "I prefer aisle seats on flights" },
        { id: "call-capture-first" },
      ),
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage(
      fauxToolCall(
        "capture",
        { content: "I prefer aisle seats on flights please" },
        { id: "call-capture-second" },
      ),
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage(
      fauxToolCall("review_digest", {}, { id: "call-review-digest" }),
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
