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

import type { Conversation, Message, TetherApi } from "./api";
import type {
  ChatBus,
  ChatFrame,
  ConnectionStatus,
  CreateChatBus,
} from "./chat-bus";
import { isPinned, restoredScrollTop } from "./chat-scroll";
import {
  deriveRows,
  emptyTurn,
  isAwaitingFirstToken,
  reduceFrame,
  stabilizeRows,
  startTurn,
} from "./chat-timeline";
import { willStartFreshSession } from "./session-freshness";
import type {
  ChatRole,
  LiveTurn,
  StoredMessage,
  TimelineRow,
} from "./chat-timeline";
import { ArtifactOverlay } from "./components/artifact-viewer";
import { MessageContent } from "./components/message-content";
import type { ArtifactPointer } from "./components/widgets/artifact-widget";
import { invalidateNamedKey, queryKeys } from "./lib/query-keys";
import { formatToolResult } from "./lib/tool-result";
import { BucketPanel } from "./panels/bucket";
import { MemoriesPanel } from "./panels/memories";
import { NotificationsPanel } from "./panels/notifications";
import { PushControl } from "./panels/push";
import { RecallPanel } from "./panels/recall";
import { SyntheticPanels } from "./panels/synthetic";
import { TriggersPanel } from "./panels/triggers";
import { YouTubeSyncPanel } from "./panels/youtube";
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
  // Pinned ⇔ the viewport is within PINNED_THRESHOLD_PX of the bottom. This
  // is the *only* thing that decides whether content changes move the
  // viewport, and it is only ever recomputed from a real `scroll` event —
  // never flipped directly by our own follow-scroll. That's safe because a
  // follow-scroll always lands exactly at the bottom, so recomputing from
  // geometry after it fires still reports pinned.
  const [pinned, setPinned] = createSignal(true);
  // Set right before an older-page fetch so the next render (which prepends
  // rows above the fold) restores the viewport to the same visual position
  // instead of jumping. Needed because `overflow-anchor` is off below — the
  // pinned-follow rule owns scroll position, so nothing else may adjust it.
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

  // The explicit pinned-follow rule: on *any* content change (new message,
  // streamed token, tool call start/end, a row settling, history hydrating),
  // a pinned viewport snaps to the bottom instantly (no smooth behavior); a
  // non-pinned viewport is left completely untouched so content can append
  // below without moving the viewport under the user. A pending scroll
  // restore from an older-page prepend takes priority over both, since those
  // rows changed for a reason unrelated to the live turn.
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
    // On phones the page scrolls (no fixed-height parent), so the transcript
    // needs an explicit floor or `flex-1` collapses to its content and the chat
    // reads as a sliver again. 55svh keeps it the dominant element above the
    // stacked sidebar while leaving the composer in view. At `lg` the shell is a
    // fixed-height grid, so we drop the floor and let flex fill the column.
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
        {/* Attach the marker to the assistant side, directly under the partial
            reply it belongs to, rather than floating it centre-stage. It is
            session-scoped (client state): the truncated text itself is what pi
            persisted, and durably flagging a message as interrupted would need a
            transcript schema change out of scope for this polish. */}
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

