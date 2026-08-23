import { A, useNavigate, useParams, useSearchParams } from "@solidjs/router";
import { createQuery, useQueryClient } from "@tanstack/solid-query";
import {
  For,
  Match,
  Show,
  Switch,
  createEffect,
  createMemo,
  createSignal,
  onCleanup,
  onMount,
  untrack,
} from "solid-js";
import type { JSX } from "solid-js";

import { useAppContext, useHost } from "../app-context";
import {
  ConversationArchiveBlockedError,
  conversationLabel,
  type ChatHost,
  type Conversation,
  type ConversationTurn,
  type UpdateConversation,
} from "../host/chat";
import { ApiError } from "../host/error";
import { isPinned, restoredScrollTop } from "../chat-scroll";
import { createConversationMode } from "../conversation-mode";
import { createLiveChatTurn } from "../live-chat-turn";
import type { ChatRole, TimelineRow } from "../live-chat-turn";
import { willStartFreshSession } from "../session-freshness";
import { ArtifactOverlay } from "../components/artifact-viewer";
import { MessageContent } from "../components/message-content";
import { VoiceComposerControls } from "../components/voice-composer";
import type { ArtifactPointer } from "../components/widgets/artifact-widget";
import { queryKeys } from "../lib/query-keys";
import { formatToolResult } from "../lib/tool-result";
import { Button } from "@/components/ui/button";
import {
  TextField,
  TextFieldInput,
  TextFieldLabel,
  TextFieldTextArea,
} from "@/components/ui/text-field";

function messageLabel(role: ChatRole): string {
  switch (role) {
    case "assistant":
      return "Tether";
    case "scheduled":
      return "Scheduled";
    case "tool":
      return "Tool";
    case "user":
      return "You";
  }
}

function bubbleClass(role: ChatRole): string {
  const base = "flex flex-col gap-1 rounded-lg text-sm";
  switch (role) {
    case "user":
      return `${base} bg-primary text-primary-foreground ml-auto max-w-[96%] px-3 py-2 sm:max-w-[90%] lg:max-w-[80%]`;
    case "assistant":
      return `${base} bg-muted mr-auto max-w-[96%] px-3 py-2 sm:max-w-[90%] lg:max-w-[80%]`;
    case "scheduled":
      return `${base} border-amber-500/40 bg-amber-500/10 mr-auto max-w-[96%] border px-3 py-2 sm:max-w-[90%] lg:max-w-[80%]`;
    case "tool":
      return `${base} text-muted-foreground mx-auto py-0.5 text-xs italic`;
  }
}

const bubbleLabelClass =
  "text-[0.7rem] font-semibold tracking-wide uppercase opacity-70";

const CHAT_INPUT_MAX_ROWS = 10;

