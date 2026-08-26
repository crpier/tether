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
  id: "tether-triage-faux",
  name: "Tether Triage Faux",
  reasoning: false,
  input: ["text"],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 128_000,
  maxTokens: 4_096,
};

export default function registerFauxTriageProvider(pi: ExtensionAPI): void {
  const core = createFauxCore({
    api: "anthropic-messages",
    provider: "faux",
    models: [SCRIPTED_MODEL],
    tokenSize: { min: 1, max: 1 },
  });

  core.setResponses([
    fauxAssistantMessage(
      fauxToolCall(
        "add_movie",
        { title: "Dune", year: 2021, intent_context: "a friend hyped it" },
        { id: "call-add-first" },
      ),
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage(
      fauxToolCall(
        "add_movie",
        { title: "dune", year: 2021, intent_context: "saw the trailer again" },
        { id: "call-add-second" },
      ),
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage(
      fauxToolCall("triage_report", {}, { id: "call-triage-report" }),
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