export function ChatView(props: {
  api: TetherApi;
  createChatBus: CreateChatBus;
}) {
  const queryClient = useQueryClient();
  const [bus, setBus] = createSignal<ChatBus | undefined>();
  const [draft, setDraft] = createSignal("");
  const [error, setError] = createSignal<string | undefined>();
  const [connection, setConnection] = createSignal<ConnectionStatus>("open");
  const [turn, setTurn] = createSignal<LiveTurn>(emptyTurn());
  const [messagesRefresh, setMessagesRefresh] = createSignal(0);
  const [notificationsRefresh, setNotificationsRefresh] = createSignal(0);
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
    queryFn: () => props.api.listConversations(),
    queryKey: queryKeys.conversations,
  }));
  const conversation = createMemo(() => conversationsQuery.data?.[0]);
  const conversationId = createMemo(() => conversation()?.id);

  // Ticks the "will this land on a fresh pi session" indicator forward without
  // a refetch — a plain `Date.now()` read at render time would never update
  // while the user just sits looking at the composer.
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

  // Settled history is paginated: `messagesQuery` only ever fetches the latest
  // page (bounded by `MESSAGES_PAGE_SIZE`); `accumulated` is the union-by-seq
  // of that page plus every older page fetched via `loadOlderMessages`.
  // Messages are append-only and immutable once settled, so merging by seq is
  // safe and stays gap-free as long as older pages are always fetched
  // contiguously backwards from the oldest seq seen so far.
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
        : props.api.listMessages(id, { limit: MESSAGES_PAGE_SIZE });
    },
    queryKey: [
      ...queryKeys.messages(conversationId() ?? "pending"),
      messagesRefresh(),
    ] as const,
  }));

  // Reset the accumulated pagination state whenever the conversation itself
  // changes (there is no in-app conversation switcher today, but this keeps
  // the state honest if one is ever added rather than leaking a former
  // conversation's rows/cursor into a new one).
  createEffect((previousId: string | undefined) => {
    const id = conversationId();
    if (id !== previousId) {
      setAccumulated(new Map());
      setHasMoreHistory(false);
    }
    return id;
  }, undefined);

  // Merge each latest-page fetch into the accumulated store. `hasMoreHistory`
  // reflects whether *this* page came back full — a fresh signal from the tail
  // end of history, distinct from whatever `loadOlderMessages` last learned
  // about the (unrelated) older boundary.
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
  // Reconciled against the previous frame's output so rows whose rendered
  // content hasn't changed keep the same object reference — see
  // `stabilizeRows` for why that matters (it's what stops the whole
  // transcript remounting on every streamed token).
  const rows = createMemo<TimelineRow[]>(
    (previous) => stabilizeRows(previous, deriveRows(storedMessages(), turn())),
    [],
  );
  const working = createMemo(() => isAwaitingFirstToken(turn()));

  // Clear the live turn once settled history catches up. Tracks only the stored
  // messages so a refetch after `agent_end` retires the streamed rows without
  // an effect loop, while a mid-stream invalidation (generating) is ignored.
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

  // Fetch the next-older page when the user scrolls near the top. Returns
  // whether a fetch actually started, so the caller (the scroll handler) only
  // arms its scroll-position restore when rows are really about to prepend.
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
        const page = await props.api.listMessages(id, {
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
      for (const key of frame.keys) {
        invalidateNamedKey(queryClient, key);
      }
      if (frame.keys.includes("messages")) {
        setMessagesRefresh((refresh) => refresh + 1);
      }
      return;
    }
    if (frame.type === "notify") {
      // The fired notification is already persisted host-side; refetch the
      // authoritative list rather than trusting the ephemeral frame, so the
      // panel is consistent with what a reload would show.
      setNotificationsRefresh((refresh) => refresh + 1);
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
    // Settle from authoritative storage only when the turn finishes; per-event
    // refetching caused the flicker the seam now avoids.
    if (frame.event === "agent_end" || frame.event === "error") {
      rehydrate();
    }
  };

  onMount(() => {
    const chatBus = props.createChatBus({
      onDisconnect: rehydrate,
      onFrame: handleFrame,
      onStatus: setConnection,
    });
    setBus(chatBus);
    onCleanup(() => {
      chatBus.close();
    });
  });

  const sendPrompt = () => {
    const content = draft().trim();
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

  const clearConversation = () => {
    const id = conversationId();
    if (id === undefined || clearing()) {
      return;
    }
    void (async () => {
      setClearing(true);
      setError(undefined);
      try {
        // Stop any in-flight turn first so its stream cannot resurrect the
        // transcript we are about to drop.
        if (generating()) {
          bus()?.abort(id);
        }
        await props.api.clearConversation(id);
        setInterrupted(false);
        setTurn(emptyTurn());
        // The conversation id is unchanged by a clear, so the id-keyed reset
        // effect above never fires here — drop the accumulated pagination
        // state explicitly instead of union-merging an empty page into it.
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

  const logout = () => {
    void (async () => {
      await props.api.logout();
      await queryClient.invalidateQueries({ queryKey: queryKeys.session });
    })();
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
    <main
      aria-labelledby="chat-title"
      class="flex min-h-screen flex-col lg:h-screen"
    >
      <header class="bg-card flex flex-wrap items-center gap-x-4 gap-y-2 border-b px-4 py-3 sm:px-5">
        <h1
          id="chat-title"
          class="mr-auto text-lg font-semibold tracking-tight"
        >
          Tether chat
        </h1>
        <Show when={conversation()}>
          {(currentConversation) => (
            <ModelSelector
              api={props.api}
              conversation={currentConversation()}
            />
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
        <Button onClick={logout} size="sm" type="button" variant="outline">
          Log out
        </Button>
      </header>
      <div class="mx-auto grid w-full max-w-6xl flex-1 grid-cols-1 gap-5 p-4 sm:p-5 lg:grid-cols-[minmax(0,1fr)_22rem] lg:overflow-hidden">
        <div class="flex min-h-0 flex-col gap-3">
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
        <aside class="flex min-h-0 flex-col gap-4 overflow-y-auto">
          <NotificationsPanel
            api={props.api}
            refreshToken={notificationsRefresh()}
          />
          <YouTubeSyncPanel api={props.api} />
          <RecallPanel api={props.api} />
          <SyntheticPanels api={props.api} />
          <MemoriesPanel api={props.api} />
          <BucketPanel api={props.api} />
          <TriggersPanel api={props.api} />
          <PushControl api={props.api} />
        </aside>
      </div>
      <ArtifactOverlay
        api={props.api}
        artifact={openArtifact()}
        onClose={() => {
          setOpenArtifact(null);
        }}
      />
    </main>
  );
}
