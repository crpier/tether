import { createQuery, useQueryClient } from "@tanstack/solid-query";
import {
  For,
  Match,
  Show,
  Switch,
  createMemo,
  createSignal,
  onCleanup,
  onMount,
} from "solid-js";

import { useHost } from "../app-context";
import type { BucketTriageReport } from "../host/bucket";
import type { Notification } from "../host/notifications";
import type { DuePrompt, EssayGradeProposal, RecallHost } from "../host/recall";
import type { TranscriptDecision } from "../host/youtube";
import { formatDateTime, formatSyncTimestamp } from "../lib/format";
import { queryKeys } from "../lib/query-keys";
import { cx } from "../lib/cva";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { TextField, TextFieldTextArea } from "@/components/ui/text-field";

type InboxItem =
  | {
      detail: string;
      group:
        | "buy_now"
        | "duplicates"
        | "missing_price_context"
        | "stale"
        | "stale_watches"
        | "under_specified";
      id: string;
      kind: "bucket-triage";
      title: string;
    }
  | { due: DuePrompt; id: string; kind: "recall" }
  | { id: string; kind: "notification"; notification: Notification }
  | {
      decision: TranscriptDecision;
      id: string;
      kind: "transcript-decision";
    };

const KIND_LABEL: Record<InboxItem["kind"], string> = {
  "bucket-triage": "Bucket triage",
  notification: "Fired reminder",
  recall: "Recall due",
  "transcript-decision": "Transcript decision",
};

function triageItems(
  report: BucketTriageReport | undefined,
): Extract<InboxItem, { kind: "bucket-triage" }>[] {
  if (report === undefined) {
    return [];
  }
  const titleFor = (bucketItemId: string) =>
    report.active.find((item) => item.id === bucketItemId)?.title ??
    bucketItemId;
  const underSpecified = report.under_specified.map((flagged) => ({
    detail: flagged.reason,
    group: "under_specified" as const,
    id: `under-specified:${flagged.bucket_item_id}`,
    kind: "bucket-triage" as const,
    title: titleFor(flagged.bucket_item_id),
  }));
  const duplicates = report.duplicates.map((cluster) => ({
    detail: `${cluster.bucket_item_ids.length.toString()} items share one identity`,
    group: "duplicates" as const,
    id: `duplicates:${cluster.bucket_item_ids.join(",")}`,
    kind: "bucket-triage" as const,
    title: titleFor(cluster.bucket_item_ids[0] ?? ""),
  }));
  const stale = report.stale.map((staleItem) => ({
    detail: `Saved ${staleItem.intent_context.age_days.toString()} days ago · "${staleItem.intent_context.intent_context}" (${Math.round(staleItem.intent_context.decay * 100).toString()}% faded)`,
    group: "stale" as const,
    id: `stale:${staleItem.bucket_item_id}`,
    kind: "bucket-triage" as const,
    title: titleFor(staleItem.bucket_item_id),
  }));
  const missingPriceContext = report.purchase.missing_price_context.map(
    (bucketItemId) => ({
      detail: "Purchase is missing a price or store",
      group: "missing_price_context" as const,
      id: `missing-price-context:${bucketItemId}`,
      kind: "bucket-triage" as const,
      title: titleFor(bucketItemId),
    }),
  );
  const staleWatches = report.purchase.stale_watches.map((bucketItemId) => ({
    detail: "Wait decision is 30 days old",
    group: "stale_watches" as const,
    id: `stale-watch:${bucketItemId}`,
    kind: "bucket-triage" as const,
    title: titleFor(bucketItemId),
  }));
  const buyNow = report.purchase.buy_now.map((bucketItemId) => ({
    detail: "Purchase decision: buy",
    group: "buy_now" as const,
    id: `buy-now:${bucketItemId}`,
    kind: "bucket-triage" as const,
    title: titleFor(bucketItemId),
  }));
  return [
    ...underSpecified,
    ...duplicates,
    ...stale,
    ...missingPriceContext,
    ...staleWatches,
    ...buyNow,
  ];
}

