import type {
  ExtensionAPI,
  ProviderModelConfig,
} from "@earendil-works/pi-coding-agent";
import {
  createFauxCore,
  fauxAssistantMessage,
  fauxToolCall,
} from "@earendil-works/pi-ai";

const CODE_MODE_MODEL: ProviderModelConfig = {
  id: "tether-code-mode-faux",
  name: "Tether Code Mode Faux",
  reasoning: false,
  input: ["text"],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 128_000,
  maxTokens: 4_096,
};

export default function registerFauxCodeModeProvider(pi: ExtensionAPI): void {
  const core = createFauxCore({
    api: "anthropic-messages",
    provider: "faux",
    models: [CODE_MODE_MODEL],
    tokenSize: { min: 1, max: 1 },
  });

  core.setResponses([
    fauxAssistantMessage(
      fauxToolCall(
        "execute_tools",
        {
          code: `
            const added = await Promise.all([
              tools.add_movie({ title: "Dune", year: 2021, intent_context: "friend recommendation" }),
              tools.add_movie({ title: "dune", year: 2021, intent_context: "trailer" }),
            ]);
            const activity = await Promise.all([
              tools.summarize_youtube_activity({
                after: "2030-01-01T00:00:00Z",
                before: "2030-01-08T00:00:00Z",
              }),
              tools.summarize_youtube_activity({
                after: "2030-01-08T00:00:00Z",
                before: "2030-01-15T00:00:00Z",
              }),
            ]);
            const report = await tools.triage_report({});
            return {
              addedIds: added.map((result) => result.item.id),
              activityCounts: activity.map((period) => period.video_count),
              duplicateCount: report.duplicates.length,
            };
          `,
        },
        { id: "call-execute-tools" },
      ),
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage("script complete"),
  ]);

  pi.registerProvider("faux", {
    api: "anthropic-messages",
    apiKey: "x",
    baseUrl: "http://localhost:0",
    models: [CODE_MODE_MODEL],
    streamSimple: core.streamSimple,
  });
}
