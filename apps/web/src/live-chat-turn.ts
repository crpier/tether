import { createEffect, createMemo, createSignal } from "solid-js";
import type { Accessor } from "solid-js";

import type { Message } from "./host/chat";
import type { ChatFrame } from "./chat-bus";
import {
  deriveRows,
  emptyTurn,
  isAwaitingFirstToken,
  reduceFrame,
  stabilizeRows,
  startTurn,
  type StoredMessage,
  type TimelineRow,
} from "./live-chat-turn-state";

const MESSAGES_PAGE_SIZE = 30;

export interface QueuedPrompt {
  id: number;
  content: string;
}

export interface LiveChatHistory {
  listMessages(
    conversationId: string,
    options: { beforeSeq?: number; limit: number },
  ): Promise<Message[]>;
  settled(): void;
}

export interface LiveChatTransport {
  abort(conversationId: string): void;
  sendPrompt(conversationId: string, content: string): void;
}

export interface LiveChatTurnDependencies {
  conversationId: Accessor<string | undefined>;
  history: LiveChatHistory;
  now?: () => number;
  transport: LiveChatTransport;
}

export function createLiveChatTurn(dependencies: LiveChatTurnDependencies) {
  const now = dependencies.now ?? Date.now;
  const [turn, setTurn] = createSignal(emptyTurn());
  const [queuedPrompts, setQueuedPrompts] = createSignal<QueuedPrompt[]>([]);
  const [outboundPrompt, setOutboundPrompt] = createSignal<QueuedPrompt | null>(
    null,
  );
  const [awaitingAgentEnd, setAwaitingAgentEnd] = createSignal(false);
  const [error, setError] = createSignal<string>();
  const [interrupted, setInterrupted] = createSignal(false);
  const [loadedSkillCount, setLoadedSkillCount] = createSignal<number>();
  const [accumulated, setAccumulated] = createSignal<Map<number, Message>>(
    new Map(),
  );
  const [hasMoreHistory, setHasMoreHistory] = createSignal(false);
  const [historyReady, setHistoryReady] = createSignal(false);
  const [loadingOlder, setLoadingOlder] = createSignal(false);
  let historyRequest = 0;
  let nextQueuedPromptId = 1;

  const busy = createMemo(() => turn().generating || awaitingAgentEnd());
  const generating = createMemo(() => turn().generating);
  const startedAt = createMemo(() => turn().startedAt);
  const stopped = createMemo(() => turn().stopped || interrupted());
  const working = createMemo(() => isAwaitingFirstToken(turn()));
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

  const loadLatestHistory = (conversationId: string) => {
    const request = historyRequest + 1;
    historyRequest = request;
    void dependencies.history
      .listMessages(conversationId, { limit: MESSAGES_PAGE_SIZE })
      .then((page) => {
        if (
          request !== historyRequest ||
          dependencies.conversationId() !== conversationId
        ) {
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
        if (!turn().generating) {
          setTurn(emptyTurn());
        }
        setHistoryReady(true);
      })
      .catch(() => {
        if (request === historyRequest) {
          setHistoryReady(true);
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
      if (conversationId !== undefined) {
        loadLatestHistory(conversationId);
      }
    }
    return conversationId;
  }, undefined);

  const loadOlderMessages = (): boolean => {
    const conversationId = dependencies.conversationId();
    if (conversationId === undefined || loadingOlder() || !hasMoreHistory()) {
      return false;
    }
    const sequenceNumbers = Array.from(accumulated().keys());
    if (sequenceNumbers.length === 0) {
      return false;
    }
    const oldestSequence = Math.min(...sequenceNumbers);
    setLoadingOlder(true);
    void dependencies.history
      .listMessages(conversationId, {
        beforeSeq: oldestSequence,
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
        setHasMoreHistory(page.length === MESSAGES_PAGE_SIZE);
      })
      .catch(() => undefined)
      .finally(() => {
        setLoadingOlder(false);
      });
    return true;
  };

  const dispatchPrompt = (prompt: QueuedPrompt, conversationId: string) => {
    setAwaitingAgentEnd(false);
    setInterrupted(false);
    setOutboundPrompt(prompt);
    setTurn(startTurn(prompt.content, now()));
    dependencies.transport.sendPrompt(conversationId, prompt.content);
  };

  const dispatchNextQueuedPrompt = () => {
    const conversationId = dependencies.conversationId();
    const next = queuedPrompts().at(0);
    if (conversationId === undefined || next === undefined) {
      return;
    }
    setQueuedPrompts((current) => current.slice(1));
    dispatchPrompt(next, conversationId);
  };

  const sendPrompt = (untrimmedContent: string) => {
    const content = untrimmedContent.trim();
    const conversationId = dependencies.conversationId();
    if (content.length === 0 || conversationId === undefined) {
      return;
    }
    setError(undefined);
    const prompt = { content, id: nextQueuedPromptId };
    nextQueuedPromptId += 1;
    if (busy()) {
      setQueuedPrompts((current) => [...current, prompt]);
      return;
    }
    dispatchPrompt(prompt, conversationId);
  };

  const editQueuedPrompt = (promptId: number, untrimmedContent: string) => {
    const content = untrimmedContent.trim();
    if (content.length === 0) {
      return;
    }
    setQueuedPrompts((current) =>
      current.map((prompt) =>
        prompt.id === promptId ? { ...prompt, content } : prompt,
      ),
    );
  };

  const cancelQueuedPrompt = (promptId: number) => {
    setQueuedPrompts((current) =>
      current.filter((prompt) => prompt.id !== promptId),
    );
  };

  const sendQueuedPromptNow = (promptId: number) => {
    const conversationId = dependencies.conversationId();
    if (conversationId === undefined || awaitingAgentEnd()) {
      return;
    }
    setQueuedPrompts((current) => {
      const selected = current.find((prompt) => prompt.id === promptId);
      return selected === undefined
        ? current
        : [selected, ...current.filter((prompt) => prompt.id !== promptId)];
    });
    if (!busy()) {
      dispatchNextQueuedPrompt();
      return;
    }
    setAwaitingAgentEnd(true);
    dependencies.transport.abort(conversationId);
  };

  const refreshSettledHistory = () => {
    dependencies.history.settled();
    const conversationId = dependencies.conversationId();
    if (conversationId !== undefined) {
      loadLatestHistory(conversationId);
    }
  };

  const handleFrame = (frame: ChatFrame) => {
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
    setTurn((current) => reduceFrame(current, frame, now()));
    if (frame.event === "user_message") {
      setOutboundPrompt(null);
    }
    if (frame.event === "abort_ack") {
      setInterrupted(true);
    }
    if (frame.event === "error") {
      setError(frame.detail ?? "Chat error");
      const rejected = outboundPrompt();
      if (rejected !== null) {
        setQueuedPrompts((current) => [rejected, ...current]);
        setOutboundPrompt(null);
      }
      refreshSettledHistory();
    }
    if (frame.event === "agent_end") {
      setOutboundPrompt(null);
      setAwaitingAgentEnd(false);
      refreshSettledHistory();
      dispatchNextQueuedPrompt();
    }
  };

  const abort = () => {
    const conversationId = dependencies.conversationId();
    if (conversationId !== undefined && !awaitingAgentEnd()) {
      setAwaitingAgentEnd(true);
      dependencies.transport.abort(conversationId);
    }
  };

  return {
    abort,
    awaitingAgentEnd,
    busy,
    cancelQueuedPrompt,
    dismissError: () => {
      setError(undefined);
    },
    editQueuedPrompt,
    error,
    generating,
    handleFrame,
    historyReady,
    loadOlderMessages,
    loadedSkillCount,
    queuedPrompts,
    rows,
    sendPrompt,
    sendQueuedPromptNow,
    startedAt,
    stopped,
    working,
  };
}

export type { ChatRole, TimelineRow } from "./live-chat-turn-state";