function recallVerdict(proposal: EssayGradeProposal): string {
  if (proposal.proposed_correct === null) {
    return proposal.rubric
      ? "No model proposal — grade your essay against the rubric."
      : "No model proposal — grade your own essay.";
  }
  const verdict = proposal.proposed_correct ? "correct" : "incorrect";
  const reasoning = proposal.reasoning ? ` — ${proposal.reasoning}` : "";
  return `Model suggests: ${verdict}${reasoning}`;
}

// Inbox (#250): everything awaiting the user's judgment — bucket triage
// advisories, due Recall prompts, transcript decisions, and fired reminders —
// grouped by kind, master-detail. Adjudicating an item here is the one
// clearing pass; the underlying vertical (Browse's Bucket tab, etc.) still
// owns the full CRUD surface.
function createMediaQuery(query: string, fallback: boolean) {
  const [matches, setMatches] = createSignal(fallback);

  onMount(() => {
    if (
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function"
    ) {
      return;
    }
    const media = window.matchMedia(query);
    const update = () => {
      setMatches(media.matches);
    };
    update();
    media.addEventListener("change", update);
    onCleanup(() => {
      media.removeEventListener("change", update);
    });
  });

  return matches;
}

export function InboxPage() {
  const bucket = useHost("bucket");
  const notifications = useHost("notifications");
  const recall = useHost("recall");
  const youtube = useHost("youtube");
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = createSignal<string | undefined>();
  const [error, setError] = createSignal<string | undefined>();
  const isDesktop = createMediaQuery("(min-width: 1024px)", true);

  const triageQuery = createQuery(() => ({
    queryFn: () => bucket.getBucketTriage(),
    queryKey: queryKeys.bucketItemsView("triage"),
  }));
  const recallQuery = createQuery(() => ({
    queryFn: () => recall.listDueRecallPrompts(),
    queryKey: queryKeys.recall,
  }));
  const notificationsQuery = createQuery(() => ({
    queryFn: () => notifications.listNotifications(),
    queryKey: queryKeys.notifications,
  }));
  const transcriptDecisionsQuery = createQuery(() => ({
    queryFn: () => youtube.listTranscriptDecisions(),
    queryKey: queryKeys.youtubeTranscriptDecisions,
  }));

  const items = createMemo<InboxItem[]>(() => [
    ...triageItems(triageQuery.data),
    ...(recallQuery.data ?? []).map((due): InboxItem => ({
      due,
      id: `recall:${due.prompt.id}`,
      kind: "recall",
    })),
    ...(notificationsQuery.data ?? []).map((item): InboxItem => ({
      id: `notification:${item.id}`,
      kind: "notification",
      notification: item,
    })),
    ...(transcriptDecisionsQuery.data ?? []).map((decision): InboxItem => ({
      decision,
      id: `transcript-decision:${decision.video_id}`,
      kind: "transcript-decision",
    })),
  ]);

  const grouped = createMemo(() => {
    const byKind = new Map<InboxItem["kind"], InboxItem[]>();
    for (const item of items()) {
      const bucket = byKind.get(item.kind) ?? [];
      bucket.push(item);
      byKind.set(item.kind, bucket);
    }
    return byKind;
  });

  const selected = createMemo(() =>
    items().find((item) => item.id === selectedId()),
  );

  const dismissNotification = (notificationId: string) => {
    void (async () => {
      await notifications.dismissNotification(notificationId);
      setSelectedId(undefined);
      void queryClient.invalidateQueries({ queryKey: queryKeys.notifications });
    })();
  };

  const transcriptDecisionAct = (
    decision: TranscriptDecision,
    action: "keep-trying" | "give-up",
  ) => {
    setError(undefined);
    void (async () => {
      try {
        if (action === "keep-trying") {
          await youtube.keepTryingTranscript(decision.video_id);
        } else {
          await youtube.giveUpTranscript(decision.video_id);
        }
        setSelectedId(undefined);
        void queryClient.invalidateQueries({ queryKey: queryKeys.youtube });
        void queryClient.refetchQueries({
          queryKey: queryKeys.youtubeTranscriptDecisions,
        });
      } catch (caught) {
        setError(
          caught instanceof Error
            ? caught.message
            : "Could not save the transcript decision",
        );
      }
    })();
  };

  const isEmpty = createMemo(() => items().length === 0);

  return (
    <section
      aria-labelledby="inbox-title"
      class="flex min-h-full flex-1 flex-col"
    >
      <header class="bg-card border-b px-4 py-3 sm:px-5">
        <h1 id="inbox-title" class="text-lg font-semibold tracking-tight">
          Inbox
        </h1>
      </header>
      <div class="flex-1 overflow-y-auto p-4 sm:p-5">
        <Show when={error()}>
          {(message) => (
            <p class="text-destructive mb-3 text-sm" role="alert">
              {message()}
            </p>
          )}
        </Show>
        <Show
          fallback={
            <p class="text-muted-foreground text-sm">
              Nothing awaiting you — inbox zero.
            </p>
          }
          when={!isEmpty()}
        >
          <div class="flex min-h-0 flex-1 gap-4 lg:h-[calc(100vh-9rem)]">
            <ul class="w-full shrink-0 space-y-3 overflow-y-auto lg:w-80">
              <For each={[...grouped().entries()]}>
                {([kind, kindItems]) => (
                  <li>
                    <h2 class="text-muted-foreground mb-1 text-xs font-semibold tracking-wide uppercase">
                      {`${KIND_LABEL[kind]} (${kindItems.length.toString()})`}
                    </h2>
                    <ul class="overflow-hidden rounded-xl border">
                      <For each={kindItems}>
                        {(item) => (
                          <li>
                            <button
                              aria-current={selectedId() === item.id}
                              class={cx(
                                "flex w-full flex-col gap-0.5 border-b px-3 py-2 text-left text-sm last:border-0",
                                selectedId() === item.id
                                  ? "bg-accent"
                                  : "hover:bg-accent/50",
                              )}
                              aria-label={itemAccessibleName(item)}
                              data-id={item.id}
                              onClick={() => {
                                setSelectedId(item.id);
                              }}
                              type="button"
                            >
                              <span class="truncate font-medium">
                                {itemTitle(item)}
                              </span>
                              <Show when={itemVisibleMetadata(item)}>
                                {(metadata) => (
                                  <span class="text-muted-foreground truncate text-xs">
                                    {metadata()}
                                  </span>
                                )}
                              </Show>
                            </button>
                          </li>
                        )}
                      </For>
                    </ul>
                  </li>
                )}
              </For>
            </ul>
            <div class="hidden min-w-0 flex-1 overflow-y-auto lg:block">
              <Show
                fallback={
                  <p class="text-muted-foreground text-sm">
                    Select an item to review it.
                  </p>
                }
                when={isDesktop() ? selected() : undefined}
              >
                {(item) => (
                  <InboxDetail
                    dismissNotification={dismissNotification}
                    item={item()}
                    recall={recall}
                    transcriptDecisionAct={transcriptDecisionAct}
                  />
                )}
              </Show>
            </div>
            <Show when={!isDesktop() ? selected() : undefined}>
              {(item) => (
                <div class="fixed inset-0 z-30 flex flex-col overflow-y-auto bg-background p-4 lg:hidden">
                  <Button
                    class="mb-3 self-start"
                    onClick={() => {
                      setSelectedId(undefined);
                    }}
                    size="sm"
                    type="button"
                    variant="ghost"
                  >
                    ← Back to inbox
                  </Button>
                  <InboxDetail
                    dismissNotification={dismissNotification}
                    item={item()}
                    recall={recall}
                    transcriptDecisionAct={transcriptDecisionAct}
                  />
                </div>
              )}
            </Show>
          </div>
        </Show>
      </div>
    </section>
  );
}

