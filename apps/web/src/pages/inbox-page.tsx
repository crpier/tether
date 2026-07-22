import { createQuery, useQueryClient } from "@tanstack/solid-query";
import { For, Match, Show, Switch, createMemo, createSignal } from "solid-js";

import { useAppContext } from "../app-context";
import { ApiError } from "../api";
import type {
  BucketTriageReport,
  DuePrompt,
  EssayGradeProposal,
  Memory,
  Notification,
} from "../api";
import { formatDate, formatSyncTimestamp } from "../lib/format";
import { queryKeys } from "../lib/query-keys";
import { cx } from "../lib/cva";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  TextField,
  TextFieldInput,
  TextFieldLabel,
  TextFieldTextArea,
} from "@/components/ui/text-field";

type InboxItem =
  | { id: string; kind: "memory"; memory: Memory }
  | {
      detail: string;
      group: "duplicates" | "stale" | "under_specified";
      id: string;
      kind: "bucket-triage";
      title: string;
    }
  | { due: DuePrompt; id: string; kind: "recall" }
  | { id: string; kind: "notification"; notification: Notification };

const KIND_LABEL: Record<InboxItem["kind"], string> = {
  "bucket-triage": "Bucket triage",
  memory: "Memory review",
  notification: "Fired reminder",
  recall: "Recall due",
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
  return [...underSpecified, ...duplicates, ...stale];
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

// Inbox (#250): everything awaiting the user's judgment — memory review
// queue, bucket triage advisories, due recall prompts, and fired reminders —
// grouped by kind, master-detail. Adjudicating an item here is the one
// clearing pass; the underlying vertical (Browse's Bucket tab, etc.) still
// owns the full CRUD surface.
export function InboxPage() {
  const { api } = useAppContext();
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = createSignal<string | undefined>();
  const [error, setError] = createSignal<string | undefined>();

  const looseQuery = createQuery(() => ({
    queryFn: () => api.listMemories("loose"),
    queryKey: queryKeys.memoriesState("loose"),
  }));
  const triageQuery = createQuery(() => ({
    queryFn: () => api.getBucketTriage(),
    queryKey: queryKeys.bucketItemsView("triage"),
  }));
  const recallQuery = createQuery(() => ({
    queryFn: () => api.listDueRecallPrompts(),
    queryKey: queryKeys.recall,
  }));
  const notificationsQuery = createQuery(() => ({
    queryFn: () => api.listNotifications(),
    queryKey: queryKeys.notifications,
  }));

  const items = createMemo<InboxItem[]>(() => [
    ...(looseQuery.data ?? []).map((memoryItem): InboxItem => ({
      id: `memory:${memoryItem.id}`,
      kind: "memory",
      memory: memoryItem,
    })),
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

  const memoriesRefresh = () => {
    void queryClient.invalidateQueries({
      queryKey: queryKeys.memories,
      refetchType: "none",
    });
    void queryClient.refetchQueries({
      queryKey: queryKeys.memoriesState("loose"),
    });
  };

  const memoryAct = (item: Memory, action: "tether" | "reject") => {
    setError(undefined);
    void (async () => {
      try {
        if (action === "tether") {
          await api.tetherMemory(item.id, item.version);
        } else {
          await api.rejectMemory(item.id, item.version);
        }
        setSelectedId(undefined);
        memoriesRefresh();
      } catch (caught) {
        if (caught instanceof ApiError && caught.status === 409) {
          setError(
            "This memory changed elsewhere — refresh and review it again.",
          );
          memoriesRefresh();
          return;
        }
        setError(
          caught instanceof Error
            ? caught.message
            : `Could not ${action} the memory`,
        );
      }
    })();
  };

  const dismissNotification = (notificationId: string) => {
    void (async () => {
      await api.dismissNotification(notificationId);
      setSelectedId(undefined);
      void queryClient.invalidateQueries({ queryKey: queryKeys.notifications });
    })();
  };

  const [captureContent, setCaptureContent] = createSignal("");

  const capture = () => {
    setError(undefined);
    const content = captureContent().trim();
    if (content.length === 0) {
      setError("Write something to capture");
      return;
    }
    void (async () => {
      try {
        await api.captureMemory(content);
        setCaptureContent("");
        memoriesRefresh();
      } catch (caught) {
        setError(
          caught instanceof Error
            ? caught.message
            : "Could not capture the memory",
        );
      }
    })();
  };

  const isEmpty = createMemo(() => items().length === 0);

  return (
    <main aria-labelledby="inbox-title" class="flex min-h-full flex-1 flex-col">
      <header class="bg-card border-b px-4 py-3 sm:px-5">
        <h1 id="inbox-title" class="text-lg font-semibold tracking-tight">
          Inbox
        </h1>
      </header>
      <div class="flex-1 overflow-y-auto p-4 sm:p-5">
        {/* Capturing a new loose memory has no other home — Browse's Memories
            tab is corpus-only, and the review queue that would receive it
            lives here. */}
        <form
          class="mb-4 flex flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            capture();
          }}
        >
          <TextField
            class="min-w-[16rem] flex-1"
            onChange={setCaptureContent}
            value={captureContent()}
          >
            <TextFieldLabel>Capture</TextFieldLabel>
            <TextFieldInput name="capture" />
          </TextField>
          <Button type="submit">Capture memory</Button>
        </form>
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
                              data-id={item.id}
                              onClick={() => {
                                setSelectedId(item.id);
                              }}
                              type="button"
                            >
                              <span class="truncate font-medium">
                                {itemTitle(item)}
                              </span>
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
                when={selected()}
              >
                {(item) => (
                  <InboxDetail
                    api={api}
                    dismissNotification={dismissNotification}
                    item={item()}
                    memoryAct={memoryAct}
                  />
                )}
              </Show>
            </div>
            <Show when={selected()}>
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
                    api={api}
                    dismissNotification={dismissNotification}
                    item={item()}
                    memoryAct={memoryAct}
                  />
                </div>
              )}
            </Show>
          </div>
        </Show>
      </div>
    </main>
  );
}

