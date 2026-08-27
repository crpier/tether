import { A, useNavigate, useParams, useSearchParams } from "@solidjs/router";
import {
  ChatContainer,
  ChatContainerContent,
  ChatContainerScrollAnchor,
  Checkpoint,
  CheckpointIcon,
  Context as KitnContext,
  ContextContent,
  ContextContentHeader,
  ContextTrigger,
  Message as KitnMessage,
  MessageActions as KitnMessageActions,
  MessageContent as KitnMessageContent,
  PromptInput,
  PromptInputActions,
  Reasoning,
  ReasoningContent,
  ReasoningTrigger,
  ScrollButton,
  Textarea as KitnTextarea,
  Tool as KitnTool,
} from "@kitn.ai/ui/solid";
import { createQuery, useQueryClient } from "@tanstack/solid-query";
import {
  For,
  Match,
  Show,
  Suspense,
  Switch,
  createEffect,
  createMemo,
  createSignal,
  lazy,
  onCleanup,
  onMount,
  untrack,
} from "solid-js";
import type { JSX } from "solid-js";

import { useAppContext, useHost } from "../app-context";
import {
  conversationLabel,
  type ChatHost,
  type Conversation,
  type ConversationTurn,
} from "../host/chat";
import { ApiError } from "../host/error";
import { createConversationMode } from "../conversation-mode";
import { projectTimelineRows } from "../kitn-chat-projection";
import type { KitnTimelineItem } from "../kitn-chat-projection";
import { createLiveChatTurn } from "../live-chat-turn";
import type { ChatRole, TimelineRow } from "../live-chat-turn";
import { willStartFreshSession } from "../session-freshness";
import { VoiceComposerControls } from "../components/voice-composer";
import type { ArtifactPointer } from "../components/widgets/artifact-widget";
import { queryKeys } from "../lib/query-keys";
import { Button } from "@/components/ui/button";

const LazyArtifactOverlay = lazy(() =>
  import("../components/artifact-viewer").then((module) => ({
    default: module.ArtifactOverlay,
  })),
);
const LazyMessageContent = lazy(() =>
  import("../components/message-content").then((module) => ({
    default: module.MessageContent,
  })),
);

function messageLabel(role: ChatRole): string {
  switch (role) {
    case "assistant":
      return "Tether";
    case "health":
      return "Health";
    case "scheduled":
      return "Scheduled";
    case "tool":
      return "Tool";
    case "user":
      return "You";
  }
}

function bubbleClass(role: ChatRole): string {
  const base = "relative flex flex-col gap-1 text-sm";
  switch (role) {
    case "user":
      return `${base} bg-primary text-primary-foreground mx-3 w-auto max-w-none rounded-lg px-3 py-2 sm:mr-0 sm:ml-auto sm:max-w-[90%] lg:max-w-[80%]`;
    case "assistant":
      return `${base} bg-background mr-0 w-full max-w-none px-3 py-2 sm:mr-auto sm:w-auto sm:max-w-[90%] sm:rounded-lg sm:bg-muted lg:max-w-[80%]`;
    case "health":
      return `${base} border-emerald-500/40 bg-emerald-500/10 mr-0 w-full max-w-none rounded-lg border px-3 py-2 sm:mr-auto sm:w-auto sm:max-w-[90%] lg:max-w-[80%]`;
    case "scheduled":
      return `${base} border-amber-500/40 bg-amber-500/10 mr-0 w-full max-w-none rounded-lg border px-3 py-2 sm:mr-auto sm:w-auto sm:max-w-[90%] lg:max-w-[80%]`;
    case "tool":
      return `${base} text-muted-foreground mx-auto py-0.5 text-xs italic`;
  }
}

const bubbleLabelClass =
  "text-[0.7rem] font-semibold tracking-wide uppercase opacity-70";

const CHAT_INPUT_MAX_ROWS = 10;

function fitChatInputToContent(element: HTMLTextAreaElement): void {
  element.style.height = "auto";

  const style = window.getComputedStyle(element);
  const lineHeight = Number.parseFloat(style.lineHeight);
  const verticalPadding =
    Number.parseFloat(style.paddingTop) +
    Number.parseFloat(style.paddingBottom);
  const maxHeight = lineHeight * CHAT_INPUT_MAX_ROWS + verticalPadding;
  const contentHeight = element.scrollHeight;

  element.style.height = `${Math.min(contentHeight, maxHeight).toString()}px`;
  element.style.overflowY = contentHeight > maxHeight ? "auto" : "hidden";
}

function ModelSelector(props: { api: ChatHost; conversation: Conversation }) {
  const queryClient = useQueryClient();
  const modelsQuery = createQuery(() => ({
    queryFn: () => props.api.listModels(),
    queryKey: queryKeys.models,
  }));
  const selectedModel = createMemo(
    () =>
      props.conversation.selected_model ??
      modelsQuery.data?.default_model ??
      "",
  );
  const [visibleModel, setVisibleModel] = createSignal("");

  let requestedModel = "";
  let persistingModel = false;

  createEffect(() => {
    if (!persistingModel) {
      requestedModel = selectedModel();
      setVisibleModel(selectedModel());
    }
  });

  const persistRequestedModel = async () => {
    persistingModel = true;
    let persistedModel = selectedModel();
    try {
      while (requestedModel !== persistedModel) {
        const nextModel = requestedModel;
        await props.api.setConversationModel(props.conversation.id, nextModel);
        persistedModel = nextModel;
      }
      await queryClient.invalidateQueries({
        queryKey: queryKeys.conversations,
      });
    } catch (error) {
      requestedModel = selectedModel();
      setVisibleModel(selectedModel());
      throw error;
    } finally {
      persistingModel = false;
      if (requestedModel !== persistedModel) {
        void persistRequestedModel();
      }
    }
  };

  const persistModel = (model: string) => {
    if (model.length === 0 || model === requestedModel) {
      return;
    }
    requestedModel = model;
    setVisibleModel(model);
    if (!persistingModel) {
      void persistRequestedModel();
    }
  };

  return (
    <div
      aria-label="Model"
      class="w-full min-w-0 sm:w-auto sm:max-w-64 sm:shrink"
      role="group"
    >
      <Show
        fallback={
          <p class="text-muted-foreground text-xs" role="status">
            {modelsQuery.isLoading
              ? "Loading model profiles…"
              : "No model profiles available."}
          </p>
        }
        when={(modelsQuery.data?.models.length ?? 0) > 0}
      >
        <select
          aria-label="Model profile"
          class="border-input bg-background h-8 w-full truncate rounded-md border px-2 text-xs disabled:cursor-default"
          disabled={
            modelsQuery.isLoading || (modelsQuery.data?.models.length ?? 0) < 2
          }
          onChange={(event) => {
            persistModel(event.currentTarget.value);
          }}
          value={visibleModel()}
        >
          <For each={modelsQuery.data?.models ?? []}>
            {(profile) => (
              <option value={profile.id}>{profile.display_name}</option>
            )}
          </For>
        </select>
      </Show>
    </div>
  );
}

