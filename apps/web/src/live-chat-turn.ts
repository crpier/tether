import { createEffect, createMemo, createSignal } from "solid-js";
import type { Accessor } from "solid-js";

import type { ConversationTurn, Message } from "./host/chat";
import type { ChatFrame, ReplyMode } from "./chat-bus";
import {
  deriveRows,
  emptyTurn,
  isAwaitingFirstToken,
  reduceFrame,
  stabilizeRows,
  startTurn,
  deltaText,
  type StoredMessage,
  type TimelineRow,
} from "./live-chat-turn-state";
import { createSpokenStream } from "./spoken-stream";

const MESSAGES_PAGE_SIZE = 30;

export interface QueuedPrompt {
  id: number;
  content: string;
  conversationId: string;
  replyMode: ReplyMode;
  requestId: string;
  retryable?: boolean;
  turnId?: string;
}

export interface LiveChatHistory {
  fetchTurn?(conversationId: string, turnId: string): Promise<ConversationTurn>;
  listMessages(
    conversationId: string,
    options: { beforeSeq?: number; limit?: number; turnId?: string },
  ): Promise<Message[]>;
  listNonterminalTurns?(conversationId: string): Promise<ConversationTurn[]>;
  settled(): void;
}

export interface LiveChatTransport {
  abort(conversationId: string, turnId: string): void;
  sendPrompt(
    conversationId: string,
    content: string,
    replyMode: ReplyMode,
    requestId: string,
  ): void;
}

/**
 * Receives provisional and settled speech for captured-spoken turns (#545).
 *
 * Sentences arrive while the reply streams; `settle` delivers whatever the
 * authoritative final text added (or the whole reply after tool activity);
 * `discard` fires when a turn aborts or errors so nothing half-true is
 * spoken.
 */
export interface SpokenSettleInfo {
  /** True when the turn ended after tools without a real final answer. */
  toolOnly: boolean;
  /** The authoritative settled text, spoken or not. */
  fullText: string;
}

export interface SpokenTurnSink {
  discard(): void;
  sentence(text: string): void;
  settle(unspokenTail: string, info: SpokenSettleInfo): void;
  restart(): void;
}

export interface ContextUsage {
  contextPercent: number;
  contextTokens: number;
  contextWindow: number;
}

export interface LiveChatTurnDependencies {
  conversationId: Accessor<string | undefined>;
  durablePendingCount?: Accessor<number>;
  durableRunningTurnId?: Accessor<string | undefined>;
  focusTurnId?: Accessor<string | undefined>;
  history: LiveChatHistory;
  now?: () => number;
  /** Current composer toggle value, read once when a prompt is enqueued. */
  replyMode?: Accessor<ReplyMode>;
  /** Speech receiver for captured-spoken turns. */
  spokenTurn?: SpokenTurnSink;
  transport: LiveChatTransport;
}

