import type {
  AnswerOutcome,
  DuePrompt,
  EssayGradeProposal,
  RecallAnswerInput,
  RecallHost,
} from "../../host/recall";
import { ApiError } from "../../host/error";

export class FakeRecallHost implements RecallHost {
  storedDuePrompts: DuePrompt[];
  answerCalls: ({ promptId: string } & RecallAnswerInput)[] = [];
  proposeCalls: { answerText: string; promptId: string }[] = [];
  proposeRejections: ApiError[] = [];
  correctIndices: Record<string, number> = {};

  constructor(duePrompts: DuePrompt[] = []) {
    this.storedDuePrompts = duePrompts;
  }

  listDueRecallPrompts(): Promise<DuePrompt[]> {
    return Promise.resolve(this.storedDuePrompts);
  }

  answerRecallPrompt(
    promptId: string,
    input: RecallAnswerInput,
  ): Promise<AnswerOutcome> {
    const answered = this.storedDuePrompts.find(
      (due) => due.prompt.id === promptId,
    );
    this.answerCalls.push({ promptId, ...input });
    this.storedDuePrompts = this.storedDuePrompts.filter(
      (due) => due.prompt.id !== promptId,
    );
    const correct =
      input.confirmed_correct ??
      (input.selected_index !== undefined
        ? input.selected_index === this.correctIndices[promptId]
        : true);
    return Promise.resolve({
      completed: false,
      correct,
      prompt: answered?.prompt ?? this.placeholderPrompt(promptId),
      quality: correct ? 5 : 1,
      tethered: false,
    });
  }

  proposeEssayGrade(
    promptId: string,
    answerText: string,
  ): Promise<EssayGradeProposal> {
    this.proposeCalls.push({ answerText, promptId });
    const forced = this.proposeRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    return Promise.resolve({
      prompt_id: promptId,
      proposed_correct: true,
      reasoning: "Covers the rubric.",
      rubric: "Mentions readiness and cooperative yielding.",
    });
  }

  private placeholderPrompt(promptId: string): DuePrompt["prompt"] {
    return {
      choices: [],
      due_at: "2026-01-01T00:00:00Z",
      id: promptId,
      kind: "multiple_choice",
      question: "",
      study_item_id: promptId,
    };
  }
}