type ToolRow = Extract<TimelineRow, { kind: "tool" }>;
type DisplayRow = KitnTimelineItem;

const TOOL_LABELS: Partial<
  Record<string, { done: string; running: string; receipt?: boolean }>
> = {
  add_book: { done: "Added book", receipt: true, running: "Adding book…" },
  add_movie: { done: "Added movie", receipt: true, running: "Adding movie…" },
  add_place: { done: "Added place", receipt: true, running: "Adding place…" },
  add_purchase: {
    done: "Added purchase",
    receipt: true,
    running: "Adding purchase…",
  },
  add_travel: { done: "Added trip", receipt: true, running: "Adding trip…" },
  archive_gmail_message: {
    done: "Archived email",
    receipt: true,
    running: "Archiving email…",
  },
  complete_bucket_item: {
    done: "Completed bucket item",
    receipt: true,
    running: "Completing bucket item…",
  },
  create_artifact: {
    done: "Created document",
    receipt: true,
    running: "Creating document…",
  },
  create_panel: {
    done: "Created panel",
    receipt: true,
    running: "Creating panel…",
  },
  create_todo: {
    done: "Created todo",
    receipt: true,
    running: "Creating todo…",
  },
  create_trigger: {
    done: "Created reminder",
    receipt: true,
    running: "Creating reminder…",
  },
  delete_bucket_item: {
    done: "Deleted bucket item",
    receipt: true,
    running: "Deleting bucket item…",
  },
  delete_panel: {
    done: "Deleted panel",
    receipt: true,
    running: "Deleting panel…",
  },
  delete_trigger: {
    done: "Deleted reminder",
    receipt: true,
    running: "Deleting reminder…",
  },
  ignore_youtube_video: {
    done: "Ignored video",
    receipt: true,
    running: "Ignoring video…",
  },
  label_ebook: {
    done: "Labeled ebook",
    receipt: true,
    running: "Labeling ebook…",
  },
  link_todo_trigger: {
    done: "Linked reminder to todo",
    receipt: true,
    running: "Linking reminder…",
  },
  queue_memory_assimilation: {
    done: "Queued memory update",
    receipt: true,
    running: "Queueing memory update…",
  },
  read_gmail_message: { done: "Read email", running: "Reading email…" },
  record_product_observation: {
    done: "Recorded product feedback",
    receipt: true,
    running: "Recording product feedback…",
  },
  retry_youtube_video: {
    done: "Retried video",
    receipt: true,
    running: "Retrying video…",
  },
  search_gmail: { done: "Searched Gmail", running: "Searching Gmail…" },
  set_bucket_item_intent: {
    done: "Updated bucket item",
    receipt: true,
    running: "Updating bucket item…",
  },
  set_purchase_decision: {
    done: "Updated purchase decision",
    receipt: true,
    running: "Updating purchase decision…",
  },
  set_todo_status: {
    done: "Updated todo",
    receipt: true,
    running: "Updating todo…",
  },
  trash_gmail_message: {
    done: "Moved email to Trash",
    receipt: true,
    running: "Moving email to Trash…",
  },
  update_artifact: {
    done: "Updated document",
    receipt: true,
    running: "Updating document…",
  },
  update_gmail_labels: {
    done: "Updated email labels",
    receipt: true,
    running: "Updating email labels…",
  },
  update_panel: {
    done: "Updated panel",
    receipt: true,
    running: "Updating panel…",
  },
  web_search: { done: "Searched the web", running: "Searching the web…" },
};

function toolResultPayload(
  value: unknown,
): Record<string, unknown> | undefined {
  if (value === null || typeof value !== "object") {
    return undefined;
  }
  const details = (value as Record<string, unknown>).details;
  if (details === null || typeof details !== "object") {
    return undefined;
  }
  const result = (details as Record<string, unknown>).result;
  return result !== null && typeof result === "object" && !Array.isArray(result)
    ? (result as Record<string, unknown>)
    : undefined;
}

function undoableArchiveMessageId(row: ToolRow): string | undefined {
  if (row.status !== "done" || row.toolName !== "archive_gmail_message") {
    return undefined;
  }
  const result = toolResultPayload(row.result);
  return result?.outcome === "done" && typeof result.message_id === "string"
    ? result.message_id
    : undefined;
}

function toolText(row: ToolRow): string {
  const configured = TOOL_LABELS[row.toolName];
  const fallbackName = row.toolName.replaceAll("_", " ");
  const label =
    configured?.[row.status] ??
    (row.status === "running"
      ? `Using ${fallbackName}…`
      : `Used ${fallbackName}`);
  if (row.status !== "done" || row.toolName !== "search_gmail") {
    return label;
  }
  const messages = toolResultPayload(row.result)?.messages;
  return Array.isArray(messages)
    ? `${label} · ${messages.length.toString()} results`
    : label;
}

// Elapsed-time label that ticks via a text node mutation rather than a signal,
// so a running turn never re-renders the whole transcript once a second.
function WorkingIndicator(props: { startedAt: number }) {
  let label: HTMLSpanElement | undefined;
  const render = () => {
    if (label) {
      const seconds = Math.max(
        0,
        Math.round((Date.now() - props.startedAt) / 1000),
      );
      label.textContent = `${seconds.toString()}s`;
    }
  };
  onMount(() => {
    render();
    const handle = window.setInterval(render, 1000);
    onCleanup(() => {
      window.clearInterval(handle);
    });
  });
  return (
    <article aria-label="Tether working" class={bubbleClass("assistant")}>
      <strong class={bubbleLabelClass}>Tether</strong>
      <p class="text-muted-foreground flex items-center gap-2 text-sm">
        <span
          aria-hidden="true"
          class="bg-muted-foreground/70 inline-block size-2 animate-pulse rounded-full"
        />
        <span>Working</span>
        <span
          ref={(element) => {
            label = element;
          }}
          class="tabular-nums opacity-70"
        />
      </p>
    </article>
  );
}

function UndoArchiveButton(props: {
  messageId: string;
  onUndo: (messageId: string) => Promise<void>;
}) {
  const [status, setStatus] = createSignal<
    "ready" | "undoing" | "done" | "error"
  >("ready");
  return (
    <Show
      fallback={<span class="text-primary font-medium">Restored to Inbox</span>}
      when={status() !== "done"}
    >
      <button
        aria-label="Undo archive"
        class="text-primary ml-auto font-medium hover:underline"
        disabled={status() === "undoing"}
        onClick={() => {
          setStatus("undoing");
          void props
            .onUndo(props.messageId)
            .then(() => {
              setStatus("done");
            })
            .catch(() => {
              setStatus("error");
            });
        }}
        type="button"
      >
        {status() === "undoing"
          ? "Undoing…"
          : status() === "error"
            ? "Retry undo"
            : "Undo"}
      </button>
    </Show>
  );
}

function CopyMessageIcon() {
  return (
    <svg
      aria-hidden="true"
      class="size-4"
      fill="none"
      stroke="currentColor"
      stroke-linecap="round"
      stroke-linejoin="round"
      stroke-width="1.75"
      viewBox="0 0 24 24"
    >
      <rect height="13" rx="2" width="13" x="9" y="9" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  );
}

