import { createQuery, useQueryClient } from "@tanstack/solid-query";
import { For, Match, Show, Switch, createEffect, createSignal } from "solid-js";

import type {
  AnswerOutcome,
  DuePrompt,
  EssayGradeProposal,
  TetherApi,
} from "../api";
import { panelClass } from "../lib/panel";
import { queryKeys } from "../lib/query-keys";
import { Button } from "@/components/ui/button";

function recallFeedback(outcome: AnswerOutcome): string {
  if (!outcome.correct) {
    return "Not quite — this prompt will come back sooner.";
  }
  if (outcome.completed) {
    return outcome.tethered
      ? "Correct — fully recalled, the memory is now tethered!"
      : "Correct — fully recalled, study item complete!";
  }
  return "Correct — see you next round.";
}

function proposalVerdict(proposal: EssayGradeProposal): string {
  if (proposal.proposed_correct === null) {
    return proposal.rubric
      ? "No model proposal — grade your essay against the rubric."
      : "No model proposal — grade your own essay.";
  }
  const verdict = proposal.proposed_correct ? "correct" : "incorrect";
  const reasoning = proposal.reasoning ? ` — ${proposal.reasoning}` : "";
  return `Model suggests: ${verdict}${reasoning}`;
}

export function RecallPanel(props: { api: TetherApi }) {
  const queryClient = useQueryClient();
  const promptsQuery = createQuery(() => ({
    queryFn: () => props.api.listDueRecallPrompts(),
    queryKey: queryKeys.recall,
  }));
  const [shownAt, setShownAt] = createSignal(Date.now());
  const [feedback, setFeedback] = createSignal<string | undefined>();
  const [error, setError] = createSignal<string | undefined>();
  // Per-prompt free-text drafts and essay grade proposals, keyed by prompt id.
  const [drafts, setDrafts] = createSignal<Record<string, string>>({});
  const [proposals, setProposals] = createSignal<
    Record<string, EssayGradeProposal>
  >({});

  // Restart the response timer whenever the set of due prompts changes, so each
  // prompt is timed from when it became visible (response time feeds scheduling).
  createEffect(() => {
    void promptsQuery.data;
    setShownAt(Date.now());
  });

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.recall });
    void queryClient.refetchQueries({ queryKey: queryKeys.recall });
  };

  const draftFor = (promptId: string) => drafts()[promptId] ?? "";

  const setDraft = (promptId: string, value: string) => {
    setDrafts((current) => ({ ...current, [promptId]: value }));
  };

  const answer = (
    promptId: string,
    input: {
      answer_text?: string;
      confirmed_correct?: boolean;
      selected_index?: number;
    },
  ) => {
    const responseMs = Math.max(0, Date.now() - shownAt());
    void (async () => {
      setError(undefined);
      try {
        const outcome = await props.api.answerRecallPrompt(promptId, {
          ...input,
          response_ms: responseMs,
        });
        setFeedback(recallFeedback(outcome));
        refresh();
      } catch (caught) {
        setError(
          caught instanceof Error ? caught.message : "Could not submit answer",
        );
      }
    })();
  };

  const proposeGrade = (promptId: string) => {
    void (async () => {
      setError(undefined);
      try {
        const proposal = await props.api.proposeEssayGrade(
          promptId,
          draftFor(promptId),
        );
        setProposals((current) => ({ ...current, [promptId]: proposal }));
      } catch (caught) {
        // The proposal is advisory (ADR 0004): a failed request must not lock
        // the human out of answering, so fall back to an empty proposal — no
        // verdict, no rubric — and let them confirm or override unaided.
        setProposals((current) => ({
          ...current,
          [promptId]: {
            prompt_id: promptId,
            proposed_correct: null,
            reasoning: null,
            rubric: "",
          },
        }));
        setError(
          caught instanceof Error
            ? caught.message
            : "Could not propose a grade",
        );
      }
    })();
  };

  const multipleChoice = (due: DuePrompt) => (
    <div class="flex flex-wrap gap-2" role="group">
      <For each={due.prompt.choices}>
        {(choice, choiceIndex) => (
          <Button
            onClick={() => {
              answer(due.prompt.id, { selected_index: choiceIndex() });
            }}
            size="sm"
            type="button"
            variant="outline"
          >
            {choice}
          </Button>
        )}
      </For>
    </div>
  );

  const shortAnswer = (due: DuePrompt) => (
    <div class="flex flex-wrap gap-2">
      <input
        aria-label="Your answer"
        class="border-input bg-background h-8 flex-1 rounded-md border px-2 text-sm"
        onInput={(event) => {
          setDraft(due.prompt.id, event.currentTarget.value);
        }}
        type="text"
        value={draftFor(due.prompt.id)}
      />
      <Button
        disabled={draftFor(due.prompt.id).trim() === ""}
        onClick={() => {
          answer(due.prompt.id, { answer_text: draftFor(due.prompt.id) });
        }}
        size="sm"
        type="button"
        variant="outline"
      >
        Submit answer
      </Button>
    </div>
  );

  // The essay flow keeps the human in charge of the grade (ADR 0004): the
  // model only proposes one, and the confirm/override click is what answers.
  const essay = (due: DuePrompt) => {
    const proposal = () => proposals()[due.prompt.id];
    return (
      <div class="space-y-2">
        <textarea
          aria-label="Your essay"
          class="border-input bg-background min-h-20 w-full rounded-md border px-2 py-1 text-sm"
          onInput={(event) => {
            setDraft(due.prompt.id, event.currentTarget.value);
          }}
          value={draftFor(due.prompt.id)}
        />
        <Show
          fallback={
            <Button
              disabled={draftFor(due.prompt.id).trim() === ""}
              onClick={() => {
                proposeGrade(due.prompt.id);
              }}
              size="sm"
              type="button"
              variant="outline"
            >
              Submit for grading
            </Button>
          }
          when={proposal()}
        >
          {(graded) => (
            <div class="space-y-2">
              <Show when={graded().rubric}>
                <p class="text-muted-foreground text-xs">{graded().rubric}</p>
              </Show>
              <p class="text-sm">{proposalVerdict(graded())}</p>
              <div class="flex flex-wrap gap-2" role="group">
                <Button
                  onClick={() => {
                    answer(due.prompt.id, {
                      answer_text: draftFor(due.prompt.id),
                      confirmed_correct: true,
                    });
                  }}
                  size="sm"
                  type="button"
                  variant="outline"
                >
                  Confirm correct
                </Button>
                <Button
                  onClick={() => {
                    answer(due.prompt.id, {
                      answer_text: draftFor(due.prompt.id),
                      confirmed_correct: false,
                    });
                  }}
                  size="sm"
                  type="button"
                  variant="outline"
                >
                  Mark incorrect
                </Button>
              </div>
            </div>
          )}
        </Show>
      </div>
    );
  };

  return (
    <section aria-label="Recall" class={panelClass}>
      <h2 class="mb-3 text-sm font-semibold">Recall</h2>
      <Show when={feedback()}>
        {(message) => (
          <p class="mb-2 text-sm text-emerald-600" role="status">
            {message()}
          </p>
        )}
      </Show>
      <Show when={error()}>
        {(message) => (
          <p class="text-destructive mb-2 text-sm" role="alert">
            {message()}
          </p>
        )}
      </Show>
      <Show
        fallback={
          <p class="text-muted-foreground text-sm">No recall prompts due</p>
        }
        when={(promptsQuery.data ?? []).length > 0}
      >
        <ul class="space-y-2">
          <For each={promptsQuery.data ?? []}>
            {(due) => (
              <li
                aria-label={`Recall prompt: ${due.prompt.question}`}
                class="bg-muted space-y-2 rounded-md border px-3 py-2"
              >
                <p class="text-sm font-medium">{due.prompt.question}</p>
                <span class="text-muted-foreground text-xs">{`from ${due.study_item.source_title}`}</span>
                <Switch fallback={multipleChoice(due)}>
                  <Match when={due.prompt.kind === "short_answer"}>
                    {shortAnswer(due)}
                  </Match>
                  <Match when={due.prompt.kind === "essay"}>{essay(due)}</Match>
                </Switch>
              </li>
            )}
          </For>
        </ul>
      </Show>
    </section>
  );
}
