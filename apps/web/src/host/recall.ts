import type { components } from "../generated";
import { requireData, type RestContext } from "./transport";

export type DuePrompt = components["schemas"]["DuePromptRead"];
export type AnswerOutcome = components["schemas"]["AnswerOutcomeRead"];
export type EssayGradeProposal =
  components["schemas"]["EssayGradeProposalRead"];
type AnswerPromptRequest = components["schemas"]["AnswerPromptRequest"];

export type RecallAnswerInput = Pick<AnswerPromptRequest, "response_ms"> &
  Partial<
    Pick<
      AnswerPromptRequest,
      "answer_text" | "confirmed_correct" | "selected_index"
    >
  >;

export interface RecallHost {
  listDueRecallPrompts(): Promise<DuePrompt[]>;
  answerRecallPrompt(
    promptId: string,
    input: RecallAnswerInput,
  ): Promise<AnswerOutcome>;
  proposeEssayGrade(
    promptId: string,
    answerText: string,
  ): Promise<EssayGradeProposal>;
}

export function createRecallHost(context: RestContext): RecallHost {
  return {
    async listDueRecallPrompts() {
      const { data, response } = await context.client.GET(
        "/api/recall/prompts",
      );
      return requireData(data, response);
    },
    async answerRecallPrompt(promptId, input) {
      const { data, response } = await context.client.POST(
        "/api/recall/prompts/{prompt_id}/answer",
        {
          body: {
            answer_text: input.answer_text ?? null,
            confirmed_correct: input.confirmed_correct ?? null,
            response_ms: input.response_ms,
            selected_index: input.selected_index ?? null,
          },
          params: { path: { prompt_id: promptId } },
        },
      );
      return requireData(data, response);
    },
    async proposeEssayGrade(promptId, answerText) {
      const { data, response } = await context.client.POST(
        "/api/recall/prompts/{prompt_id}/grade-proposal",
        {
          body: { answer_text: answerText },
          params: { path: { prompt_id: promptId } },
        },
      );
      return requireData(data, response);
    },
  };
}