function FeedbackMessageIcon() {
  return (
    <svg
      aria-hidden="true"
      class="size-4"
      fill="none"
      stroke="currentColor"
      stroke-linecap="round"
      stroke-linejoin="round"
      stroke-width="1.75"
      viewBox="0 0 24 24"
    >
      <path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4Z" />
      <path d="M12 7v4" />
      <path d="M12 15h.01" />
    </svg>
  );
}

function MessageActions(props: {
  canRecordFeedback: boolean;
  messageId: string;
  onCopy: () => void;
  onRecordFeedback: (
    messageId: string,
    interpretation: string,
  ) => Promise<void>;
}) {
  const [feedbackOpen, setFeedbackOpen] = createSignal(false);
  const [interpretation, setInterpretation] = createSignal("");
  const [feedbackStatus, setFeedbackStatus] = createSignal<
    "idle" | "saving" | "saved" | "error"
  >("idle");

  const saveFeedback = async () => {
    const expectedBehavior = interpretation().trim();
    if (expectedBehavior.length === 0 || feedbackStatus() === "saving") {
      return;
    }
    setFeedbackStatus("saving");
    try {
      await props.onRecordFeedback(props.messageId, expectedBehavior);
      setFeedbackStatus("saved");
      setFeedbackOpen(false);
    } catch {
      setFeedbackStatus("error");
    }
  };

  return (
    <KitnMessageActions class="contents text-xs">
      <div class="absolute top-1 right-2 flex gap-0.5 opacity-60 focus-within:opacity-100 hover:opacity-100">
        <button
          aria-label="Copy message"
          class="flex size-7 items-center justify-center rounded-md hover:bg-black/10 focus-visible:ring-2 focus-visible:ring-current/40 dark:hover:bg-white/10"
          onClick={props.onCopy}
          title="Copy message"
          type="button"
        >
          <CopyMessageIcon />
        </button>
        <Show when={props.canRecordFeedback && feedbackStatus() !== "saved"}>
          <button
            aria-label="Record product feedback"
            class="flex size-7 items-center justify-center rounded-md hover:bg-black/10 focus-visible:ring-2 focus-visible:ring-current/40 dark:hover:bg-white/10"
            onClick={() => {
              setFeedbackOpen(true);
              setFeedbackStatus("idle");
            }}
            title="Record product feedback"
            type="button"
          >
            <FeedbackMessageIcon />
          </button>
        </Show>
      </div>
      <Show when={feedbackOpen()}>
        <form
          class="mt-2 space-y-2 rounded-md border p-2"
          onSubmit={(event) => {
            event.preventDefault();
            void saveFeedback();
          }}
        >
          <label class="block space-y-1">
            <span class="font-medium">Expected behavior</span>
            <textarea
              aria-label="Expected behavior"
              class="border-input min-h-16 w-full rounded-md border bg-transparent p-2"
              onInput={(event) => {
                setInterpretation(event.currentTarget.value);
              }}
              placeholder="What should Tether do instead?"
              value={interpretation()}
            />
          </label>
          <div class="flex gap-2">
            <Button
              aria-label="Save feedback"
              disabled={
                interpretation().trim().length === 0 ||
                feedbackStatus() === "saving"
              }
              size="sm"
              type="submit"
            >
              {feedbackStatus() === "saving" ? "Saving…" : "Save"}
            </Button>
            <Button
              onClick={() => {
                setFeedbackOpen(false);
              }}
              size="sm"
              type="button"
              variant="ghost"
            >
              Cancel
            </Button>
          </div>
          <Show when={feedbackStatus() === "error"}>
            <p class="text-destructive" role="alert">
              Feedback could not be recorded.
            </p>
          </Show>
        </form>
      </Show>
      <Show when={feedbackStatus() === "saved"}>
        <p class="text-muted-foreground mt-1" role="status">
          Feedback recorded.
        </p>
      </Show>
    </KitnMessageActions>
  );
}