function formatContextTokens(tokens: number): string {
  if (tokens >= 1_000_000) {
    return `${(Math.round(tokens / 100_000) / 10).toString()}m`;
  }
  return `${Math.round(tokens / 1_000).toString()}k`;
}

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
  const selectedIndex = createMemo(() => {
    const index = (modelsQuery.data?.models ?? []).findIndex(
      (model) => model.id === selectedModel(),
    );
    return Math.max(0, index);
  });
  const [sliderIndex, setSliderIndex] = createSignal(0);
  const profilePosition = createMemo(
    () =>
      `Profile ${(sliderIndex() + 1).toString()} of ${(modelsQuery.data?.models.length ?? 1).toString()}`,
  );

  let requestedModel = "";
  let persistingModel = false;

  createEffect(() => {
    setSliderIndex(selectedIndex());
    if (!persistingModel) {
      requestedModel = selectedModel();
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
    if (!persistingModel) {
      void persistRequestedModel();
    }
  };

  return (
    <div
      aria-label="Model"
      class="w-32 shrink-0 sm:w-36"
      role="group"
      title={profilePosition()}
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
        <input
          aria-label="Model profile"
          aria-valuetext={profilePosition()}
          class="accent-primary h-6 w-full cursor-pointer disabled:cursor-default"
          disabled={
            modelsQuery.isLoading || (modelsQuery.data?.models.length ?? 0) < 2
          }
          max={(modelsQuery.data?.models.length ?? 1) - 1}
          min="0"
          onChange={(event) => {
            const profile =
              modelsQuery.data?.models[event.currentTarget.valueAsNumber];
            if (profile !== undefined) {
              persistModel(profile.id);
            }
          }}
          onInput={(event) => {
            setSliderIndex(event.currentTarget.valueAsNumber);
          }}
          step="1"
          type="range"
          value={sliderIndex()}
        />
      </Show>
    </div>
  );
}

type ToolRow = Extract<TimelineRow, { kind: "tool" }>;
type DisplayRow =
  | Exclude<TimelineRow, { kind: "tool" }>
  | { kind: "tool-group"; id: string; tools: ToolRow[] };

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

function groupedTimelineRows(rows: TimelineRow[]): DisplayRow[] {
  const grouped: DisplayRow[] = [];
  for (const row of rows) {
    const previous = grouped.at(-1);
    if (row.kind === "tool" && previous?.kind === "tool-group") {
      previous.tools.push(row);
      continue;
    }
    if (row.kind === "tool") {
      grouped.push({
        id: `tool-group-${row.id}`,
        kind: "tool-group",
        tools: [row],
      });
      continue;
    }
    grouped.push(row);
  }
  return grouped;
}

function displayRowText(row: DisplayRow): string {
  if (row.kind === "message" || row.kind === "reasoning") {
    return row.text;
  }
  return row.tools
    .map(
      (tool) =>
        `${toolText(tool)} ${formatToolDetail(tool.args)} ${formatToolResult(tool.result)}`,
    )
    .join(" ");
}

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

// Render a tool's args/result for the transcript. Strings pass through; objects
// pretty-print as JSON. Empty objects and nullish values collapse to "" so the
// caller can hide the block entirely rather than show a bare `{}`.
function formatToolDetail(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  if (
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.keys(value).length === 0
  ) {
    return "";
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "[unserializable]";
  }
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

function MessageActions(props: {
  canRecordFeedback: boolean;
  messageId: string;
  onCopy: () => void;
  onQuote: () => void;
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
    <div class="mt-2 text-xs">
      <div class="flex gap-3 opacity-70 focus-within:opacity-100 hover:opacity-100">
        <button
          aria-label="Copy message"
          class="hover:underline"
          onClick={props.onCopy}
          type="button"
        >
          Copy
        </button>
        <button
          aria-label="Quote message"
          class="hover:underline"
          onClick={props.onQuote}
          type="button"
        >
          Quote
        </button>
        <Show when={props.canRecordFeedback && feedbackStatus() !== "saved"}>
          <button
            aria-label="Record product feedback"
            class="hover:underline"
            onClick={() => {
              setFeedbackOpen(true);
              setFeedbackStatus("idle");
            }}
            type="button"
          >
            Feedback
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
    </div>
  );
}

function MessageRow(props: {
  isSpoken: (text: string) => boolean;
  row: DisplayRow;
  transcriptItemNumber: number;
  onCopy: (text: string) => void;
  onOpenArtifact: (artifact: ArtifactPointer) => void;
  onOpenEvidence: (uri: string) => void;
  onQuote: (text: string) => void;
  onRecordFeedback: (
    messageId: string,
    interpretation: string,
  ) => Promise<void>;
  onUndoArchive: (messageId: string) => Promise<void>;
}) {
  return (
    <Switch>
      <Match when={props.row.kind === "tool-group" && props.row}>
        {(group) => (
          <article
            aria-label="Tool activity"
            class={`text-muted-foreground mr-auto max-w-[96%] space-y-2 rounded-lg border px-3 py-2 text-xs sm:max-w-[90%] lg:max-w-[80%] ${group().tools.some((tool) => TOOL_LABELS[tool.toolName]?.receipt === true) ? "border-primary/20 bg-primary/5" : "bg-muted/50"}`}
          >
            <For each={group().tools}>
              {(tool) => {
                const args = () => formatToolDetail(tool.args);
                const result = () => formatToolResult(tool.result);
                return (
                  <div>
                    <div class="flex items-center gap-2">
                      <Show
                        fallback={<span aria-hidden="true">✓</span>}
                        when={tool.status === "running"}
                      >
                        <span
                          aria-hidden="true"
                          class="border-muted-foreground/40 border-t-muted-foreground inline-block size-3 animate-spin rounded-full border-2"
                        />
                      </Show>
                      <strong class={bubbleLabelClass}>{toolText(tool)}</strong>
                      <Show when={undoableArchiveMessageId(tool)}>
                        {(messageId) => (
                          <UndoArchiveButton
                            messageId={messageId()}
                            onUndo={props.onUndoArchive}
                          />
                        )}
                      </Show>
                    </div>
                    <Show when={args().length > 0}>
                      <details class="mt-1.5">
                        <summary class="cursor-pointer select-none opacity-80">
                          arguments
                        </summary>
                        <pre class="bg-background/40 mt-1 max-h-60 overflow-auto whitespace-pre-wrap break-words rounded px-2 py-1 font-mono text-[11px]">
                          {args()}
                        </pre>
                      </details>
                    </Show>
                    <Show when={result().length > 0}>
                      <details class="mt-1.5">
                        <summary class="cursor-pointer select-none opacity-80">
                          result
                        </summary>
                        <pre class="bg-background/40 mt-1 max-h-60 overflow-auto whitespace-pre-wrap break-words rounded px-2 py-1 font-mono text-[11px]">
                          {result()}
                        </pre>
                      </details>
                    </Show>
                  </div>
                );
              }}
            </For>
          </article>
        )}
      </Match>
      <Match when={props.row.kind === "reasoning" && props.row}>
        {(reasoning) => {
          // Expanded while the turn runs; auto-compacts to a toggle once it is
          // done. Tracking `done` (not `streaming`) keeps the trace open while
          // the answer streams, and lets the user re-expand a finished trace.
          const [open, setOpen] = createSignal(!reasoning().done);
          createEffect(() => {
            setOpen(!reasoning().done);
          });
          return (
            <article
              aria-label={`Tether reasoning for transcript item ${props.transcriptItemNumber.toString()}`}
              class="bg-muted/50 text-muted-foreground mr-auto max-w-[96%] rounded-lg px-3 py-2 text-xs sm:max-w-[90%] lg:max-w-[80%]"
            >
              <button
                type="button"
                aria-expanded={open()}
                aria-label={`Thinking details for transcript item ${props.transcriptItemNumber.toString()}`}
                class="flex w-full items-center gap-1 text-left"
                onClick={() => {
                  setOpen((value) => !value);
                }}
              >
                <span aria-hidden="true" class="text-[0.6rem]">
                  {open() ? "▾" : "▸"}
                </span>
                <strong class={bubbleLabelClass}>Thinking</strong>
              </button>
              <Show when={open()}>
                <p class="mt-1 whitespace-pre-wrap break-words">
                  {reasoning().text}
                </p>
              </Show>
            </article>
          );
        }}
      </Match>
      <Match when={props.row.kind === "message" && props.row}>
        {(message) => (
          <article
            aria-label={`${messageLabel(message().role)} message`}
            class={bubbleClass(message().role)}
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
                message().role === "assistant" && props.isSpoken(message().text)
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
                <p class="whitespace-pre-wrap break-words">
                  {message().role === "tool"
                    ? `used ${message().toolName ?? message().text}`
                    : message().text}
                </p>
              }
              when={message().role === "assistant"}
            >
              <MessageContent
                onOpenArtifact={props.onOpenArtifact}
                onOpenEvidence={props.onOpenEvidence}
                streaming={message().streaming}
                text={message().text}
              />
            </Show>
            <Show when={!message().streaming && message().role !== "tool"}>
              <MessageActions
                canRecordFeedback={
                  message().role === "user" && !message().id.startsWith("live-")
                }
                messageId={message().id}
                onCopy={() => {
                  props.onCopy(message().text);
                }}
                onQuote={() => {
                  props.onQuote(message().text);
                }}
                onRecordFeedback={props.onRecordFeedback}
              />
            </Show>
          </article>
        )}
      </Match>
    </Switch>
  );
}

// Scroll near the top by less than this many px triggers an older-page fetch.
const NEAR_TOP_THRESHOLD_PX = 100;

function MessageRows(props: {
  focusRowId?: string;
  rows: TimelineRow[];
  searchEnabled: boolean;
  working: boolean;
  startedAt: number | null;
  stopped: boolean;
  historyReady: boolean;
  /** Whether this text was spoken this session (🔊 chip, #546). */
  isSpoken: (text: string) => boolean;
  // Triggers a fetch of the next-older page; a no-op if one is already in
  // flight or history is exhausted. Returns whether a fetch actually started,
  // so the caller only arms its scroll-position restore when rows are really
  // about to prepend.
  onNearTop: () => boolean;
  onSearchOpen: () => Promise<void>;
  onCopy: (text: string) => void;
  onOpenArtifact: (artifact: ArtifactPointer) => void;
  onOpenEvidence: (uri: string) => void;
  onQuote: (text: string) => void;
  onRecordFeedback: (
    messageId: string,
    interpretation: string,
  ) => Promise<void>;
  onUndoArchive: (messageId: string) => Promise<void>;
}) {
  let viewport: HTMLElement | undefined;
  const [pinned, setPinned] = createSignal(true);
  const [searchOpen, setSearchOpen] = createSignal(false);
  const [searchQuery, setSearchQuery] = createSignal("");
  const [activeMatch, setActiveMatch] = createSignal(0);
  const displayRows = createMemo(() => groupedTimelineRows(props.rows));
  const matchingIds = createMemo(() => {
    const query = searchQuery().trim().toLocaleLowerCase();
    if (query.length === 0) {
      return [];
    }
    return displayRows()
      .filter((row) => displayRowText(row).toLocaleLowerCase().includes(query))
      .map((row) => row.id);
  });
  const activeMatchId = createMemo<string | undefined>(() =>
    matchingIds().at(activeMatch() % Math.max(matchingIds().length, 1)),
  );
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
  createEffect(() => {
    const matchId = activeMatchId();
    const element =
      matchId === undefined ? undefined : rowElements.get(matchId);
    if (viewport !== undefined && element !== undefined) {
      viewport.scrollTop = Math.max(
        0,
        element.offsetTop - viewport.clientHeight / 2,
      );
    }
  });
  createEffect(() => {
    void searchQuery();
    setActiveMatch(0);
  });
  let pendingRestore: { scrollHeight: number; scrollTop: number } | null = null;

  const updatePinned = () => {
    if (!viewport) {
      setPinned(true);
      return;
    }
    setPinned(
      isPinned(
        viewport.scrollTop,
        viewport.scrollHeight,
        viewport.clientHeight,
      ),
    );
  };
  const scrollToEnd = () => {
    if (viewport) {
      viewport.scrollTop = viewport.scrollHeight;
    }
  };

  createEffect(() => {
    void props.rows;
    void props.working;
    if (pendingRestore !== null && viewport !== undefined) {
      const { scrollHeight, scrollTop } = pendingRestore;
      viewport.scrollTop = restoredScrollTop(
        scrollTop,
        scrollHeight,
        viewport.scrollHeight,
      );
      pendingRestore = null;
      return;
    }
    if (pinned()) {
      queueMicrotask(scrollToEnd);
    }
  });

  return (
    <div class="relative flex min-h-0 flex-1 flex-col gap-2">
      <Show when={props.searchEnabled}>
        <Show
          fallback={
            <Button
              aria-label="Search transcript"
              class="absolute top-2 right-2 z-10 shadow-sm"
              onClick={() => {
                setSearchOpen(true);
                void props.onSearchOpen();
              }}
              size="sm"
              type="button"
              variant="secondary"
            >
              Search
            </Button>
          }
          when={searchOpen()}
        >
          <div class="bg-card flex shrink-0 items-center gap-2 rounded-lg border p-2 shadow-sm">
            <input
              aria-label="Search transcript"
              autofocus
              class="border-input min-w-0 flex-1 rounded-md border bg-transparent px-2 py-1 text-sm"
              onInput={(event) => {
                setSearchQuery(event.currentTarget.value);
              }}
              placeholder="Search transcript"
              type="search"
              value={searchQuery()}
            />
            <span
              class="text-muted-foreground min-w-14 text-right text-xs"
              role="status"
            >
              {matchingIds().length.toString()}{" "}
              {matchingIds().length === 1 ? "match" : "matches"}
            </span>
            <Button
              aria-label="Previous transcript match"
              disabled={matchingIds().length < 2}
              onClick={() => {
                setActiveMatch(
                  (current) =>
                    (current - 1 + matchingIds().length) % matchingIds().length,
                );
              }}
              size="icon-sm"
              type="button"
              variant="ghost"
            >
              ↑
            </Button>
            <Button
              aria-label="Next transcript match"
              disabled={matchingIds().length < 2}
              onClick={() => {
                setActiveMatch(
                  (current) => (current + 1) % matchingIds().length,
                );
              }}
              size="icon-sm"
              type="button"
              variant="ghost"
            >
              ↓
            </Button>
            <Button
              aria-label="Close transcript search"
              onClick={() => {
                setSearchOpen(false);
                setSearchQuery("");
              }}
              size="icon-sm"
              type="button"
              variant="ghost"
            >
              ✕
            </Button>
          </div>
        </Show>
      </Show>
      <section
        ref={(element) => {
          viewport = element;
        }}
        aria-label={props.historyReady ? "Chat transcript" : undefined}
        class="bg-card flex-1 space-y-3 overflow-y-auto [overflow-anchor:none] rounded-xl border p-3 shadow-sm"
        onScroll={() => {
          updatePinned();
          if (
            viewport !== undefined &&
            viewport.scrollTop < NEAR_TOP_THRESHOLD_PX
          ) {
            const snapshot = {
              scrollHeight: viewport.scrollHeight,
              scrollTop: viewport.scrollTop,
            };
            if (props.onNearTop()) {
              pendingRestore = snapshot;
            }
          }
        }}
      >
        <For each={displayRows()}>
          {(row, index) => {
            const matches = () => matchingIds().includes(row.id);
            const active = () => matches() && activeMatchId() === row.id;
            return (
              <div
                ref={(element) => {
                  rowElements.set(row.id, element);
                }}
                class={
                  active() ? "rounded-lg ring-2 ring-primary/60" : undefined
                }
                data-search-match={
                  active() ? "active" : matches() ? "match" : undefined
                }
              >
                <MessageRow
                  isSpoken={props.isSpoken}
                  onCopy={props.onCopy}
                  onOpenArtifact={props.onOpenArtifact}
                  onOpenEvidence={props.onOpenEvidence}
                  onQuote={props.onQuote}
                  onRecordFeedback={props.onRecordFeedback}
                  onUndoArchive={props.onUndoArchive}
                  row={row}
                  transcriptItemNumber={index() + 1}
                />
              </div>
            );
          }}
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
      </section>
      <Show when={!pinned()}>
        <Button
          class="absolute bottom-3 left-1/2 -translate-x-1/2 shadow"
          onClick={() => {
            setPinned(true);
            scrollToEnd();
          }}
          size="sm"
          type="button"
          variant="secondary"
        >
          Jump to latest ↓
        </Button>
      </Show>
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
        {props.turn.origin === "scheduled" ? "Scheduled prompt" : "Prompt"}
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

function ConversationCreateForm(props: {
  api: ChatHost;
  onCreated: (conversation: Conversation) => void;
}) {
  const [displayName, setDisplayName] = createSignal("");
  const [scopeBrief, setScopeBrief] = createSignal("");
  const [saving, setSaving] = createSignal(false);
  const [error, setError] = createSignal<string>();
  const submit: JSX.EventHandler<HTMLFormElement, SubmitEvent> = (event) => {
    event.preventDefault();
    if (saving() || scopeBrief().trim().length === 0) {
      return;
    }
    setSaving(true);
    setError(undefined);
    const name = displayName().trim();
    void props.api
      .createConversation({
        display_name: name.length > 0 ? name : undefined,
        scope_brief: scopeBrief().trim(),
      })
      .then(props.onCreated)
      .catch(() => {
        setError("Conversation could not be created.");
      })
      .finally(() => {
        setSaving(false);
      });
  };
  return (
    <form
      class="bg-card m-auto w-full max-w-lg space-y-4 rounded-lg border p-5"
      onSubmit={submit}
    >
      <div>
        <h1 class="text-lg font-semibold">New Scoped Conversation</h1>
        <p class="text-muted-foreground mt-1 text-sm">
          Give Tether a durable scope brief. The chat is named automatically
          from its first message if you leave the name blank.
        </p>
      </div>
      <TextField onChange={setDisplayName} value={displayName()}>
        <TextFieldLabel>Conversation name (optional)</TextFieldLabel>
        <TextFieldInput autofocus placeholder="Untitled chat" />
      </TextField>
      <TextField onChange={setScopeBrief} value={scopeBrief()}>
        <TextFieldLabel>Scope brief</TextFieldLabel>
        <TextFieldTextArea rows={4} />
      </TextField>
      <Show when={error()}>
        {(message) => (
          <p class="text-destructive text-sm" role="alert">
            {message()}
          </p>
        )}
      </Show>
      <div class="flex gap-2">
        <Button
          disabled={saving() || scopeBrief().trim().length === 0}
          type="submit"
        >
          {saving() ? "Creating…" : "Create conversation"}
        </Button>
        <Button as="a" href="/chat" variant="ghost">
          Cancel
        </Button>
      </div>
    </form>
  );
}

function ConversationPicker(props: {
  conversations: Conversation[];
  onClose: () => void;
}) {
  let dialog: HTMLDivElement | undefined;
  const previouslyFocused = document.activeElement;

  onMount(() => {
    const focusable = () =>
      Array.from(
        dialog?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      );
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        props.onClose();
        return;
      }
      if (event.key !== "Tab") {
        return;
      }
      const elements = focusable();
      if (elements.length === 0) {
        event.preventDefault();
        dialog?.focus();
        return;
      }
      const first = elements[0];
      const last = elements.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    queueMicrotask(() => focusable()[0]?.focus());
    onCleanup(() => {
      document.removeEventListener("keydown", onKeyDown);
      if (previouslyFocused instanceof HTMLElement) {
        previouslyFocused.focus();
      }
    });
  });

  return (
    <>
      <div class="fixed inset-0 z-40 bg-black/40" />
      <div
        ref={(element) => {
          dialog = element;
        }}
        aria-label="Choose conversation"
        aria-modal="true"
        class="bg-background fixed inset-3 z-50 flex max-h-[calc(100dvh-1.5rem)] flex-col rounded-lg border p-4 shadow-xl"
        role="dialog"
        tabindex={-1}
      >
        <div class="flex items-center justify-between">
          <h2 class="font-semibold">Choose conversation</h2>
          <Button
            aria-label="Close conversation picker"
            onClick={props.onClose}
            size="sm"
            type="button"
            variant="ghost"
          >
            Close
          </Button>
        </div>
        <nav class="mt-3 min-h-0 flex-1 overflow-y-auto flex flex-col gap-2">
          <For each={props.conversations}>
            {(candidate) => (
              <A
                class="rounded-md border px-3 py-2"
                href={
                  candidate.kind === "main" ? "/chat" : `/chat/${candidate.id}`
                }
                onClick={props.onClose}
              >
                {conversationLabel(candidate, props.conversations)}
              </A>
            )}
          </For>
        </nav>
      </div>
    </>
  );
}

function ConversationHeader(props: {
  api: ChatHost;
  conversation: Conversation;
  onArchived: () => void;
  onChanged: () => void;
}) {
  const navigate = useNavigate();
  const [editing, setEditing] = createSignal(false);
  const [displayName, setDisplayName] = createSignal(
    props.conversation.display_name ?? "",
  );
  const [scopeBrief, setScopeBrief] = createSignal(
    props.conversation.scope_brief ?? "",
  );
  const [error, setError] = createSignal<string>();
  const [restoring, setRestoring] = createSignal(false);

  createEffect((previousId: string | undefined) => {
    if (props.conversation.id !== previousId) {
      setEditing(false);
      setDisplayName(props.conversation.display_name ?? "");
      setScopeBrief(props.conversation.scope_brief ?? "");
      setError(undefined);
    }
    return props.conversation.id;
  }, undefined);

  const save = () => {
    const body: UpdateConversation = {};
    const nextDisplayName = displayName().trim();
    const nextScopeBrief = scopeBrief().trim();
    if (nextDisplayName !== props.conversation.display_name) {
      body.display_name = nextDisplayName;
    }
    if (nextScopeBrief !== props.conversation.scope_brief) {
      body.scope_brief = nextScopeBrief;
    }
    if (Object.keys(body).length === 0) {
      setEditing(false);
      return;
    }
    void props.api
      .updateConversation(props.conversation.id, body)
      .then(() => {
        setEditing(false);
        props.onChanged();
      })
      .catch(() => {
        setError("Conversation could not be updated.");
      });
  };
  const restore = () => {
    if (restoring()) {
      return;
    }
    setRestoring(true);
    setError(undefined);
    void props.api
      .restoreConversation(props.conversation.id)
      .then(props.onChanged)
      .catch(() => {
        setError("Conversation could not be restored.");
      })
      .finally(() => {
        setRestoring(false);
      });
  };
  const archive = () => {
    setError(undefined);
    void props.api
      .archiveConversation(props.conversation.id)
      .then(props.onArchived)
      .catch((caught: unknown) => {
        if (
          caught instanceof ConversationArchiveBlockedError &&
          caught.blocker === "active_prompt_trigger"
        ) {
          navigate(`/browse/reminders?conversation=${props.conversation.id}`);
          return;
        }
        setError(
          caught instanceof ConversationArchiveBlockedError
            ? "Wait for this Conversation's turns to finish before archiving."
            : "Conversation could not be archived.",
        );
      });
  };

  return (
    <header class="bg-card shrink-0 rounded-lg border px-3 py-2">
      <Show
        fallback={
          <div class="flex items-start gap-3">
            <div class="min-w-0 flex-1">
              <h2 class="truncate text-base font-semibold">
                {props.conversation.kind === "main"
                  ? "Main Chat"
                  : (props.conversation.display_name ?? "Untitled chat")}
              </h2>
              <Show when={props.conversation.kind === "scoped"}>
                <p class="text-muted-foreground line-clamp-2 text-xs">
                  {props.conversation.scope_brief}
                </p>
              </Show>
            </div>
            <Show when={props.conversation.kind === "scoped"}>
              <Show when={props.conversation.status === "active"}>
                <Button
                  aria-label="Edit conversation"
                  onClick={() => setEditing(true)}
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  Edit
                </Button>
              </Show>
              <Show
                fallback={
                  <Button
                    aria-label="Archive conversation"
                    onClick={archive}
                    size="sm"
                    type="button"
                    variant="ghost"
                  >
                    Archive
                  </Button>
                }
                when={props.conversation.status === "archived"}
              >
                <Button
                  aria-label="Restore conversation"
                  disabled={restoring()}
                  onClick={restore}
                  size="sm"
                  type="button"
                >
                  {restoring() ? "Restoring…" : "Restore"}
                </Button>
              </Show>
            </Show>
          </div>
        }
        when={editing()}
      >
        <div class="space-y-2">
          <TextField onChange={setDisplayName} value={displayName()}>
            <TextFieldLabel>Conversation name</TextFieldLabel>
            <TextFieldInput />
          </TextField>
          <TextField onChange={setScopeBrief} value={scopeBrief()}>
            <TextFieldLabel>Scope brief</TextFieldLabel>
            <TextFieldTextArea rows={3} />
          </TextField>
          <div class="flex gap-2">
            <Button
              disabled={
                displayName().trim().length === 0 ||
                scopeBrief().trim().length === 0
              }
              onClick={save}
              size="sm"
              type="button"
            >
              Save conversation
            </Button>
            <Button
              onClick={() => setEditing(false)}
              size="sm"
              type="button"
              variant="ghost"
            >
              Cancel
            </Button>
          </div>
        </div>
      </Show>
      <Show when={error()}>
        {(message) => (
          <p class="text-destructive mt-2 text-xs" role="alert">
            {message()}
          </p>
        )}
      </Show>
    </header>
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
  const [searchParams] = useSearchParams();
  const promptParam = searchParams.prompt;
  const starterPrompt = typeof promptParam === "string" ? promptParam : "";
  const [draft, setDraft] = createSignal(starterPrompt);
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
    () => params.conversationId === undefined && searchParams.new === "1",
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
    loadAllMessages,
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

  const [pickerOpen, setPickerOpen] = createSignal(false);
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
      <div class="mx-auto flex min-h-0 w-full max-w-4xl flex-1 flex-col gap-2 overflow-hidden p-2 sm:p-3">
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
        <Show when={creating()}>
          <ConversationCreateForm
            api={api}
            onCreated={(created) => {
              refreshConversations();
              navigate(`/chat/${created.id}`);
            }}
          />
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
          <div class="flex shrink-0 items-center gap-2 lg:hidden">
            <Button
              aria-label="Choose conversation"
              onClick={() => setPickerOpen(true)}
              size="sm"
              type="button"
              variant="outline"
            >
              Conversations
            </Button>
            <A class="text-primary ml-auto text-sm" href="/chat?new=1">
              New
            </A>
          </div>
          <Show when={pickerOpen()}>
            <ConversationPicker
              conversations={conversationsQuery.data ?? []}
              onClose={() => setPickerOpen(false)}
            />
          </Show>
          <Show when={conversation()}>
            {(current) => (
              <ConversationHeader
                api={api}
                conversation={current()}
                onArchived={() => {
                  refreshConversations();
                  navigate("/chat");
                }}
                onChanged={refreshConversations}
              />
            )}
          </Show>
          <div class="flex shrink-0 justify-end gap-3 text-xs">
            <A class="text-primary" href="/chat?new=1">
              New Conversation
            </A>
            <A class="text-muted-foreground" href="/chat?archived=1">
              Archived
            </A>
          </div>
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
            onQuote={(text) => {
              const quote = text
                .split("\n")
                .map((line) => `> ${line}`)
                .join("\n");
              setDraft(
                (current) =>
                  `${current}${current.trim().length > 0 ? "\n\n" : ""}${quote}\n\n`,
              );
              queueMicrotask(() => messageInput?.focus());
            }}
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
            onSearchOpen={loadAllMessages}
            onUndoArchive={(messageId) =>
              api.undoGmailArchive(messageId).then(() => undefined)
            }
            rows={rows()}
            searchEnabled={conversation()?.status === "active"}
            startedAt={startedAt()}
            stopped={stopped()}
            working={working()}
          />
          <Show when={conversation()?.status === "active"}>
            <div
              aria-label="Composer context"
              class="flex shrink-0 items-center gap-2"
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
              <div class="flex min-w-0 flex-1 flex-wrap items-center gap-2">
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
                  <p
                    class="text-muted-foreground text-xs"
                    title="Pi's working context resets after a few minutes idle; chat history stays."
                  >
                    Next message starts a fresh working session
                  </p>
                </Show>
                <Show when={visibleContextUsage()}>
                  {(usage) => {
                    const roundedPercent = () =>
                      Math.round(usage().contextPercent);
                    const severityClass = () =>
                      usage().contextPercent >= 90
                        ? "border-destructive/50 text-destructive"
                        : usage().contextPercent >= 70
                          ? "border-amber-500/50 text-amber-700 dark:text-amber-300"
                          : "text-muted-foreground";
                    return (
                      <span
                        class={`rounded-full border px-2 py-0.5 text-xs tabular-nums ${severityClass()}`}
                        role="status"
                        title={`${formatContextTokens(usage().contextTokens)} of ${formatContextTokens(usage().contextWindow)} tokens · ${roundedPercent().toString()}% of pi working context`}
                      >
                        {formatContextTokens(usage().contextTokens)} context
                      </span>
                    );
                  }}
                </Show>
              </div>
            </div>
            <form class="shrink-0 space-y-2" onSubmit={onSubmit}>
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
              <div
                aria-label="Message composer"
                class="bg-card flex items-end gap-1 rounded-xl border p-1 shadow-sm"
                role="group"
              >
                <TextField
                  class="min-w-0 flex-1 gap-0"
                  onChange={setDraft}
                  value={draft()}
                >
                  <TextFieldTextArea
                    aria-label="Message"
                    class="min-h-11 resize-none border-0 px-2 py-2 shadow-none focus-visible:ring-0"
                    onInput={(event) => {
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
                  />
                </TextField>
                <div class="flex shrink-0 items-center gap-1">
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
                </div>
              </div>
            </form>
          </Show>
        </Show>
      </div>
      <ArtifactOverlay
        api={artifacts}
        artifact={openArtifact()}
        onClose={() => {
          setOpenArtifact(null);
        }}
      />
    </section>
  );
}
