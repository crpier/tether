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

import { useAppContext } from "../app-context";
import type { Conversation, Message, TetherApi } from "../api";
import type { ChatFrame } from "../chat-bus";
import { isPinned, restoredScrollTop } from "../chat-scroll";
import {
  deriveRows,
  emptyTurn,
  isAwaitingFirstToken,
  reduceFrame,
  stabilizeRows,
  startTurn,
} from "../chat-timeline";
import { willStartFreshSession } from "../session-freshness";
import type {
  ChatRole,
  LiveTurn,
  StoredMessage,
  TimelineRow,
} from "../chat-timeline";
import { ArtifactOverlay } from "../components/artifact-viewer";
import { MessageContent } from "../components/message-content";
import { VoiceComposerControls } from "../components/voice-composer";
import type { ArtifactPointer } from "../components/widgets/artifact-widget";
import type { VoiceMode } from "../voice-recorder";
import { queryKeys } from "../lib/query-keys";
import { formatToolResult } from "../lib/tool-result";
import { Button } from "@/components/ui/button";
import {
  TextField,
  TextFieldLabel,
  TextFieldTextArea,
} from "@/components/ui/text-field";

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
      return `${base} bg-primary text-primary-foreground ml-auto max-w-[80%] px-3 py-2`;
    case "assistant":
      return `${base} bg-muted mr-auto max-w-[80%] px-3 py-2`;
    case "tool":
      return `${base} text-muted-foreground mx-auto py-0.5 text-xs italic`;
  }
}

const bubbleLabelClass =
  "text-[0.7rem] font-semibold tracking-wide uppercase opacity-70";

// Default transcript page size: the latest N messages load up front, older
// ones page in on demand as the user scrolls up (see `loadOlderMessages`).
const MESSAGES_PAGE_SIZE = 30;