function MessageRow(props: {
  isSpoken: (text: string) => boolean;
  row: DisplayRow;
  transcriptItemNumber: number;
  onCopy: (text: string) => void;
  onOpenArtifact: (artifact: ArtifactPointer) => void;
  onOpenEvidence: (uri: string) => void;
  onRecordFeedback: (
    messageId: string,
    interpretation: string,
  ) => Promise<void>;
  onUndoArchive: (messageId: string) => Promise<void>;
}) {
  return (
    <Switch>
      <Match when={props.row.kind === "session-boundary"}>
        <Checkpoint
          aria-label="Historical Pi session boundary"
          class="text-muted-foreground text-xs"
          role="separator"
        >
          <CheckpointIcon />
          <span class="shrink-0 whitespace-nowrap">New Pi session</span>
        </Checkpoint>
      </Match>
      <Match when={props.row.kind === "tool-group" && props.row}>
        {(group) => {
          const toolIds = createMemo(() =>
            group().tools.map((tool) => tool.row.id),
          );
          return (
            <article
              aria-label="Tool activity"
              class={`chat-tool-group text-muted-foreground mx-3 w-auto max-w-none rounded-lg border px-2 py-1 text-xs sm:mr-auto sm:ml-0 sm:max-w-[90%] lg:max-w-[80%] ${group().tools.some(({ row }) => TOOL_LABELS[row.toolName]?.receipt === true) ? "border-primary/20 bg-primary/5" : "bg-muted/50"}`}
            >
              <For each={toolIds()}>
                {(_id, index) => (
                  <Show when={group().tools[index()]}>
                    {(projectedTool) => {
                      const tool = () => projectedTool().row;
                      return (
                        <div class="flex min-w-0 items-center gap-1">
                          <KitnTool
                            class={`chat-tool-trace min-w-0 flex-1 bg-transparent ${tool().status === "done" ? "chat-tool-trace-complete" : ""}`}
                            defaultOpen={tool().status === "running"}
                            toolPart={{
                              ...projectedTool().toolPart,
                              type: toolText(tool()),
                            }}
                          />
                          <Show when={undoableArchiveMessageId(tool())}>
                            {(messageId) => (
                              <UndoArchiveButton
                                messageId={messageId()}
                                onUndo={props.onUndoArchive}
                              />
                            )}
                          </Show>
                        </div>
                      );
                    }}
                  </Show>
                )}
              </For>
            </article>
          );
        }}
      </Match>
      <Match when={props.row.kind === "reasoning" && props.row}>
        {(projectedReasoning) => {
          const reasoning = () => projectedReasoning().reasoning;
          return (
            <article
              aria-label={`Tether reasoning for transcript item ${props.transcriptItemNumber.toString()}`}
              class="bg-muted/50 text-muted-foreground mr-0 w-full max-w-none rounded-lg px-3 py-2 text-xs sm:mr-auto sm:w-auto sm:max-w-[90%] lg:max-w-[80%]"
            >
              <Reasoning
                defaultOpen={!reasoning().done}
                isStreaming={!reasoning().done}
              >
                <ReasoningTrigger
                  aria-label={`Thinking details for transcript item ${props.transcriptItemNumber.toString()}`}
                  class="w-full text-left"
                >
                  <strong class={bubbleLabelClass}>Thinking</strong>
                </ReasoningTrigger>
                <ReasoningContent>
                  <p class="mt-1 whitespace-pre-wrap break-words">
                    {reasoning().text}
                  </p>
                </ReasoningContent>
              </Reasoning>
            </article>
          );
        }}
      </Match>
      <Match when={props.row.kind === "message" && props.row}>
        {(projectedMessage) => {
          const message = () => projectedMessage().message;
          return (
            <KitnMessage
              aria-label={projectedMessage().ariaLabel}
              class={bubbleClass(message().role)}
              data-message-id={projectedMessage().id}
              role={projectedMessage().role}
            >
              <strong class={bubbleLabelClass}>
                {messageLabel(message().role)}
              </strong>
              <Show when={message().role === "scheduled" && message().turn}>
                {(turn) => (
                  <div class="text-muted-foreground space-y-0.5 text-xs">
                    <p>
                      Intended {turn().intended_fire_at ?? "time unavailable"} ·{" "}
                      {turn().status}
                    </p>
                    <Show when={turn().failure_summary}>
                      {(failure) => <p class="text-destructive">{failure()}</p>}
                    </Show>
                    <Show when={turn().occurrence_id}>
                      {(occurrenceId) => (
                        <A
                          href={`/browse/reminders?occurrence=${occurrenceId()}`}
                        >
                          View scheduled occurrence
                        </A>
                      )}
                    </Show>
                  </div>
                )}
              </Show>
              <Show
                when={
                  message().role === "assistant" &&
                  props.isSpoken(message().text)
                }
              >
                <span
                  aria-label="Spoken reply"
                  class="ml-1 align-middle text-xs"
                  title="This reply was spoken aloud"
                >
                  🔊
                </span>
              </Show>
              <Show
                fallback={
                  <KitnMessageContent class="chat-message-plain">
                    {message().role === "tool"
                      ? `used ${message().toolName ?? message().text}`
                      : message().text}
                  </KitnMessageContent>
                }
                when={message().role === "assistant"}
              >
                <Suspense
                  fallback={
                    <KitnMessageContent class="chat-message-plain">
                      {message().text}
                    </KitnMessageContent>
                  }
                >
                  <LazyMessageContent
                    onOpenArtifact={props.onOpenArtifact}
                    onOpenEvidence={props.onOpenEvidence}
                    streaming={message().streaming}
                    text={message().text}
                  />
                </Suspense>
              </Show>
              <Show when={!message().streaming && message().role !== "tool"}>
                <MessageActions
                  canRecordFeedback={
                    message().role === "user" &&
                    !message().id.startsWith("live-")
                  }
                  messageId={message().id}
                  onCopy={() => {
                    props.onCopy(message().text);
                  }}
                  onRecordFeedback={props.onRecordFeedback}
                />
              </Show>
            </KitnMessage>
          );
        }}
      </Match>
    </Switch>
  );
}

// Scroll near the top by less than this many px triggers an older-page fetch.
const NEAR_TOP_THRESHOLD_PX = 100;

function MessageRows(props: {
  focusRowId?: string;
  rows: TimelineRow[];
  sessionGapSeconds: number;
  working: boolean;
  startedAt: number | null;
  stopped: boolean;
  historyReady: boolean;
  /** Whether this text was spoken this session (🔊 chip, #546). */
  isSpoken: (text: string) => boolean;
  // Triggers the next-older page near the top. Kitn's container preserves the
  // visible anchor while the fetched rows prepend.
  onNearTop: () => boolean;
  onCopy: (text: string) => void;
  onOpenArtifact: (artifact: ArtifactPointer) => void;
  onOpenEvidence: (uri: string) => void;
  onRecordFeedback: (
    messageId: string,
    interpretation: string,
  ) => Promise<void>;
  onUndoArchive: (messageId: string) => Promise<void>;
}) {
  let viewport: HTMLElement | undefined;
  let root: HTMLDivElement | undefined;
  const displayRows = createMemo(() =>
    projectTimelineRows(props.rows, {
      sessionGapSeconds: props.sessionGapSeconds,
    }),
  );
  const displayRowIds = createMemo(() => displayRows().map((row) => row.id));
  const rowElements = new Map<string, HTMLDivElement>();
  createEffect(() => {
    const focusRowId = props.focusRowId;
    const element =
      focusRowId === undefined ? undefined : rowElements.get(focusRowId);
    if (viewport !== undefined && element !== undefined) {
      queueMicrotask(() => {
        if (viewport !== undefined) {
          viewport.scrollTop = Math.max(0, element.offsetTop - 24);
        }
      });
    }
  });
  onMount(() => {
    viewport = root?.querySelector<HTMLElement>("[role='log']") ?? undefined;
  });

  return (
    <div
      ref={(element) => {
        root = element;
      }}
      class="relative flex min-h-0 flex-1 flex-col gap-2"
    >
      <ChatContainer
        aria-label={props.historyReady ? "Chat transcript" : undefined}
        class="bg-background relative flex-1 border-0 shadow-none sm:rounded-xl sm:border sm:bg-card sm:shadow-sm"
        onScroll={(event) => {
          viewport = event.currentTarget;
          if (event.currentTarget.scrollTop < NEAR_TOP_THRESHOLD_PX) {
            props.onNearTop();
          }
        }}
      >
        <ChatContainerContent class="space-y-3 px-0 py-3 sm:p-3">
          <For each={displayRowIds()}>
            {(_id, index) => (
              <Show when={displayRows()[index()]}>
                {(row) => {
                  return (
                    <div
                      ref={(element) => {
                        rowElements.set(row().id, element);
                      }}
                    >
                      <MessageRow
                        isSpoken={props.isSpoken}
                        onCopy={props.onCopy}
                        onOpenArtifact={props.onOpenArtifact}
                        onOpenEvidence={props.onOpenEvidence}
                        onRecordFeedback={props.onRecordFeedback}
                        onUndoArchive={props.onUndoArchive}
                        row={row()}
                        transcriptItemNumber={index() + 1}
                      />
                    </div>
                  );
                }}
              </Show>
            )}
          </For>
          <Show when={props.working && props.startedAt !== null}>
            <WorkingIndicator startedAt={props.startedAt ?? Date.now()} />
          </Show>
          <Show when={props.stopped}>
            <p
              aria-label="Generation stopped"
              class="text-muted-foreground mr-auto flex items-center gap-1.5 py-0.5 text-xs italic"
              role="status"
            >
              <span
                aria-hidden="true"
                class="bg-muted-foreground/60 inline-block size-1.5 rounded-full"
              />
              Generation stopped.
            </p>
          </Show>
          <ChatContainerScrollAnchor />
        </ChatContainerContent>
        <div class="pointer-events-none absolute inset-x-0 bottom-3 flex justify-center">
          <ScrollButton class="pointer-events-auto shadow" variant="outline" />
        </div>
      </ChatContainer>
    </div>
  );
}

