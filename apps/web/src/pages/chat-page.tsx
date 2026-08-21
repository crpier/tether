import { useSearchParams } from "@solidjs/router";
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
import type { ChatHost, Conversation } from "../host/chat";
import { isPinned, restoredScrollTop } from "../chat-scroll";
import { createLiveChatTurn } from "../live-chat-turn";
import type { ChatRole, TimelineRow } from "../live-chat-turn";
import { createSpeechPlayer } from "../speech-player";
import { toSpeechText } from "../speech-text";
import { willStartFreshSession } from "../session-freshness";
import { ArtifactOverlay } from "../components/artifact-viewer";
import { MessageContent } from "../components/message-content";
import { VoiceComposerControls } from "../components/voice-composer";
import type { ArtifactPointer } from "../components/widgets/artifact-widget";
import type { VoiceMode } from "../voice-recorder";
import { queryKeys } from "../lib/query-keys";
import { formatToolResult } from "../lib/tool-result";
import { Button } from "@/components/ui/button";
import { TextField, TextFieldTextArea } from "@/components/ui/text-field";

function messageLabel(role: ChatRole): string {
  switch (role) {
    case "assistant":
      return "Tether";
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

function toolText(row: Extract<TimelineRow, { kind: "tool" }>): string {
  return row.status === "running"
    ? `using ${row.toolName}…`
    : `used ${row.toolName}`;
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

function MessageRow(props: {
  row: TimelineRow;
  transcriptItemNumber: number;
  onOpenArtifact: (artifact: ArtifactPointer) => void;
}) {
  return (
    <Switch>
      <Match when={props.row.kind === "tool" && props.row}>
        {(tool) => {
          const args = () => formatToolDetail(tool().args);
          // Results get the deep-parse + trim treatment (see lib/tool-result):
          // huge/nested tool payloads only need to convey shape, not
          // completeness. Arguments stay untouched — they're small and the
          // model needs to see them verbatim to debug a call.
          const result = () => formatToolResult(tool().result);
          return (
            <article
              aria-label="Tool activity"
              class="bg-muted/50 text-muted-foreground mr-auto max-w-[96%] rounded-lg px-3 py-2 text-xs sm:max-w-[90%] lg:max-w-[80%]"
            >
              <div class="flex items-center gap-2">
                <Show
                  fallback={<span aria-hidden="true">✓</span>}
                  when={tool().status === "running"}
                >
                  <span
                    aria-hidden="true"
                    class="border-muted-foreground/40 border-t-muted-foreground inline-block size-3 animate-spin rounded-full border-2"
                  />
                </Show>
                <strong class={bubbleLabelClass}>{toolText(tool())}</strong>
              </div>
              {/* Keep the raw tool-call arguments out of the transcript flow —
                  dumping the model's tool-call JSON (e.g. a memory capture's
                  {"content": …}) read as an assistant message. Tuck it behind a
                  collapsed disclosure so it stays available without leaking. */}
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
            </article>
          );
        }}
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
                streaming={message().streaming}
                text={message().text}
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
  rows: TimelineRow[];
  working: boolean;
  startedAt: number | null;
  stopped: boolean;
  historyReady: boolean;
  // Triggers a fetch of the next-older page; a no-op if one is already in
  // flight or history is exhausted. Returns whether a fetch actually started,
  // so the caller only arms its scroll-position restore when rows are really
  // about to prepend.
  onNearTop: () => boolean;
  onOpenArtifact: (artifact: ArtifactPointer) => void;
}) {
  let viewport: HTMLElement | undefined;
  const [pinned, setPinned] = createSignal(true);
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
    <div class="relative flex min-h-0 flex-1 flex-col">
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
        <For each={props.rows}>
          {(row, index) => (
            <MessageRow
              onOpenArtifact={props.onOpenArtifact}
              row={row}
              transcriptItemNumber={index() + 1}
            />
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

export function ChatPage() {
  const { bus, chatFrame, connection } = useAppContext();
  const api = useHost("chat");
  const artifacts = useHost("artifacts");
  const queryClient = useQueryClient();
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
  const conversation = createMemo(() => conversationsQuery.data?.[0]);
  const conversationId = createMemo(() => conversation()?.id);

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

  const [conversationMode, setConversationMode] = createSignal(false);

  const speechPlayer = createSpeechPlayer();
  // Leaving the chat page must never leave speech running.
  onCleanup(() => {
    speechPlayer.cancel();
  });

  const liveTurn = createLiveChatTurn({
    conversationId,
    // Read once per queued prompt, so toggling never mutates queued or
    // running turns.
    replyMode: () => (conversationMode() ? "spoken" : "text"),
    history: {
      listMessages: (id, options) => api.listMessages(id, options),
      settled: () => {
        void queryClient.invalidateQueries({
          queryKey: queryKeys.conversations,
        });
        void queryClient.invalidateQueries({ queryKey: ["messages"] });
      },
    },
    onSettledReply: (text) => {
      // Only the authoritative settled final answer reaches playback, exactly
      // once, normalized for speech; failure never touches the transcript.
      const spoken = toSpeechText(text);
      if (spoken.length > 0) {
        speechPlayer.speak(spoken);
      }
    },
    transport: {
      abort: (id) => {
        bus()?.abort(id);
      },
      sendPrompt: (id, content, replyMode) => {
        bus()?.sendPrompt(id, content, replyMode);
      },
    },
  });
  const {
    abort,
    awaitingAgentEnd,
    busy,
    cancelQueuedPrompt: removeQueuedPrompt,
    dismissError,
    editQueuedPrompt: savePromptEdit,
    error,
    generating,
    handleFrame,
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

  const handleVoiceTranscript = (transcript: string, mode: VoiceMode) => {
    if (mode === "review") {
      setDraft(transcript);
      return;
    }
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
        <Show
          fallback={<p class="text-muted-foreground">Loading chat…</p>}
          when={!conversationsQuery.isLoading && conversation() !== undefined}
        >
          <MessageRows
            historyReady={historyReady()}
            onNearTop={loadOlderMessages}
            onOpenArtifact={setOpenArtifact}
            rows={rows()}
            startedAt={startedAt()}
            stopped={stopped()}
            working={working()}
          />
          <div
            aria-label="Composer context"
            class="flex shrink-0 items-center gap-2"
            role="group"
          >
            <Show when={conversation()}>
              {(currentConversation) => (
                <ModelSelector api={api} conversation={currentConversation()} />
              )}
            </Show>
            <div class="min-w-0 flex-1">
              <Show when={historyIncomplete() && !generating()}>
                <p class="text-muted-foreground text-xs" role="status">
                  Previous turn did not finish. Send a new message to recover.
                </p>
              </Show>
              <Show
                when={
                  startsFreshSession() && !generating() && !historyIncomplete()
                }
              >
                <p
                  class="text-muted-foreground text-xs"
                  title="The assistant's working context resets after a few minutes idle; chat history stays."
                >
                  Next message starts a fresh session
                </p>
              </Show>
            </div>
          </div>
          <form class="shrink-0 space-y-2" onSubmit={onSubmit}>
            <Show when={speechPlayer.state() !== "idle"}>
              <div
                aria-live="polite"
                class="bg-muted/40 flex items-center gap-2 rounded-md border px-3 py-2 text-sm"
                role="status"
              >
                <span class="flex-1">
                  {speechPlayer.state() === "error"
                    ? "Speech playback failed."
                    : "Speaking reply…"}
                </span>
                <Button
                  onClick={() => {
                    speechPlayer.cancel();
                  }}
                  size="sm"
                  type="button"
                  variant="outline"
                >
                  Stop playback
                </Button>
              </div>
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
                                disabled={awaitingAgentEnd()}
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
                            setEditingPromptContent(event.currentTarget.value);
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
                  placeholder="Message Tether…"
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
                <Button
                  aria-label="Conversation mode"
                  aria-pressed={conversationMode()}
                  class="rounded-full"
                  onClick={() => {
                    setConversationMode((enabled) => !enabled);
                  }}
                  size="sm"
                  title={
                    conversationMode()
                      ? "Conversation mode is on: replies are spoken"
                      : "Conversation mode is off"
                  }
                  type="button"
                  variant={conversationMode() ? "default" : "outline"}
                >
                  <span aria-hidden="true">🔊</span>
                </Button>
                <VoiceComposerControls
                  onRecordingStart={() => {
                    // Avoid microphone feedback from an ongoing reply.
                    speechPlayer.cancel();
                  }}
                  onTranscript={handleVoiceTranscript}
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