function itemTitle(item: InboxItem): string {
  switch (item.kind) {
    case "memory":
      return item.memory.content;
    case "bucket-triage":
      return item.title;
    case "recall":
      return item.due.prompt.question;
    case "notification":
      return item.notification.body;
  }
}

function InboxDetail(props: {
  api: import("../api").TetherApi;
  dismissNotification: (notificationId: string) => void;
  item: InboxItem;
  memoryAct: (item: Memory, action: "tether" | "reject") => void;
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
        <Match when={props.item.kind === "memory" && props.item}>
          {(entry) => (
            <div class="space-y-3">
              <p class="text-sm">{entry().memory.content}</p>
              <p class="text-muted-foreground text-xs">
                {`captured ${formatDate(new Date(entry().memory.created_at))}`}
              </p>
              <div class="flex gap-2">
                <Button
                  onClick={() => {
                    props.memoryAct(entry().memory, "tether");
                  }}
                  size="sm"
                  type="button"
                >
                  Tether
                </Button>
                <Button
                  onClick={() => {
                    props.memoryAct(entry().memory, "reject");
                  }}
                  size="sm"
                  type="button"
                  variant="outline"
                >
                  Reject
                </Button>
              </div>
            </div>
          )}
        </Match>
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
        <Match when={props.item.kind === "recall" && props.item}>
          {(entry) => <RecallDetail api={props.api} due={entry().due} />}
        </Match>
        <Match when={props.item.kind === "notification" && props.item}>
          {(entry) => (
            <div class="space-y-3">
              <p class="text-sm">{entry().notification.body}</p>
              <p class="text-muted-foreground text-xs">
                {formatSyncTimestamp(entry().notification.created_at)}
              </p>
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

function RecallDetail(props: {
  api: import("../api").TetherApi;
  due: DuePrompt;
}) {
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
        const outcome = await props.api.answerRecallPrompt(
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
          await props.api.proposeEssayGrade(props.due.prompt.id, draft()),
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