function FocusedTurnLifecycle(props: { turn: ConversationTurn }) {
  return (
    <article
      aria-label="Conversation turn lifecycle"
      class="bg-muted/40 space-y-1 rounded-lg border px-3 py-2 text-sm"
    >
      <p class="font-medium">
        {props.turn.origin === "health"
          ? "Health moment"
          : props.turn.origin === "scheduled"
            ? "Scheduled prompt"
            : "Prompt"}
      </p>
      <p class="whitespace-pre-wrap break-words">{props.turn.prompt}</p>
      <p class="text-muted-foreground text-xs">Status: {props.turn.status}</p>
      <Show when={props.turn.failure_summary}>
        {(summary) => <p class="text-destructive text-xs">{summary()}</p>}
      </Show>
    </article>
  );
}

function ConversationLoadError(props: { onRetry: () => void }) {
  return (
    <div
      class="bg-card m-auto max-w-md rounded-lg border p-6 shadow-sm"
      role="alert"
    >
      <h1 class="text-xl font-semibold">Conversation could not be loaded</h1>
      <p class="text-muted-foreground mt-2 text-sm">
        Check the host connection and try again.
      </p>
      <Button class="mt-4" onClick={props.onRetry} type="button">
        Retry
      </Button>
    </div>
  );
}

function ConversationNotFound() {
  return (
    <div class="bg-card m-auto max-w-md rounded-lg border p-6 shadow-sm">
      <h1 class="text-xl font-semibold">Conversation not found</h1>
      <p class="text-muted-foreground mt-2 text-sm">
        This Conversation does not exist.
      </p>
      <A class="text-primary mt-4 inline-block font-medium" href="/chat">
        Main Chat
      </A>
    </div>
  );
}

