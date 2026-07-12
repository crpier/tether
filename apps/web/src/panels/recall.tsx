import { createQuery, useQueryClient } from "@tanstack/solid-query";
import { For, Show, createEffect, createSignal } from "solid-js";

import type { AnswerOutcome, TetherApi } from "../api";
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

export function RecallPanel(props: { api: TetherApi }) {
  const queryClient = useQueryClient();
  const promptsQuery = createQuery(() => ({
    queryFn: () => props.api.listDueRecallPrompts(),
    queryKey: queryKeys.recall,
  }));
  const [shownAt, setShownAt] = createSignal(Date.now());
  const [feedback, setFeedback] = createSignal<string | undefined>();
  const [error, setError] = createSignal<string | undefined>();

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

  const answer = (promptId: string, choiceIndex: number) => {
    const responseMs = Math.max(0, Date.now() - shownAt());
    void (async () => {
      setError(undefined);
      try {
        const outcome = await props.api.answerRecallPrompt(
          promptId,
          choiceIndex,
          responseMs,
        );
        setFeedback(recallFeedback(outcome));
        refresh();
      } catch (caught) {
        setError(
          caught instanceof Error ? caught.message : "Could not submit answer",
        );
      }
    })();
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
                <div class="flex flex-wrap gap-2" role="group">
                  <For each={due.prompt.choices}>
                    {(choice, choiceIndex) => (
                      <Button
                        onClick={() => {
                          answer(due.prompt.id, choiceIndex());
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
              </li>
            )}
          </For>
        </ul>
      </Show>
    </section>
  );
}