export function createLiveChatTurn(dependencies: LiveChatTurnDependencies) {
  const now = dependencies.now ?? Date.now;
  const [turn, setTurn] = createSignal(emptyTurn());
  const [queuedPrompts, setQueuedPrompts] = createSignal<QueuedPrompt[]>([]);
  const visibleQueuedPrompts = createMemo(() =>
    queuedPrompts().map(({ content, id, replyMode, retryable, turnId }) => ({
      content,
      id,
      replyMode,
      retryable,
      turnId,
    })),
  );
  const [, setOutboundPrompt] = createSignal<QueuedPrompt | null>(null);
  const [awaitingAgentEnd, setAwaitingAgentEnd] = createSignal(false);
  const [error, setError] = createSignal<string>();
  const [interrupted, setInterrupted] = createSignal(false);
  const [loadedSkillCount, setLoadedSkillCount] = createSignal<number>();
  const [contextUsage, setContextUsage] = createSignal<ContextUsage>();
  const [accumulated, setAccumulated] = createSignal<Map<number, Message>>(
    new Map(),
  );
  const [hasMoreHistory, setHasMoreHistory] = createSignal(false);
  const [historyReady, setHistoryReady] = createSignal(false);
  const [loadingOlder, setLoadingOlder] = createSignal(false);
  const [focusedMessageId, setFocusedMessageId] = createSignal<string>();
  const [focusedTurn, setFocusedTurn] = createSignal<ConversationTurn>();
  const [focusedTurnError, setFocusedTurnError] = createSignal<
    "load" | "not_found"
  >();
  const [latestBeforeSeq, setLatestBeforeSeq] = createSignal<number>();
  const [readThroughSeq, setReadThroughSeq] = createSignal(0);
  const [durableStartedAt, setDurableStartedAt] = createSignal<number | null>(
    null,
  );
  const [durableTurnsReady, setDurableTurnsReady] = createSignal(false);
  const [hydratedRunningTurnId, setHydratedRunningTurnId] =
    createSignal<string>();
  const [activeTurnConversationId, setActiveTurnConversationId] =
    createSignal<string>();
  const [settledTurnIds, setSettledTurnIds] = createSignal<Set<string>>(
    new Set(),
  );
  const awaitingTickets: QueuedPrompt[] = [];
  const cancelAfterTicket = new Set<string>();
  let activeTurnId: string | undefined;
  let hydratedRunningConversationId: string | undefined;
  let runningPrompt: QueuedPrompt | null = null;
  let historyRequest = 0;
  let nextQueuedPromptId = 1;
  let runningReplyMode: ReplyMode = "text";
  // Per-running-turn speech stream (#545): null unless this prompt was
  // captured as spoken and a sink was provided.
  let spokenStream: ReturnType<typeof createSpokenStream> | null = null;
  // Parallel or back-to-back tool calls form one audible working phase. New
  // assistant prose ends that phase and allows a later tool phase to cue.
  let toolPhaseActive = false;

  const durablePendingCount = createMemo(() =>
    durableTurnsReady()
      ? queuedPrompts().filter((prompt) => prompt.turnId !== undefined).length
      : (dependencies.durablePendingCount?.() ?? 0),
  );
  const durableRunningTurnId = createMemo(() => {
    const currentConversationId = dependencies.conversationId();
    const hydrated =
      hydratedRunningConversationId === currentConversationId
        ? hydratedRunningTurnId()
        : undefined;
    const runningId = hydrated ?? dependencies.durableRunningTurnId?.();
    return runningId !== undefined && !settledTurnIds().has(runningId)
      ? runningId
      : undefined;
  });
  const busy = createMemo(
    () =>
      turn().generating ||
      awaitingAgentEnd() ||
      queuedPrompts().length > 0 ||
      durablePendingCount() > 0 ||
      durableRunningTurnId() !== undefined,
  );
  const generating = createMemo(
    () =>
      turn().generating ||
      queuedPrompts().length > 0 ||
      durablePendingCount() > 0 ||
      durableRunningTurnId() !== undefined,
  );
  const startedAt = createMemo(() => turn().startedAt ?? durableStartedAt());
  const stopped = createMemo(() => turn().stopped || interrupted());
  const working = createMemo(
    () =>
      isAwaitingFirstToken(turn()) ||
      (!turn().generating && durableRunningTurnId() !== undefined),
  );
  const historyIncomplete = createMemo(() => {
    const messages = Array.from(accumulated().values());
    if (messages.length === 0) {
      return false;
    }
    const latest = messages.reduce((left, right) =>
      left.seq > right.seq ? left : right,
    );
    return latest.role === "reasoning" || latest.role === "tool";
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
        turn: message.turn,
        turnId: message.turn_id,
      })),
  );
  const rows = createMemo<TimelineRow[]>(
    (previous) =>
      stabilizeRows(
        previous,
        deriveRows(
          storedMessages(),
          activeTurnConversationId() === dependencies.conversationId()
            ? turn()
            : emptyTurn(),
          activeTurnConversationId() === dependencies.conversationId()
            ? activeTurnId
            : undefined,
        ),
      ),
    [],
  );

  const loadLatestHistory = (conversationId: string) => {
    const request = historyRequest + 1;
    const focusTurnId = dependencies.focusTurnId?.();
    historyRequest = request;
    const latest = dependencies.history.listMessages(conversationId, {
      limit: MESSAGES_PAGE_SIZE,
    });
    const focused =
      focusTurnId === undefined
        ? Promise.resolve<Message[]>([])
        : dependencies.history.listMessages(conversationId, {
            turnId: focusTurnId,
          });
    const shouldLoadDurableTurns =
      (dependencies.durablePendingCount?.() ?? 0) > 0 ||
      dependencies.durableRunningTurnId?.() !== undefined;
    const nonterminal =
      shouldLoadDurableTurns &&
      dependencies.history.listNonterminalTurns !== undefined
        ? dependencies.history
            .listNonterminalTurns(conversationId)
            .catch(() => [])
        : Promise.resolve<ConversationTurn[]>([]);
    const detail =
      focusTurnId === undefined || dependencies.history.fetchTurn === undefined
        ? Promise.resolve<ConversationTurn | undefined>(undefined)
        : dependencies.history
            .fetchTurn(conversationId, focusTurnId)
            .then((value) => value)
            .catch((caught: unknown) => {
              if (
                typeof caught === "object" &&
                caught !== null &&
                "status" in caught &&
                caught.status === 404
              ) {
                setFocusedTurnError("not_found");
              } else {
                setFocusedTurnError("load");
              }
              return undefined;
            });
    void Promise.all([latest, focused, nonterminal, detail])
      .then(([page, focusedPage, durableTurns, turnDetail]) => {
        if (
          request !== historyRequest ||
          dependencies.conversationId() !== conversationId
        ) {
          return;
        }
        setAccumulated((current) => {
          const merged = new Map(current);
          let changed = false;
          for (const message of [...page, ...focusedPage]) {
            if (merged.get(message.seq) !== message) {
              merged.set(message.seq, message);
              changed = true;
            }
          }
          return changed ? merged : current;
        });
        setFocusedMessageId(focusedPage.at(0)?.id);
        setFocusedTurn(turnDetail);
        if (turnDetail !== undefined) {
          setFocusedTurnError(undefined);
        }
        const pendingTurns = durableTurns.filter(
          (durableTurn) =>
            durableTurn.status === "pending" &&
            !settledTurnIds().has(durableTurn.id),
        );
        setHydratedRunningTurnId(
          durableTurns.find((durableTurn) => durableTurn.status === "running")
            ?.id,
        );
        hydratedRunningConversationId = conversationId;
        setQueuedPrompts((current) => {
          const pendingById = new Map(
            pendingTurns.map((durableTurn) => [durableTurn.id, durableTurn]),
          );
          const pendingByRequest = new Map(
            pendingTurns.flatMap((durableTurn) =>
              durableTurn.request_id === null
                ? []
                : [[durableTurn.request_id, durableTurn] as const],
            ),
          );
          const seen = new Set<string>();
          const retained = current.flatMap((prompt) => {
            const durableTurn =
              (prompt.turnId === undefined
                ? undefined
                : pendingById.get(prompt.turnId)) ??
              pendingByRequest.get(prompt.requestId);
            if (durableTurn === undefined) {
              return settledTurnIds().has(prompt.turnId ?? "") ? [] : [prompt];
            }
            seen.add(durableTurn.id);
            return [{ ...prompt, turnId: durableTurn.id }];
          });
          const hydrated = pendingTurns
            .filter((durableTurn) => !seen.has(durableTurn.id))
            .map((durableTurn) => {
              const prompt: QueuedPrompt = {
                content: durableTurn.prompt,
                conversationId,
                id: nextQueuedPromptId,
                replyMode: durableTurn.reply_mode,
                requestId:
                  durableTurn.request_id ?? `durable:${durableTurn.id}`,
                turnId: durableTurn.id,
              };
              nextQueuedPromptId += 1;
              return prompt;
            });
          return [...retained, ...hydrated];
        });
        setDurableTurnsReady(
          dependencies.history.listNonterminalTurns !== undefined &&
            shouldLoadDurableTurns,
        );
        setLatestBeforeSeq(
          page.length === 0
            ? undefined
            : Math.min(...page.map((message) => message.seq)),
        );
        setReadThroughSeq(
          focusTurnId === undefined
            ? Math.max(0, ...page.map((message) => message.seq))
            : Math.max(0, ...focusedPage.map((message) => message.seq)),
        );
        setHasMoreHistory(page.length === MESSAGES_PAGE_SIZE);
        if (!turn().generating) {
          setTurn(emptyTurn());
        }
        setHistoryReady(true);
      })
      .catch(() => {
        if (request === historyRequest) {
          setHistoryReady(true);
          setDurableTurnsReady(false);
        }
      });
  };

  createEffect((previousId: string | undefined) => {
    const conversationId = dependencies.conversationId();
    if (conversationId !== previousId) {
      historyRequest += 1;
      setAccumulated(new Map());
      setHasMoreHistory(false);
      setHistoryReady(false);
      setLoadedSkillCount(undefined);
      setContextUsage(undefined);
      setFocusedMessageId(undefined);
      setFocusedTurn(undefined);
      setFocusedTurnError(undefined);
      setLatestBeforeSeq(undefined);
      setReadThroughSeq(0);
      setQueuedPrompts([]);
      setDurableTurnsReady(false);
      setHydratedRunningTurnId(undefined);
      hydratedRunningConversationId = undefined;
      setSettledTurnIds(new Set<string>());
      awaitingTickets.splice(0);
      cancelAfterTicket.clear();
      setOutboundPrompt(null);
      setAwaitingAgentEnd(false);
      setError(undefined);
      setInterrupted(false);
      setTurn(emptyTurn());
      activeTurnId = undefined;
      setActiveTurnConversationId(undefined);
      runningPrompt = null;
      runningReplyMode = "text";
      spokenStream = null;
      toolPhaseActive = false;
      if (conversationId !== undefined) {
        loadLatestHistory(conversationId);
      }
    }
    return conversationId;
  }, undefined);

  createEffect((previousRunningId: string | undefined) => {
    const runningId = durableRunningTurnId();
    if (runningId !== previousRunningId) {
      setDurableStartedAt(runningId === undefined ? null : now());
    }
    return runningId;
  }, undefined);

  createEffect((previousTurnId: string | undefined) => {
    const focusTurnId = dependencies.focusTurnId?.();
    const conversationId = dependencies.conversationId();
    if (
      focusTurnId !== undefined &&
      focusTurnId !== previousTurnId &&
      conversationId !== undefined
    ) {
      loadLatestHistory(conversationId);
    }
    return focusTurnId;
  }, undefined);

  const loadOlderMessages = (): boolean => {
    const conversationId = dependencies.conversationId();
    if (conversationId === undefined || loadingOlder() || !hasMoreHistory()) {
      return false;
    }
    const beforeSeq = latestBeforeSeq();
    if (beforeSeq === undefined) {
      return false;
    }
    setLoadingOlder(true);
    void dependencies.history
      .listMessages(conversationId, {
        beforeSeq,
        limit: MESSAGES_PAGE_SIZE,
      })
      .then((page) => {
        if (dependencies.conversationId() !== conversationId) {
          return;
        }
        setAccumulated((current) => {
          const merged = new Map(current);
          for (const message of page) {
            merged.set(message.seq, message);
          }
          return merged;
        });
        if (page.length > 0) {
          setLatestBeforeSeq(Math.min(...page.map((message) => message.seq)));
        }
        setHasMoreHistory(page.length === MESSAGES_PAGE_SIZE);
      })
      .catch(() => undefined)
      .finally(() => {
        setLoadingOlder(false);
      });
    return true;
  };

  const loadAllMessages = async (): Promise<void> => {
    const conversationId = dependencies.conversationId();
    if (conversationId === undefined || loadingOlder() || !hasMoreHistory()) {
      return;
    }
    const initialBeforeSeq = latestBeforeSeq();
    if (initialBeforeSeq === undefined) {
      return;
    }
    setLoadingOlder(true);
    let beforeSeq = initialBeforeSeq;
    try {
      while (dependencies.conversationId() === conversationId) {
        const page = await dependencies.history.listMessages(conversationId, {
          beforeSeq,
          limit: MESSAGES_PAGE_SIZE,
        });
        setAccumulated((current) => {
          const merged = new Map(current);
          for (const message of page) {
            merged.set(message.seq, message);
          }
          return merged;
        });
        if (page.length < MESSAGES_PAGE_SIZE) {
          setHasMoreHistory(false);
          return;
        }
        beforeSeq = Math.min(...page.map((message) => message.seq));
        setLatestBeforeSeq(beforeSeq);
      }
    } catch {
      return;
    } finally {
      setLoadingOlder(false);
    }
  };

  const beginPrompt = (prompt: QueuedPrompt) => {
    setAwaitingAgentEnd(false);
    setInterrupted(false);
    setOutboundPrompt(prompt);
    setActiveTurnConversationId(prompt.conversationId);
    setTurn(startTurn(prompt.content, now()));
    runningPrompt = prompt;
    runningReplyMode = prompt.replyMode;
    toolPhaseActive = false;
    spokenStream =
      runningReplyMode === "spoken" && dependencies.spokenTurn !== undefined
        ? createSpokenStream(
            (sentence) => dependencies.spokenTurn?.sentence(sentence),
            () => dependencies.spokenTurn?.restart(),
          )
        : null;
  };

  const submitPrompt = (prompt: QueuedPrompt) => {
    awaitingTickets.push(prompt);
    dependencies.transport.sendPrompt(
      prompt.conversationId,
      prompt.content,
      prompt.replyMode,
      prompt.requestId,
    );
  };

  const sendPrompt = (untrimmedContent: string) => {
    const content = untrimmedContent.trim();
    const conversationId = dependencies.conversationId();
    if (content.length === 0 || conversationId === undefined) {
      return;
    }
    setError(undefined);
    const prompt: QueuedPrompt = {
      content,
      conversationId,
      id: nextQueuedPromptId,
      replyMode: dependencies.replyMode?.() ?? "text",
      requestId: crypto.randomUUID(),
    };
    nextQueuedPromptId += 1;
    setQueuedPrompts((current) => [...current, prompt]);
    submitPrompt(prompt);
  };

  const editQueuedPrompt = (promptId: number, untrimmedContent: string) => {
    const content = untrimmedContent.trim();
    const conversationId = dependencies.conversationId();
    const current = queuedPrompts().find((prompt) => prompt.id === promptId);
    if (
      content.length === 0 ||
      conversationId === undefined ||
      current?.turnId === undefined
    ) {
      return;
    }
    dependencies.transport.abort(conversationId, current.turnId);
    const replacement: QueuedPrompt = {
      ...current,
      content,
      requestId: crypto.randomUUID(),
      turnId: undefined,
    };
    setQueuedPrompts((prompts) =>
      prompts.map((prompt) => (prompt.id === promptId ? replacement : prompt)),
    );
    submitPrompt(replacement);
  };

  const cancelQueuedPrompt = (promptId: number) => {
    const conversationId = dependencies.conversationId();
    const prompt = queuedPrompts().find(
      (candidate) => candidate.id === promptId,
    );
    if (conversationId !== undefined && prompt !== undefined) {
      if (prompt.turnId === undefined) {
        cancelAfterTicket.add(prompt.requestId);
      } else {
        dependencies.transport.abort(conversationId, prompt.turnId);
      }
    }
    setQueuedPrompts((current) =>
      current.filter((candidate) => candidate.id !== promptId),
    );
  };

  const sendQueuedPromptNow = (promptId: number) => {
    const conversationId = dependencies.conversationId();
    if (conversationId === undefined || awaitingAgentEnd()) {
      return;
    }
    const selected = queuedPrompts().find((prompt) => prompt.id === promptId);
    const reordered =
      selected === undefined
        ? queuedPrompts()
        : [
            selected,
            ...queuedPrompts().filter((prompt) => prompt.id !== promptId),
          ];
    if (reordered.every((prompt) => prompt.turnId !== undefined)) {
      const replacements = reordered.map((prompt) => {
        dependencies.transport.abort(conversationId, prompt.turnId ?? "");
        return {
          ...prompt,
          requestId: crypto.randomUUID(),
          turnId: undefined,
        };
      });
      setQueuedPrompts(replacements);
      for (const prompt of replacements) {
        submitPrompt(prompt);
      }
    } else {
      setQueuedPrompts(reordered);
      if (
        selected !== undefined &&
        selected.turnId === undefined &&
        !awaitingTickets.some(
          (prompt) => prompt.requestId === selected.requestId,
        )
      ) {
        const retry = { ...selected, retryable: false };
        setQueuedPrompts((current) =>
          current.map((prompt) => (prompt.id === retry.id ? retry : prompt)),
        );
        submitPrompt(retry);
      }
    }
    const runningTurnId =
      (runningPrompt?.conversationId === conversationId
        ? activeTurnId
        : undefined) ?? durableRunningTurnId();
    if (runningTurnId !== undefined) {
      setAwaitingAgentEnd(true);
      dependencies.transport.abort(conversationId, runningTurnId);
    }
  };

  const refreshSettledHistory = () => {
    dependencies.history.settled();
    const conversationId = dependencies.conversationId();
    if (conversationId !== undefined) {
      loadLatestHistory(conversationId);
    }
  };

  const handleFrame = (frame: ChatFrame) => {
    if (frame.type === "connection") {
      if (frame.status === "open") {
        refreshSettledHistory();
        const conversationId = dependencies.conversationId();
        if (conversationId !== undefined) {
          awaitingTickets.splice(0);
          const reconnecting = [
            ...(runningPrompt === null ? [] : [runningPrompt]),
            ...queuedPrompts(),
          ].filter((prompt) => prompt.conversationId === conversationId);
          const seenRequests = new Set<string>();
          for (const prompt of reconnecting) {
            if (!seenRequests.has(prompt.requestId)) {
              seenRequests.add(prompt.requestId);
              submitPrompt(prompt);
            }
          }
        }
      }
      return;
    }
    if (frame.type === "invalidate") {
      const conversationId = dependencies.conversationId();
      if (frame.keys.includes("messages") && conversationId !== undefined) {
        loadLatestHistory(conversationId);
      }
      return;
    }
    if (frame.type !== "chat") {
      return;
    }
    const conversationId = dependencies.conversationId();
    if (
      frame.conversation_id !== undefined &&
      conversationId !== undefined &&
      frame.conversation_id !== conversationId
    ) {
      return;
    }
    if (
      frame.event === "skill_status" &&
      typeof frame.loaded_count === "number" &&
      Number.isInteger(frame.loaded_count) &&
      frame.loaded_count >= 0
    ) {
      setLoadedSkillCount(frame.loaded_count);
      return;
    }
    if (frame.event === "session_status") {
      if (
        typeof frame.context_tokens === "number" &&
        Number.isInteger(frame.context_tokens) &&
        frame.context_tokens >= 0 &&
        typeof frame.context_window === "number" &&
        Number.isInteger(frame.context_window) &&
        frame.context_window > 0 &&
        typeof frame.context_percent === "number" &&
        Number.isFinite(frame.context_percent) &&
        frame.context_percent >= 0
      ) {
        setContextUsage({
          contextPercent: frame.context_percent,
          contextTokens: frame.context_tokens,
          contextWindow: frame.context_window,
        });
      } else {
        setContextUsage(undefined);
      }
      return;
    }
    if (frame.event === "turn_queued" && frame.turn_id !== undefined) {
      const accepted = awaitingTickets.shift();
      if (accepted !== undefined) {
        accepted.turnId = frame.turn_id;
        if (cancelAfterTicket.delete(accepted.requestId)) {
          dependencies.transport.abort(accepted.conversationId, frame.turn_id);
        }
        if (runningPrompt?.requestId === accepted.requestId) {
          runningPrompt = accepted;
        }
        setQueuedPrompts((current) =>
          current.map((prompt) =>
            prompt.requestId === accepted.requestId
              ? { ...prompt, turnId: frame.turn_id }
              : prompt,
          ),
        );
        activeTurnId = frame.turn_id;
      } else if (
        queuedPrompts().some((prompt) => prompt.turnId === frame.turn_id) ||
        runningPrompt?.turnId === frame.turn_id
      ) {
        activeTurnId = frame.turn_id;
      }
      if (frame.status === "running") {
        setHydratedRunningTurnId(frame.turn_id);
        hydratedRunningConversationId = conversationId;
      }
      return;
    }
    if (frame.event === "user_message" && frame.turn_id !== undefined) {
      let queued = queuedPrompts().find(
        (prompt) => prompt.turnId === frame.turn_id,
      );
      if (queued === undefined) {
        const accepted = awaitingTickets.shift();
        if (accepted !== undefined) {
          accepted.turnId = frame.turn_id;
          queued = accepted;
        }
      }
      if (queued !== undefined) {
        setQueuedPrompts((current) =>
          current.filter((prompt) => prompt.requestId !== queued.requestId),
        );
        beginPrompt(queued);
      }
      activeTurnId = frame.turn_id;
      setHydratedRunningTurnId(frame.turn_id);
      hydratedRunningConversationId = conversationId;
      setOutboundPrompt(null);
    } else if (
      frame.turn_id === undefined &&
      runningPrompt === null &&
      awaitingTickets.length > 0 &&
      frame.event !== "error" &&
      frame.event !== "abort_ack" &&
      frame.event !== "turn_ended"
    ) {
      // Older hosts and test transports may begin streaming before the durable
      // ticket frames arrive. Keep that wire behavior usable without treating
      // the provisional prompt as canonical transcript history.
      const accepted = awaitingTickets.shift();
      if (accepted !== undefined) {
        setQueuedPrompts((current) =>
          current.filter((prompt) => prompt.requestId !== accepted.requestId),
        );
        beginPrompt(accepted);
        setOutboundPrompt(null);
      }
    }
    const matchesKnownTurn =
      frame.turn_id === undefined ||
      frame.turn_id === activeTurnId ||
      frame.turn_id === runningPrompt?.turnId ||
      queuedPrompts().some((prompt) => prompt.turnId === frame.turn_id);
    if (frame.event === "turn_ended" && !matchesKnownTurn) {
      refreshSettledHistory();
      return;
    }
    setTurn((current) => reduceFrame(current, frame, now()));
    if (frame.event === "abort_ack") {
      setInterrupted(true);
      spokenStream = null;
      dependencies.spokenTurn?.discard();
      if (runningPrompt === null) {
        const rejectedPrompt = awaitingTickets.shift();
        if (rejectedPrompt !== undefined) {
          cancelAfterTicket.delete(rejectedPrompt.requestId);
          setQueuedPrompts((current) =>
            current.filter(
              (prompt) => prompt.requestId !== rejectedPrompt.requestId,
            ),
          );
        }
      }
    }
    if (frame.event === "error") {
      setError(frame.detail ?? "Chat error");
      spokenStream = null;
      dependencies.spokenTurn?.discard();
      if (frame.turn_id === undefined) {
        const rejectedPrompt = awaitingTickets.shift();
        if (rejectedPrompt !== undefined) {
          setQueuedPrompts((current) =>
            current.map((prompt) =>
              prompt.requestId === rejectedPrompt.requestId
                ? { ...prompt, retryable: true }
                : prompt,
            ),
          );
          if (runningPrompt?.requestId === rejectedPrompt.requestId) {
            runningPrompt = null;
            setTurn(emptyTurn());
          }
        }
        setOutboundPrompt(null);
        setAwaitingAgentEnd(false);
      }
      refreshSettledHistory();
    }
    // Provisional speech (#545): stream complete sentences as they arrive.
    // Only for captured-spoken turns; tool activity restarts the stream so
    // the settled answer plays whole once the context switches back.
    if (spokenStream !== null) {
      if (frame.event === "text_delta") {
        toolPhaseActive = false;
        spokenStream.push(deltaText(frame.delta));
      } else if (frame.event === "tool_start") {
        if (!toolPhaseActive) {
          spokenStream.restart();
          toolPhaseActive = true;
        }
      }
    }
    if (frame.event === "turn_ended") {
      if (frame.status === "failed") {
        setError(frame.failure_summary ?? "Chat turn failed");
        dependencies.spokenTurn?.discard();
      } else if (frame.status === "cancelled") {
        setInterrupted(true);
        dependencies.spokenTurn?.discard();
      }
      if (frame.turn_id !== undefined) {
        setSettledTurnIds((current) =>
          new Set(current).add(frame.turn_id ?? ""),
        );
        setQueuedPrompts((current) =>
          current.filter((prompt) => prompt.turnId !== frame.turn_id),
        );
      }
      spokenStream = null;
      toolPhaseActive = false;
      if (frame.turn_id === activeTurnId) {
        activeTurnId = undefined;
      }
      if (
        frame.turn_id === undefined ||
        frame.turn_id === runningPrompt?.turnId
      ) {
        runningPrompt = null;
      }
      if (frame.turn_id === hydratedRunningTurnId()) {
        setHydratedRunningTurnId(undefined);
        hydratedRunningConversationId = undefined;
      }
      setOutboundPrompt(null);
      setAwaitingAgentEnd(false);
      refreshSettledHistory();
    }
    if (frame.event === "agent_end") {
      const settledReplyMode = runningReplyMode;
      const stream = spokenStream;
      spokenStream = null;
      toolPhaseActive = false;
      refreshSettledHistory();
      // Playback corresponds to the prompt's captured mode, not the toggle's
      // current value, and never fires for aborted or errored turns.
      if (
        settledReplyMode === "spoken" &&
        !stopped() &&
        error() === undefined
      ) {
        const fullText =
          typeof frame.final_text === "string" ? frame.final_text : "";
        if (frame.tool_only === true) {
          // The final text is a host-side marker, not real prose — flag it so
          // the sink can decide what (if anything) a listener should hear.
          dependencies.spokenTurn?.settle("", {
            fullText,
            toolOnly: true,
          });
        } else if (fullText.length > 0) {
          dependencies.spokenTurn?.settle(
            stream === null ? fullText : stream.tail(fullText),
            { fullText, toolOnly: false },
          );
        }
      }
    }
  };

  const abort = () => {
    const conversationId = dependencies.conversationId();
    const ownedActiveTurnId =
      runningPrompt?.conversationId === conversationId
        ? activeTurnId
        : undefined;
    const turnId = ownedActiveTurnId ?? durableRunningTurnId();
    if (
      conversationId !== undefined &&
      turnId !== undefined &&
      !awaitingAgentEnd()
    ) {
      setAwaitingAgentEnd(true);
      dependencies.transport.abort(conversationId, turnId);
    }
  };

  return {
    abort,
    awaitingAgentEnd,
    busy,
    cancelQueuedPrompt,
    clearContextUsage: () => {
      setContextUsage(undefined);
    },
    contextUsage,
    durablePendingCount,
    dismissError: () => {
      setError(undefined);
    },
    editQueuedPrompt,
    error,
    focusedMessageId,
    focusedTurn,
    focusedTurnError,
    generating,
    handleFrame,
    highestSettledSeq: readThroughSeq,
    historyIncomplete,
    historyReady,
    loadAllMessages,
    loadOlderMessages,
    loadedSkillCount,
    queuedPrompts: visibleQueuedPrompts,
    rows,
    sendPrompt,
    sendQueuedPromptNow,
    startedAt,
    stopped,
    working,
  };
}

export type { ChatRole, TimelineRow } from "./live-chat-turn-state";