export function ChatPage() {
  const { bus, chatFrame, connection, openEvidence } = useAppContext();
  const api = useHost("chat");
  const artifacts = useHost("artifacts");
  const productObservations = useHost("productObservations");
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const params = useParams<{ conversationId?: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const promptParam = searchParams.prompt;
  const starterPrompt = typeof promptParam === "string" ? promptParam : "";
  const [draft, setDraft] = createSignal(starterPrompt);
  const [newChatPending, setNewChatPending] = createSignal(false);
  const [newChatFailed, setNewChatFailed] = createSignal(false);
  let messageInput: HTMLTextAreaElement | undefined;
  const [editingPromptId, setEditingPromptId] = createSignal<number | null>(
    null,
  );
  const [editingPromptContent, setEditingPromptContent] = createSignal("");
  const [openArtifact, setOpenArtifact] = createSignal<ArtifactPointer | null>(
    null,
  );
  const canSend = createMemo(() => draft().trim().length > 0);

  createEffect(() => {
    draft();
    queueMicrotask(() => {
      if (messageInput !== undefined) {
        fitChatInputToContent(messageInput);
      }
    });
  });

  onMount(() => {
    const refitInput = () => {
      if (messageInput !== undefined) {
        fitChatInputToContent(messageInput);
      }
    };
    window.addEventListener("resize", refitInput);
    onCleanup(() => {
      window.removeEventListener("resize", refitInput);
    });
  });

  const conversationsQuery = createQuery(() => ({
    queryFn: () => api.listConversations(),
    queryKey: queryKeys.conversations,
  }));
  const requestedConversationQuery = createQuery(() => ({
    enabled: params.conversationId !== undefined,
    queryFn: ({ queryKey }) => api.fetchConversation(String(queryKey[2])),
    queryKey: ["conversations", "detail", params.conversationId],
  }));
  const creating = createMemo(
    () =>
      (params.conversationId === undefined && searchParams.new === "1") ||
      newChatPending(),
  );
  const archivedMode = createMemo(
    () => params.conversationId === undefined && searchParams.archived === "1",
  );
  const auxiliaryMode = createMemo(() => creating() || archivedMode());
  const conversation = createMemo(() =>
    auxiliaryMode()
      ? undefined
      : params.conversationId === undefined
        ? conversationsQuery.data?.find(
            (candidate) => candidate.kind === "main",
          )
        : requestedConversationQuery.data,
  );
  const conversationId = createMemo(() => conversation()?.id);
  const turnParam = createMemo(() => {
    const turn = searchParams.turn;
    return typeof turn === "string" && turn.length > 0 ? turn : undefined;
  });
  const routeNotFound = createMemo(
    () =>
      params.conversationId !== undefined &&
      requestedConversationQuery.error instanceof ApiError &&
      requestedConversationQuery.error.status === 404,
  );
  const routeLoadError = createMemo(
    () =>
      params.conversationId !== undefined &&
      requestedConversationQuery.isError &&
      !routeNotFound(),
  );

  createEffect(() => {
    const main = conversationsQuery.data?.find(
      (candidate) => candidate.kind === "main",
    );
    if (main !== undefined && params.conversationId === main.id) {
      navigate(`/chat${window.location.search}`, { replace: true });
    }
  });

  createEffect(() => {
    const currentBus = bus();
    const currentConversationId = conversationId();
    if (
      connection() === "open" &&
      currentBus !== undefined &&
      currentConversationId !== undefined
    ) {
      currentBus.requestSessionStatus(currentConversationId);
    }
  });

  const [nowTick, setNowTick] = createSignal(Date.now());
  onMount(() => {
    const interval = setInterval(() => {
      setNowTick(Date.now());
    }, 5000);
    onCleanup(() => {
      clearInterval(interval);
    });
  });
  const startsFreshSession = createMemo(() => {
    const current = conversation();
    if (current === undefined) {
      return false;
    }
    return willStartFreshSession(
      current.latest_activity,
      current.session_gap_seconds,
      nowTick(),
    );
  });

  // The whole spoken loop — speech player, hands-free re-arm, barge-in,
  // interaction tracking — lives behind this one interface; the page only
  // renders its state and forwards user intent into it.
  const conversationMode = createConversationMode({
    synthesize: (text, signal) => api.synthesizeSpeech(text, signal),
  });
  createEffect((previousId: string | undefined) => {
    const currentId = conversationId();
    if (previousId !== undefined && currentId !== previousId) {
      conversationMode.stop();
    }
    return currentId;
  }, undefined);

  const liveTurn = createLiveChatTurn({
    conversationId,
    durablePendingCount: () => conversation()?.pending_turn_count ?? 0,
    durableRunningTurnId: () => conversation()?.running_turn_id ?? undefined,
    focusTurnId: turnParam,
    // Read once per queued prompt, so toggling never mutates queued or
    // running turns.
    replyMode: () => conversationMode.replyMode(),
    history: {
      fetchTurn: (id, turnId) => api.fetchTurn(id, turnId),
      listMessages: (id, options) => api.listMessages(id, options),
      listNonterminalTurns: (id) => api.listNonterminalTurns(id),
      settled: () => {
        void queryClient.invalidateQueries({
          queryKey: queryKeys.conversations,
        });
        void queryClient.invalidateQueries({ queryKey: ["messages"] });
      },
    },
    spokenTurn: conversationMode.spokenTurn,
    transport: {
      abort: (id, turnId) => {
        bus()?.abort(id, turnId);
      },
      sendPrompt: (id, content, replyMode, requestId) => {
        bus()?.sendPrompt(id, content, replyMode, requestId);
      },
    },
  });
  const {
    abort,
    awaitingAgentEnd,
    busy,
    cancelQueuedPrompt: removeQueuedPrompt,
    clearContextUsage,
    contextUsage,
    dismissError,
    durablePendingCount,
    editQueuedPrompt: savePromptEdit,
    error,
    focusedMessageId,
    focusedTurn,
    focusedTurnError,
    generating,
    handleFrame,
    highestSettledSeq,
    historyIncomplete,
    historyReady,
    loadOlderMessages,
    loadedSkillCount,
    queuedPrompts,
    rows,
    sendPrompt: sendLivePrompt,
    sendQueuedPromptNow,
    startedAt,
    stopped,
    working,
  } = liveTurn;

  const markedRead = new Map<string, number>();
  const readInFlight = new Map<string, number>();
  const readFailures = new Map<string, { attempts: number; seq: number }>();
  const [readRetryRevision, setReadRetryRevision] = createSignal(0);
  createEffect(() => {
    readRetryRevision();
    const current = conversation();
    const renderedSeq = highestSettledSeq();
    if (
      current?.status !== "active" ||
      !historyReady() ||
      renderedSeq <= current.last_read_seq ||
      renderedSeq <= (markedRead.get(current.id) ?? 0) ||
      renderedSeq <= (readInFlight.get(current.id) ?? 0) ||
      (readFailures.get(current.id)?.seq === renderedSeq &&
        (readFailures.get(current.id)?.attempts ?? 0) >= 2)
    ) {
      return;
    }
    readInFlight.set(current.id, renderedSeq);
    queueMicrotask(() => {
      if (conversationId() !== current.id) {
        readInFlight.delete(current.id);
        return;
      }
      void api
        .markConversationRead(current.id, renderedSeq)
        .then(() => {
          readFailures.delete(current.id);
          markedRead.set(current.id, renderedSeq);
          void queryClient.invalidateQueries({
            queryKey: queryKeys.conversations,
          });
        })
        .catch(() => {
          const previousFailure = readFailures.get(current.id);
          const attempts =
            previousFailure?.seq === renderedSeq
              ? previousFailure.attempts + 1
              : 1;
          readFailures.set(current.id, { attempts, seq: renderedSeq });
          if (attempts === 1) {
            window.setTimeout(() => {
              setReadRetryRevision((value) => value + 1);
            }, 100);
          }
        })
        .finally(() => {
          if (readInFlight.get(current.id) === renderedSeq) {
            readInFlight.delete(current.id);
          }
        });
    });
  });

  const [archivedRestoreError, setArchivedRestoreError] =
    createSignal<string>();
  const [restoringArchivedId, setRestoringArchivedId] = createSignal<string>();
  const archivedQuery = createQuery(() => ({
    enabled: searchParams.archived === "1",
    queryFn: () => api.listConversations({ includeArchived: true }),
    queryKey: ["conversations", "archived"],
  }));
  const refreshConversations = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.conversations });
    void queryClient.invalidateQueries({
      queryKey: ["conversations", "detail", params.conversationId],
    });
    void queryClient.refetchQueries({ queryKey: queryKeys.conversations });
    void queryClient.refetchQueries({
      queryKey: ["conversations", "detail", params.conversationId],
    });
  };

  createEffect(() => {
    if (searchParams.new !== "1" || params.conversationId !== undefined) {
      return;
    }
    setNewChatPending(true);
    void api
      .createConversation({})
      .then((created) => {
        refreshConversations();
        navigate(`/chat/${created.id}`);
      })
      .catch(() => {
        setNewChatFailed(true);
        setNewChatPending(false);
        setSearchParams({ new: null }, { replace: true });
      });
  });

  const restoreArchived = (archivedConversationId: string) => {
    if (restoringArchivedId() !== undefined) {
      return;
    }
    setRestoringArchivedId(archivedConversationId);
    setArchivedRestoreError(undefined);
    void api
      .restoreConversation(archivedConversationId)
      .then(() => {
        void queryClient.refetchQueries({
          queryKey: ["conversations", "archived"],
        });
        refreshConversations();
      })
      .catch(() => {
        setArchivedRestoreError("Conversation could not be restored.");
      })
      .finally(() => {
        setRestoringArchivedId(undefined);
      });
  };

  const visibleContextUsage = createMemo(() => {
    const usage = contextUsage();
    return usage !== undefined && usage.contextTokens >= 50_000
      ? usage
      : undefined;
  });

  createEffect((wasFresh: boolean) => {
    const fresh = startsFreshSession();
    if (fresh && !wasFresh) {
      clearContextUsage();
    }
    return fresh;
  }, startsFreshSession());

  createEffect(() => {
    const frame = chatFrame();
    if (frame !== undefined) {
      untrack(() => handleFrame(frame));
    }
  });

  const sendPrompt = (overrideContent?: string) => {
    const content = (overrideContent ?? draft()).trim();
    if (content.length === 0 || conversationId() === undefined) {
      return;
    }
    // Barge-in (#546): the user taking over stops active playback.
    conversationMode.onPromptSent();
    setDraft("");
    sendLivePrompt(content);
  };

  const beginEditingQueuedPrompt = (prompt: {
    id: number;
    content: string;
  }) => {
    setEditingPromptId(prompt.id);
    setEditingPromptContent(prompt.content);
  };

  const saveQueuedPrompt = (promptId: number) => {
    savePromptEdit(promptId, editingPromptContent());
    setEditingPromptId(null);
    setEditingPromptContent("");
  };

  const cancelQueuedPrompt = (promptId: number) => {
    removeQueuedPrompt(promptId);
    if (editingPromptId() === promptId) {
      setEditingPromptId(null);
      setEditingPromptContent("");
    }
  };

  const handleVoiceTranscript = (transcript: string) => {
    sendPrompt(transcript);
  };

  const onSubmit: JSX.EventHandler<HTMLFormElement, SubmitEvent> = (event) => {
    event.preventDefault();
    sendPrompt();
  };

  // Enter sends; Shift+Enter keeps the default newline. Single-tenant app, so a
  // bare Enter is the expected fast path rather than chasing a submit button.
  const onMessageKeyDown: JSX.EventHandler<
    HTMLTextAreaElement,
    KeyboardEvent
  > = (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendPrompt();
    }
  };

  return (
    <section
      aria-labelledby="chat-title"
      class="flex h-full min-h-0 flex-1 flex-col overflow-hidden"
    >
      <h1 class="sr-only" id="chat-title">
        Tether chat
      </h1>
      <Show when={(loadedSkillCount() ?? 0) > 0}>
        <p class="sr-only" role="status">
          Skills loaded · {loadedSkillCount()}
        </p>
      </Show>
      <div class="mx-auto flex min-h-0 w-full max-w-4xl flex-1 flex-col gap-2 overflow-hidden px-0 py-2 sm:p-3">
        <Show when={connection() !== "open"}>
          <p
            class="bg-muted text-muted-foreground flex items-center gap-2 rounded-md border px-3 py-2 text-sm"
            role="status"
          >
            <span
              aria-hidden="true"
              class="bg-amber-500 inline-block size-2 animate-pulse rounded-full"
            />
            {connection() === "connecting"
              ? "Reconnecting to Tether…"
              : "Disconnected — retrying…"}
          </p>
        </Show>
        <Show when={error()}>
          {(message) => (
            <div
              class="border-destructive/40 bg-destructive/10 text-destructive flex items-start gap-2 rounded-md border px-3 py-2 text-sm"
              role="alert"
            >
              <p class="line-clamp-3 flex-1" title={message()}>
                {message()}
              </p>
              <button
                aria-label="Dismiss error"
                class="shrink-0 opacity-70 hover:opacity-100"
                onClick={() => {
                  dismissError();
                }}
                type="button"
              >
                ✕
              </button>
            </div>
          )}
        </Show>
        <Show when={newChatFailed()}>
          <div
            class="border-destructive/40 bg-destructive/10 text-destructive flex items-start gap-2 rounded-md border px-3 py-2 text-sm"
            role="alert"
          >
            <p class="flex-1">Conversation could not be created.</p>
            <button
              aria-label="Dismiss error"
              class="shrink-0 opacity-70 hover:opacity-100"
              onClick={() => setNewChatFailed(false)}
              type="button"
            >
              ✕
            </button>
          </div>
        </Show>
        <Show when={routeNotFound()}>
          <ConversationNotFound />
        </Show>
        <Show when={routeLoadError()}>
          <ConversationLoadError
            onRetry={() => {
              void requestedConversationQuery.refetch();
            }}
          />
        </Show>
        <Show when={searchParams.archived === "1"}>
          <section
            aria-label="Archived Conversations"
            class="bg-card rounded-lg border p-4"
          >
            <div class="flex items-center justify-between">
              <h2 class="font-semibold">Archived Conversations</h2>
              <A class="text-primary text-sm" href="/chat">
                Back to Main
              </A>
            </div>
            <Show when={archivedRestoreError()}>
              {(message) => (
                <p class="text-destructive mt-3 text-sm" role="alert">
                  {message()}
                </p>
              )}
            </Show>
            <ul class="mt-3 space-y-2">
              <For
                each={(archivedQuery.data ?? []).filter(
                  (candidate) => candidate.status === "archived",
                )}
              >
                {(archived) => (
                  <li class="flex items-center gap-2 rounded-md border px-3 py-2">
                    <A
                      class="min-w-0 flex-1 truncate"
                      href={`/chat/${archived.id}`}
                    >
                      {conversationLabel(archived, archivedQuery.data ?? [])}
                    </A>
                    <Button
                      aria-label={`Restore ${conversationLabel(
                        archived,
                        archivedQuery.data ?? [],
                      )}`}
                      disabled={restoringArchivedId() !== undefined}
                      onClick={() => {
                        restoreArchived(archived.id);
                      }}
                      size="sm"
                      type="button"
                    >
                      {restoringArchivedId() === archived.id
                        ? "Restoring…"
                        : "Restore"}
                    </Button>
                  </li>
                )}
              </For>
            </ul>
          </section>
        </Show>
        <Show
          fallback={
            <Show
              when={
                !creating() &&
                !routeNotFound() &&
                !routeLoadError() &&
                !archivedMode()
              }
            >
              <p class="text-muted-foreground">Loading chat…</p>
            </Show>
          }
          when={
            !creating() &&
            !routeNotFound() &&
            !routeLoadError() &&
            !archivedMode() &&
            !conversationsQuery.isLoading &&
            conversation() !== undefined
          }
        >
          <Show when={focusedTurnError()}>
            {(turnError) => (
              <div
                class={
                  turnError() === "not_found"
                    ? "text-muted-foreground rounded-lg border px-3 py-2 text-sm"
                    : "text-destructive rounded-lg border px-3 py-2 text-sm"
                }
                role="alert"
              >
                {turnError() === "not_found"
                  ? "Conversation turn was not found."
                  : "Conversation turn could not be loaded."}
              </div>
            )}
          </Show>
          <Show
            when={focusedMessageId() === undefined ? focusedTurn() : undefined}
          >
            {(turn) => <FocusedTurnLifecycle turn={turn()} />}
          </Show>
          <MessageRows
            focusRowId={focusedMessageId()}
            historyReady={historyReady()}
            isSpoken={(text) => conversationMode.isSpoken(text)}
            onCopy={(text) => {
              void navigator.clipboard.writeText(text).catch(() => undefined);
            }}
            onNearTop={loadOlderMessages}
            onOpenArtifact={setOpenArtifact}
            onOpenEvidence={openEvidence}
            onRecordFeedback={async (messageId, interpretation) => {
              const currentConversationId = conversationId();
              if (currentConversationId === undefined) {
                return;
              }
              await productObservations.recordProductObservation(
                currentConversationId,
                messageId,
                interpretation,
              );
            }}
            onUndoArchive={(messageId) =>
              api.undoGmailArchive(messageId).then(() => undefined)
            }
            rows={rows()}
            sessionGapSeconds={conversation()?.session_gap_seconds ?? 300}
            startedAt={startedAt()}
            stopped={stopped()}
            working={working()}
          />
          <Show when={conversation()?.status === "active"}>
            <div
              aria-label="Composer context"
              class="mx-3 flex shrink-0 flex-wrap items-center gap-2 sm:mx-0"
              role="group"
            >
              <Show when={conversation()}>
                {(currentConversation) => (
                  <ModelSelector
                    api={api}
                    conversation={currentConversation()}
                  />
                )}
              </Show>
              <div class="flex min-w-full flex-1 flex-wrap items-center gap-2 sm:min-w-0">
                <Show when={historyIncomplete() && !generating()}>
                  <p class="text-muted-foreground text-xs" role="status">
                    Previous turn did not finish. Send a new message to recover.
                  </p>
                </Show>
                <Show
                  when={
                    startsFreshSession() &&
                    contextUsage() === undefined &&
                    !generating() &&
                    !historyIncomplete()
                  }
                >
                  <Checkpoint
                    aria-label="Pi session boundary"
                    class="min-w-48 flex-1 text-xs"
                    role="status"
                    title="Pi's working context resets after a few minutes idle; chat history stays."
                  >
                    <CheckpointIcon />
                    <span class="shrink-0">
                      Next message starts a fresh working session
                    </span>
                  </Checkpoint>
                </Show>
                <Show when={visibleContextUsage()}>
                  {(usage) => {
                    const severityClass = () =>
                      usage().contextPercent >= 90
                        ? "text-destructive"
                        : usage().contextPercent >= 70
                          ? "text-amber-700 dark:text-amber-300"
                          : "text-muted-foreground";
                    return (
                      <KitnContext
                        maxTokens={usage().contextWindow}
                        usedTokens={usage().contextTokens}
                      >
                        <ContextTrigger
                          class={`h-7 gap-1 px-2 text-xs ${severityClass()}`}
                        />
                        <ContextContent>
                          <ContextContentHeader />
                        </ContextContent>
                      </KitnContext>
                    );
                  }}
                </Show>
              </div>
            </div>
            <form class="mx-3 shrink-0 space-y-2 sm:mx-0" onSubmit={onSubmit}>
              <Show
                when={
                  conversationMode.enabled() &&
                  generating() &&
                  conversationMode.playbackState() === "idle"
                }
              >
                <p
                  aria-live="polite"
                  class="bg-muted/40 rounded-md border px-3 py-1.5 text-sm"
                  role="status"
                >
                  Thinking…
                </p>
              </Show>
              <Show when={conversationMode.playbackState() !== "idle"}>
                <p
                  aria-live="polite"
                  class="bg-muted/40 rounded-md border px-3 py-1.5 text-sm"
                  role="status"
                >
                  {conversationMode.playbackState() === "error"
                    ? "Speech playback failed."
                    : "Speaking reply…"}
                </p>
              </Show>
              <Show when={durablePendingCount() > queuedPrompts().length}>
                <p class="text-muted-foreground text-xs" role="status">
                  {durablePendingCount().toString()} messages queued
                </p>
              </Show>
              <Show when={queuedPrompts().length > 0}>
                <section
                  aria-label="Queued messages"
                  aria-live="polite"
                  class="bg-muted/40 space-y-2 rounded-lg border p-3"
                >
                  <p class="text-muted-foreground text-xs font-medium">
                    Queued messages
                  </p>
                  <For each={queuedPrompts()}>
                    {(prompt, index) => (
                      <article
                        aria-label={`Queued message ${(index() + 1).toString()}`}
                        class="bg-background space-y-2 rounded-md border px-3 py-2"
                      >
                        <Show
                          fallback={
                            <>
                              <p class="whitespace-pre-wrap break-words text-sm">
                                {prompt.content}
                              </p>
                              <div class="flex flex-wrap gap-2">
                                <Button
                                  onClick={() => {
                                    beginEditingQueuedPrompt(prompt);
                                  }}
                                  size="sm"
                                  type="button"
                                  variant="outline"
                                >
                                  Edit
                                </Button>
                                <Button
                                  disabled={
                                    awaitingAgentEnd() ||
                                    (prompt.turnId === undefined &&
                                      prompt.retryable !== true)
                                  }
                                  onClick={() => {
                                    sendQueuedPromptNow(prompt.id);
                                  }}
                                  size="sm"
                                  type="button"
                                >
                                  Send now
                                </Button>
                                <Button
                                  onClick={() => {
                                    cancelQueuedPrompt(prompt.id);
                                  }}
                                  size="sm"
                                  type="button"
                                  variant="outline"
                                >
                                  Cancel message
                                </Button>
                              </div>
                            </>
                          }
                          when={editingPromptId() === prompt.id}
                        >
                          <label
                            class="sr-only"
                            for={`queued-prompt-${prompt.id.toString()}`}
                          >
                            Edit queued message {(index() + 1).toString()}
                          </label>
                          <textarea
                            class="border-input min-h-16 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
                            id={`queued-prompt-${prompt.id.toString()}`}
                            onInput={(event) => {
                              setEditingPromptContent(
                                event.currentTarget.value,
                              );
                            }}
                            value={editingPromptContent()}
                          />
                          <div class="flex flex-wrap gap-2">
                            <Button
                              disabled={
                                editingPromptContent().trim().length === 0
                              }
                              onClick={() => {
                                saveQueuedPrompt(prompt.id);
                              }}
                              size="sm"
                              type="button"
                            >
                              Save changes
                            </Button>
                            <Button
                              onClick={() => {
                                setEditingPromptId(null);
                                setEditingPromptContent("");
                              }}
                              size="sm"
                              type="button"
                              variant="outline"
                            >
                              Keep unchanged
                            </Button>
                          </div>
                        </Show>
                      </article>
                    )}
                  </For>
                </section>
              </Show>
              <PromptInput
                aria-label="Message composer"
                class="bg-card flex items-end gap-1 rounded-xl border p-1 shadow-sm"
                role="group"
              >
                <KitnTextarea
                  aria-label="Message"
                  autoResize={false}
                  class="min-h-11 min-w-0 flex-1 resize-none border-0 px-2 py-2 shadow-none focus-visible:ring-0"
                  onInput={(event) => {
                    setDraft(event.currentTarget.value);
                    fitChatInputToContent(event.currentTarget);
                  }}
                  onKeyDown={onMessageKeyDown}
                  placeholder={
                    conversationMode.enabled()
                      ? "Reply spoken…"
                      : "Message Tether…"
                  }
                  ref={(element) => {
                    messageInput = element;
                    queueMicrotask(() => {
                      fitChatInputToContent(element);
                      if (starterPrompt.length > 0) {
                        element.focus();
                      }
                    });
                  }}
                  rows={1}
                  value={draft()}
                />
                <PromptInputActions class="shrink-0 gap-1">
                  <VoiceComposerControls
                    active={() => conversationMode.enabled()}
                    autoStartSignal={() => conversationMode.voiceAutoStart()}
                    onEndConversation={() => {
                      conversationMode.stop();
                    }}
                    onRecordingStart={() => conversationMode.onRecordingStart()}
                    onRecordingStop={() => {
                      conversationMode.onRecordingStop();
                    }}
                    onStartConversation={() => {
                      conversationMode.start();
                    }}
                    onTranscript={handleVoiceTranscript}
                    playbackState={() => conversationMode.playbackState()}
                    recordingCancelSignal={() =>
                      conversationMode.recordingCancelSignal()
                    }
                    transcribe={(blob) => api.transcribeAudio(blob)}
                  />
                  <Button
                    aria-label={busy() ? "Queue message" : "Send"}
                    class="rounded-full"
                    disabled={!canSend()}
                    size="icon-sm"
                    title={busy() ? "Queue message" : "Send"}
                    type="submit"
                  >
                    <span aria-hidden="true">↑</span>
                  </Button>
                  <Show when={generating()}>
                    <Button
                      aria-label="Stop"
                      class="rounded-full"
                      disabled={awaitingAgentEnd()}
                      onClick={abort}
                      size="icon-sm"
                      title="Stop"
                      type="button"
                      variant="outline"
                    >
                      <span aria-hidden="true">■</span>
                    </Button>
                  </Show>
                </PromptInputActions>
              </PromptInput>
            </form>
          </Show>
        </Show>
      </div>
      <Show when={openArtifact()}>
        {(artifact) => (
          <LazyArtifactOverlay
            api={artifacts}
            artifact={artifact()}
            onClose={() => {
              setOpenArtifact(null);
            }}
          />
        )}
      </Show>
    </section>
  );
}