function itemTitle(item: InboxItem): string {
  switch (item.kind) {
    case "bucket-triage":
      return item.title;
    case "recall":
      return item.due.prompt.question;
    case "notification":
      return item.notification.body;
    case "transcript-decision":
      return item.decision.title;
  }
}

function itemAccessibleName(item: InboxItem): string | undefined {
  if (item.kind !== "notification") {
    return undefined;
  }
  return `Fired reminder: ${item.notification.body} — fired ${formatSyncTimestamp(item.notification.created_at)} — id ${item.notification.id}`;
}

function itemVisibleMetadata(item: InboxItem): string | undefined {
  if (item.kind !== "notification") {
    return undefined;
  }
  return `Fired ${formatDateTime(new Date(item.notification.created_at))} · ID ${shortId(item.notification.id)}`;
}

function shortId(id: string): string {
  return id.length > 12 ? `…${id.slice(-8)}` : id;
}

function InboxDetail(props: {
  dismissNotification: (notificationId: string) => void;
  item: InboxItem;
  recall: RecallHost;
  transcriptDecisionAct: (
    decision: TranscriptDecision,
    action: "keep-trying" | "give-up",
  ) => void;
}) {
  return (
    <div
      aria-label={`Inbox item: ${itemTitle(props.item)}`}
      class="bg-card flex flex-col gap-4 rounded-xl border p-4 shadow-sm"
      data-id={props.item.id}
    >
      <div class="flex items-center gap-2">
        <Badge variant="secondary">{KIND_LABEL[props.item.kind]}</Badge>
      </div>
      <Switch>
        <Match when={props.item.kind === "bucket-triage" && props.item}>
          {(entry) => (
            <div class="space-y-2">
              <h2 class="text-lg font-semibold">{entry().title}</h2>
              <p class="text-sm">{entry().detail}</p>
              <p class="text-muted-foreground text-xs">
                Manage this item on Browse → Bucket.
              </p>
            </div>
          )}
        </Match>
        <Match when={props.item.kind === "transcript-decision" && props.item}>
          {(entry) => (
            <div class="space-y-3">
              <div>
                <h2 class="text-lg font-semibold">{entry().decision.title}</h2>
                <p class="text-muted-foreground text-xs">
                  {entry().decision.channel}
                </p>
              </div>
              <p class="text-sm">
                No configured transcript provider could retrieve this video.
                Should Tether keep trying?
              </p>
              <Show when={entry().decision.last_error}>
                {(message) => (
                  <p class="text-muted-foreground text-xs">{message()}</p>
                )}
              </Show>
              <a
                class="text-primary block text-sm underline-offset-4 hover:underline"
                href={`https://www.youtube.com/watch?v=${entry().decision.video_id}`}
                rel="noreferrer"
                target="_blank"
              >
                Watch on YouTube
              </a>
              <div class="flex gap-2">
                <Button
                  onClick={() => {
                    props.transcriptDecisionAct(
                      entry().decision,
                      "keep-trying",
                    );
                  }}
                  size="sm"
                  type="button"
                >
                  Keep trying
                </Button>
                <Button
                  onClick={() => {
                    props.transcriptDecisionAct(entry().decision, "give-up");
                  }}
                  size="sm"
                  type="button"
                  variant="outline"
                >
                  Give up
                </Button>
              </div>
            </div>
          )}
        </Match>
        <Match when={props.item.kind === "recall" && props.item}>
          {(entry) => <RecallDetail due={entry().due} recall={props.recall} />}
        </Match>
        <Match when={props.item.kind === "notification" && props.item}>
          {(entry) => (
            <div class="space-y-3">
              <p class="text-sm">{entry().notification.body}</p>
              <div class="text-muted-foreground space-y-1 text-xs">
                <p>
                  {`Fired: ${formatDateTime(new Date(entry().notification.created_at))}`}
                </p>
                <p>{`ID: ${entry().notification.id}`}</p>
              </div>
              <Button
                onClick={() => {
                  props.dismissNotification(entry().notification.id);
                }}
                size="sm"
                type="button"
              >
                Dismiss
              </Button>
            </div>
          )}
        </Match>
      </Switch>
    </div>
  );
}