function ModelSelector(props: { api: TetherApi; conversation: Conversation }) {
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

  const persistModel = (model: string) => {
    if (model.length === 0 || model === selectedModel()) {
      return;
    }
    void (async () => {
      await props.api.setConversationModel(props.conversation.id, model);
      await queryClient.invalidateQueries({
        queryKey: queryKeys.conversations,
      });
    })();
  };

  return (
    <div
      aria-label="Model"
      class="flex flex-wrap items-center gap-1.5"
      role="group"
    >
      <span class="text-muted-foreground text-xs">Model</span>
      <For each={modelsQuery.data?.models ?? []}>
        {(model) => (
          <Button
            aria-pressed={selectedModel() === model.id}
            disabled={modelsQuery.isLoading}
            onClick={() => {
              persistModel(model.id);
            }}
            size="sm"
            type="button"
            variant={selectedModel() === model.id ? "default" : "outline"}
          >
            {model.display_name}
          </Button>
        )}
      </For>
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
              class="bg-muted/50 text-muted-foreground mr-auto max-w-[80%] rounded-lg px-3 py-2 text-xs"
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
              aria-label="Tether reasoning"
              class="bg-muted/50 text-muted-foreground mr-auto max-w-[80%] rounded-lg px-3 py-2 text-xs"
            >
              <button
                type="button"
                aria-expanded={open()}
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
    <div class="relative flex min-h-[55svh] flex-1 flex-col lg:min-h-0">
      <section
        ref={(element) => {
          viewport = element;
        }}
        aria-label="Chat transcript"
        class="bg-card flex-1 space-y-3 overflow-y-auto [overflow-anchor:none] rounded-xl border p-4 shadow-sm"
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
          {(row) => (
            <MessageRow onOpenArtifact={props.onOpenArtifact} row={row} />
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
  const { api, bus, chatFrame, connection } = useAppContext();
  const queryClient = useQueryClient();
  const [draft, setDraft] = createSignal("");
  const [error, setError] = createSignal<string | undefined>();
  const [turn, setTurn] = createSignal<LiveTurn>(emptyTurn());
  const [messagesRefresh, setMessagesRefresh] = createSignal(0);
  // Survives the live turn being retired by settled history, so the "stopped"
  // marker stays on the (now persisted) partial reply instead of flashing away.
  const [interrupted, setInterrupted] = createSignal(false);
  const [clearing, setClearing] = createSignal(false);
  // Signal-driven overlay (#188, no router): set by an artifact card's Open
  // click, cleared to `null` on close. `null` both hides the overlay and
  // (via ArtifactOverlay's own effect) tears down its iframe.
  const [openArtifact, setOpenArtifact] = createSignal<ArtifactPointer | null>(
    null,
  );
  const generating = createMemo(() => turn().generating);
  const canSend = createMemo(() => !generating() && draft().trim().length > 0);

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

  const [accumulated, setAccumulated] = createSignal<Map<number, Message>>(
    new Map(),
  );
  const [hasMoreHistory, setHasMoreHistory] = createSignal(false);
  const [loadingOlder, setLoadingOlder] = createSignal(false);

  const messagesQuery = createQuery(() => ({
    enabled: conversationId() !== undefined,
    queryFn: async () => {
      const id = conversationId();
      return id === undefined
        ? []
        : api.listMessages(id, { limit: MESSAGES_PAGE_SIZE });
    },
    queryKey: [
      ...queryKeys.messages(conversationId() ?? "pending"),
      messagesRefresh(),
    ] as const,
  }));

  createEffect((previousId: string | undefined) => {
    const id = conversationId();
    if (id !== previousId) {
      setAccumulated(new Map());
      setHasMoreHistory(false);
    }
    return id;
  }, undefined);

  createEffect(() => {
    const page = messagesQuery.data;
    if (page === undefined) {
      return;
    }
    setAccumulated((current) => {
      const merged = new Map(current);
      for (const message of page) {
        merged.set(message.seq, message);
      }
      return merged;
    });
    setHasMoreHistory(page.length === MESSAGES_PAGE_SIZE);
  });

  const storedMessages = createMemo<StoredMessage[]>(() =>
    Array.from(accumulated().values())
      .sort((left, right) => left.seq - right.seq)
      .map((message) => ({
        id: message.id,
        role: message.role,
        content: message.content,
        toolName: message.tool_name,
        toolArgs: message.tool_args,
        toolResult: message.tool_result,
      })),
  );
  const rows = createMemo<TimelineRow[]>(
    (previous) => stabilizeRows(previous, deriveRows(storedMessages(), turn())),
    [],
  );
  const working = createMemo(() => isAwaitingFirstToken(turn()));

  createEffect(() => {
    const stored = messagesQuery.data;
    if (stored === undefined) {
      return;
    }
    untrack(() => {
      if (!turn().generating) {
        setTurn(emptyTurn());
      }
    });
  });

  const loadOlderMessages = (): boolean => {
    const id = conversationId();
    if (id === undefined || loadingOlder() || !hasMoreHistory()) {
      return false;
    }
    const seqs = Array.from(accumulated().keys());
    if (seqs.length === 0) {
      return false;
    }
    const oldestSeq = Math.min(...seqs);
    setLoadingOlder(true);
    void (async () => {
      try {
        const page = await api.listMessages(id, {
          limit: MESSAGES_PAGE_SIZE,
          beforeSeq: oldestSeq,
        });
        setAccumulated((current) => {
          const merged = new Map(current);
          for (const message of page) {
            merged.set(message.seq, message);
          }
          return merged;
        });
        setHasMoreHistory(page.length === MESSAGES_PAGE_SIZE);
      } finally {
        setLoadingOlder(false);
      }
    })();
    return true;
  };

  const rehydrate = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.conversations });
    void queryClient.invalidateQueries({ queryKey: ["messages"] });
    setMessagesRefresh((refresh) => refresh + 1);
  };

  const handleFrame = (frame: ChatFrame) => {
    if (frame.type === "invalidate") {
      // The global handler (app.tsx) already refetches every named key; a
      // "messages" invalidate additionally needs this page's own refresh
      // token bumped, changing the query key so settled history is
      // guaranteed a fresh fetch rather than relying on an already-active
      // query picking up a bare `refetchQueries`.
      if (frame.keys.includes("messages")) {
        setMessagesRefresh((refresh) => refresh + 1);
      }
      return;
    }
    if (frame.type !== "chat") {
      return;
    }
    const currentConversationId = conversationId();
    if (
      frame.conversation_id !== undefined &&
      currentConversationId !== undefined &&
      frame.conversation_id !== currentConversationId
    ) {
      return;
    }
    setTurn((current) => reduceFrame(current, frame, Date.now()));
    if (frame.event === "abort_ack") {
      setInterrupted(true);
    }
    if (frame.event === "error") {
      setError(frame.detail ?? "Chat error");
    }
    if (frame.event === "agent_end" || frame.event === "error") {
      rehydrate();
    }
  };

  // The bus disconnect callback lives above the router (app.tsx); this page
  // only reacts to the frames the bus hands it while mounted.
  createEffect(() => {
    const frame = chatFrame();
    if (frame !== undefined) {
      handleFrame(frame);
    }
  });

  const sendPrompt = (overrideContent?: string) => {
    const content = (overrideContent ?? draft()).trim();
    const id = conversationId();
    if (content.length === 0 || id === undefined) {
      return;
    }
    setDraft("");
    setError(undefined);
    setInterrupted(false);
    setTurn(startTurn(content, Date.now()));
    bus()?.sendPrompt(id, content);
  };

  const handleVoiceTranscript = (transcript: string, mode: VoiceMode) => {
    if (mode === "review") {
      setDraft(transcript);
      return;
    }
    sendPrompt(transcript);
  };

  const clearConversation = () => {
    const id = conversationId();
    if (id === undefined || clearing()) {
      return;
    }
    void (async () => {
      setClearing(true);
      setError(undefined);
      try {
        if (generating()) {
          bus()?.abort(id);
        }
        await api.clearConversation(id);
        setInterrupted(false);
        setTurn(emptyTurn());
        setAccumulated(new Map());
        setHasMoreHistory(false);
        rehydrate();
      } catch (caught) {
        setError(
          caught instanceof Error ? caught.message : "Could not clear the chat",
        );
      } finally {
        setClearing(false);
      }
    })();
  };

  const abort = () => {
    const id = conversationId();
    if (id !== undefined) {
      bus()?.abort(id);
    }
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
    <main aria-labelledby="chat-title" class="flex min-h-full flex-1 flex-col">
      <header class="bg-card flex flex-wrap items-center gap-x-4 gap-y-2 border-b px-4 py-3 sm:px-5">
        <h1
          id="chat-title"
          class="mr-auto text-lg font-semibold tracking-tight"
        >
          Tether chat
        </h1>
        <Show when={conversation()}>
          {(currentConversation) => (
            <ModelSelector api={api} conversation={currentConversation()} />
          )}
        </Show>
        <Button
          disabled={clearing() || conversation() === undefined}
          onClick={clearConversation}
          size="sm"
          type="button"
          variant="outline"
        >
          New chat
        </Button>
      </header>
      <div class="mx-auto flex w-full max-w-4xl flex-1 flex-col gap-3 p-4 sm:p-5 lg:overflow-hidden">
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
                  setError(undefined);
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
            onNearTop={loadOlderMessages}
            onOpenArtifact={setOpenArtifact}
            rows={rows()}
            startedAt={turn().startedAt}
            stopped={turn().stopped || interrupted()}
            working={working()}
          />
          <Show when={startsFreshSession() && !generating()}>
            <p
              class="text-muted-foreground text-xs"
              title="The assistant's working context resets after a few minutes idle; chat history stays."
            >
              Next message starts a fresh session
            </p>
          </Show>
          <form class="space-y-2" onSubmit={onSubmit}>
            <TextField onChange={setDraft} value={draft()}>
              <TextFieldLabel>Message</TextFieldLabel>
              <TextFieldTextArea onKeyDown={onMessageKeyDown} />
            </TextField>
            <VoiceComposerControls
              disabled={generating()}
              onTranscript={handleVoiceTranscript}
              transcribe={(blob) => api.transcribeAudio(blob)}
            />
            <div class="flex justify-end gap-2">
              <Button disabled={!canSend()} type="submit">
                Send
              </Button>
              <Button
                disabled={!generating()}
                onClick={abort}
                type="button"
                variant="outline"
              >
                Stop
              </Button>
            </div>
          </form>
        </Show>
      </div>
      <ArtifactOverlay
        api={api}
        artifact={openArtifact()}
        onClose={() => {
          setOpenArtifact(null);
        }}
      />
    </main>
  );
}