function RecallDetail(props: { due: DuePrompt; recall: RecallHost }) {
  const queryClient = useQueryClient();
  const [shownAt] = createSignal(Date.now());
  const [draft, setDraft] = createSignal("");
  const [feedback, setFeedback] = createSignal<string | undefined>();
  const [error, setError] = createSignal<string | undefined>();
  const [proposal, setProposal] = createSignal<
    EssayGradeProposal | undefined
  >();

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.recall });
    void queryClient.refetchQueries({ queryKey: queryKeys.recall });
  };

  const answer = (input: {
    answer_text?: string;
    confirmed_correct?: boolean;
    selected_index?: number;
  }) => {
    const responseMs = Math.max(0, Date.now() - shownAt());
    void (async () => {
      setError(undefined);
      try {
        const outcome = await props.recall.answerRecallPrompt(
          props.due.prompt.id,
          {
            ...input,
            response_ms: responseMs,
          },
        );
        setFeedback(
          outcome.correct
            ? "Correct — nice work."
            : "Not quite — this prompt will come back sooner.",
        );
        refresh();
      } catch (caught) {
        setError(
          caught instanceof Error ? caught.message : "Could not submit answer",
        );
      }
    })();
  };

  const proposeGrade = () => {
    void (async () => {
      setError(undefined);
      try {
        setProposal(
          await props.recall.proposeEssayGrade(props.due.prompt.id, draft()),
        );
      } catch (caught) {
        setProposal({
          prompt_id: props.due.prompt.id,
          proposed_correct: null,
          reasoning: null,
          rubric: "",
        });
        setError(
          caught instanceof Error
            ? caught.message
            : "Could not propose a grade",
        );
      }
    })();
  };

  return (
    <div class="space-y-3">
      <h2 class="text-lg font-semibold">{props.due.prompt.question}</h2>
      <p class="text-muted-foreground text-xs">
        {`from ${props.due.study_item.source_title}`}
      </p>
      <Show when={feedback()}>
        {(message) => (
          <p class="text-sm text-emerald-600" role="status">
            {message()}
          </p>
        )}
      </Show>
      <Show when={error()}>
        {(message) => (
          <p class="text-destructive text-sm" role="alert">
            {message()}
          </p>
        )}
      </Show>
      <Switch
        fallback={
          <div class="flex flex-wrap gap-2" role="group">
            <For each={props.due.prompt.choices}>
              {(choice, choiceIndex) => (
                <Button
                  onClick={() => {
                    answer({ selected_index: choiceIndex() });
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
        }
      >
        <Match when={props.due.prompt.kind === "short_answer"}>
          <div class="flex flex-wrap gap-2">
            <input
              aria-label="Your answer"
              class="border-input bg-background h-8 flex-1 rounded-md border px-2 text-sm"
              onInput={(event) => {
                setDraft(event.currentTarget.value);
              }}
              type="text"
              value={draft()}
            />
            <Button
              disabled={draft().trim() === ""}
              onClick={() => {
                answer({ answer_text: draft() });
              }}
              size="sm"
              type="button"
              variant="outline"
            >
              Submit answer
            </Button>
          </div>
        </Match>
        <Match when={props.due.prompt.kind === "essay"}>
          <div class="space-y-2">
            <TextField onChange={setDraft} value={draft()}>
              <TextFieldTextArea aria-label="Your essay" />
            </TextField>
            <Show
              fallback={
                <Button
                  disabled={draft().trim() === ""}
                  onClick={proposeGrade}
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
                    <p class="text-muted-foreground text-xs">
                      {graded().rubric}
                    </p>
                  </Show>
                  <p class="text-sm">{recallVerdict(graded())}</p>
                  <div class="flex flex-wrap gap-2" role="group">
                    <Button
                      onClick={() => {
                        answer({
                          answer_text: draft(),
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
                        answer({
                          answer_text: draft(),
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
        </Match>
      </Switch>
    </div>
  );
}
