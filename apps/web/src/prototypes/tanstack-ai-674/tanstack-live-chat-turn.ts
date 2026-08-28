// THROWAWAY PROTOTYPE. This file must never ship from main.
// It places TanStack AI behind createLiveChatTurn's caller-facing interface so
// the spike measures deleted behavior rather than a smaller, incomparable API.

import { EventType, type StreamChunk } from "@tanstack/ai";
import {
  ChatClient,
  type RunAgentInputContext,
  type SubscribeConnectionAdapter,
  type UIMessage,
} from "@tanstack/ai-client";
import { createMemo, createSignal, onCleanup } from "solid-js";

import type { Attachment, Message } from "../../host/chat";
import type { ChatFrame, ReplyMode } from "../../chat-bus";
import type { LiveChatTurnDependencies } from "../../live-chat-turn";
import type { TimelineRow } from "../../live-chat-turn-state";
import type { createLiveChatTurn } from "../../live-chat-turn";

export type TanStackLiveChatTurn = ReturnType<typeof createLiveChatTurn>;

interface PendingRead {
  resolve: (result: IteratorResult<StreamChunk>) => void;
}

class TetherFrameConnection implements SubscribeConnectionAdapter {
  private chunks: StreamChunk[] = [];
  private pendingReads: PendingRead[] = [];
  private closed = false;
  private runContext: RunAgentInputContext | undefined;
  private hostTurnId: string | undefined;
  private assistantMessageId: string | undefined;
  private reasoningMessageId: string | undefined;
  private textOpen = false;
  private reasoningOpen = false;
  private preforwardedSends = 0;

  constructor(
    private readonly sendPrompt: (
      content: string,
      replyMode: ReplyMode,
      requestId: string,
      attachmentIds: readonly string[],
    ) => void,
  ) {}

  markNextSendForwarded(): void {
    this.preforwardedSends += 1;
  }

  async *subscribe(abortSignal?: AbortSignal): AsyncIterable<StreamChunk> {
    while (!this.closed && !abortSignal?.aborted) {
      const chunk = await this.read(abortSignal);
      if (chunk === undefined) {
        return;
      }
      yield chunk;
    }
  }

  send(
    messages: Parameters<SubscribeConnectionAdapter["send"]>[0],
    data?: Record<string, unknown>,
    _abortSignal?: AbortSignal,
    runContext?: RunAgentInputContext,
  ): Promise<void> {
    const latest = messages.findLast((message) => message.role === "user");
    const content =
      latest !== undefined && "parts" in latest
        ? latest.parts
            .filter((part) => part.type === "text")
            .map((part) => part.content)
            .join("")
        : typeof latest?.content === "string"
          ? latest.content
          : "";
    const replyMode = data?.replyMode === "spoken" ? "spoken" : "text";
    const attachmentIds = Array.isArray(data?.attachmentIds)
      ? data.attachmentIds.filter(
          (attachmentId): attachmentId is string =>
            typeof attachmentId === "string",
        )
      : [];
    this.runContext = runContext;
    if (this.preforwardedSends > 0) {
      this.preforwardedSends -= 1;
      return Promise.resolve();
    }
    this.sendPrompt(
      content,
      replyMode,
      runContext?.runId ?? crypto.randomUUID(),
      attachmentIds,
    );
    return Promise.resolve();
  }

  feed(frame: ChatFrame): void {
    if (frame.type !== "chat") {
      return;
    }
    const threadId = this.runContext?.threadId ?? frame.conversation_id ?? "";
    const runId = this.runContext?.runId ?? frame.turn_id ?? "";
    if (frame.turn_id !== undefined) {
      this.hostTurnId = frame.turn_id;
    }
    switch (frame.event) {
      case "turn_queued":
        if (frame.status === "running") {
          this.push({
            type: EventType.RUN_STARTED,
            threadId,
            runId,
            timestamp: Date.now(),
            metadata: { tetherTurnId: frame.turn_id },
          });
        }
        return;
      case "message_start": {
        const suffix = frame.content_index ?? 0;
        this.assistantMessageId = `live:${frame.turn_id ?? runId}:${suffix.toString()}`;
        this.reasoningMessageId = `${this.assistantMessageId}:reasoning`;
        this.textOpen = true;
        this.push({
          type: EventType.TEXT_MESSAGE_START,
          messageId: this.assistantMessageId,
          role: "assistant",
          timestamp: Date.now(),
        });
        return;
      }
      case "thinking_delta":
        this.startReasoning();
        this.push({
          type: EventType.REASONING_MESSAGE_CONTENT,
          messageId: this.reasoningMessageId ?? this.messageId("reasoning"),
          delta: frameText(frame.delta),
          timestamp: Date.now(),
        });
        return;
      case "text_delta":
        this.push({
          type: EventType.TEXT_MESSAGE_CONTENT,
          messageId: this.assistantMessageId ?? this.messageId("assistant"),
          delta: frameText(frame.delta),
          timestamp: Date.now(),
        });
        return;
      case "tool_start": {
        const toolCallId = frame.tool_id ?? this.messageId("tool");
        this.push({
          type: EventType.TOOL_CALL_START,
          toolCallId,
          toolCallName: frame.tool_name ?? "tool",
          parentMessageId: this.assistantMessageId,
          timestamp: Date.now(),
        });
        this.push({
          type: EventType.TOOL_CALL_ARGS,
          toolCallId,
          delta: JSON.stringify(frame.tool_args ?? {}),
          timestamp: Date.now(),
        });
        this.push({
          type: EventType.TOOL_CALL_END,
          toolCallId,
          timestamp: Date.now(),
        });
        return;
      }
      case "tool_end": {
        const toolCallId = frame.tool_id ?? this.messageId("tool");
        this.push({
          type: EventType.TOOL_CALL_RESULT,
          messageId: `${toolCallId}:result`,
          toolCallId,
          role: "tool",
          content: JSON.stringify(frame.tool_result ?? null),
          timestamp: Date.now(),
        });
        return;
      }
      case "message_end":
        this.endOpenMessages();
        return;
      case "agent_end":
        this.endOpenMessages();
        this.push({
          type: EventType.RUN_FINISHED,
          threadId,
          runId,
          timestamp: Date.now(),
          finishReason: "stop",
          metadata: {
            tether: {
              finalText: frame.final_text,
              replyMode: frame.reply_mode,
              toolOnly: frame.tool_only,
            },
          },
        });
        return;
      case "turn_ended":
        if (frame.status === "failed" || frame.status === "cancelled") {
          this.endOpenMessages();
          this.push({
            type: EventType.RUN_ERROR,
            threadId,
            runId,
            message: frame.failure_summary ?? frame.status,
            code: frame.failure_code ?? frame.status,
            timestamp: Date.now(),
          });
        }
        return;
      case "error":
        this.push({
          type: EventType.RUN_ERROR,
          threadId,
          runId,
          message: frame.detail ?? "Chat failed",
          timestamp: Date.now(),
        });
        return;
      default:
        return;
    }
  }

  close(): void {
    this.closed = true;
    for (const pending of this.pendingReads.splice(0)) {
      pending.resolve({ done: true, value: undefined });
    }
  }

  private messageId(kind: string): string {
    return `live:${this.hostTurnId ?? this.runContext?.runId ?? "run"}:${kind}`;
  }

  private startReasoning(): void {
    if (this.reasoningOpen) {
      return;
    }
    this.reasoningOpen = true;
    this.push({
      type: EventType.REASONING_MESSAGE_START,
      messageId: this.reasoningMessageId ?? this.messageId("reasoning"),
      role: "reasoning",
      timestamp: Date.now(),
    });
  }

  private endOpenMessages(): void {
    if (this.reasoningOpen) {
      this.push({
        type: EventType.REASONING_MESSAGE_END,
        messageId: this.reasoningMessageId ?? this.messageId("reasoning"),
        timestamp: Date.now(),
      });
      this.reasoningOpen = false;
    }
    if (this.textOpen) {
      this.push({
        type: EventType.TEXT_MESSAGE_END,
        messageId: this.assistantMessageId ?? this.messageId("assistant"),
        timestamp: Date.now(),
      });
      this.textOpen = false;
    }
  }

  private push(chunk: StreamChunk): void {
    const pending = this.pendingReads.shift();
    if (pending !== undefined) {
      pending.resolve({ done: false, value: chunk });
      return;
    }
    this.chunks.push(chunk);
  }

  private read(abortSignal?: AbortSignal): Promise<StreamChunk | undefined> {
    const chunk = this.chunks.shift();
    if (chunk !== undefined) {
      return Promise.resolve(chunk);
    }
    if (this.closed || abortSignal?.aborted) {
      return Promise.resolve(undefined);
    }
    return new Promise((resolve) => {
      const pending: PendingRead = {
        resolve: (result) => {
          resolve(result.done ? undefined : result.value);
        },
      };
      this.pendingReads.push(pending);
      abortSignal?.addEventListener(
        "abort",
        () => {
          const index = this.pendingReads.indexOf(pending);
          if (index >= 0) {
            this.pendingReads.splice(index, 1);
            resolve(undefined);
          }
        },
        { once: true },
      );
    });
  }
}

function frameText(delta: unknown): string {
  if (typeof delta === "string") {
    return delta;
  }
  if (typeof delta === "object" && delta !== null && "text" in delta) {
    const text = (delta as { text?: unknown }).text;
    return typeof text === "string" ? text : "";
  }
  return "";
}

function storedMessagesToUi(messages: readonly Message[]): UIMessage[] {
  return messages.map((message) => {
    const metadata = {
      tether: {
        attachments: message.attachments,
        createdAt: message.created_at,
        role: message.role,
        seq: message.seq,
        turn: message.turn,
        turnId: message.turn_id,
      },
    };
    if (message.role === "reasoning") {
      return {
        id: message.id,
        role: "assistant" as const,
        parts: [{ type: "thinking" as const, content: message.content }],
        createdAt: new Date(message.created_at),
        metadata,
      };
    }
    if (message.role === "tool") {
      return {
        id: message.id,
        role: "assistant" as const,
        parts: [
          {
            type: "tool-call" as const,
            id: message.id,
            name: message.tool_name ?? "tool",
            arguments: JSON.stringify(message.tool_args ?? {}),
            input: message.tool_args ?? {},
            output: message.tool_result,
            state: "complete" as const,
          },
        ],
        createdAt: new Date(message.created_at),
        metadata,
      };
    }
    return {
      id: message.id,
      role:
        message.role === "assistant"
          ? ("assistant" as const)
          : ("user" as const),
      parts: [{ type: "text" as const, content: message.content }],
      createdAt: new Date(message.created_at),
      metadata,
    };
  });
}

function messageText(message: UIMessage): string {
  return message.parts
    .filter((part) => part.type === "text")
    .map((part) => part.content)
    .join("");
}

function timelineRows(messages: UIMessage[]): TimelineRow[] {
  const rows: TimelineRow[] = [];
  for (const message of messages) {
    const tether = message.metadata?.tether as
      | {
          attachments?: Attachment[];
          createdAt?: string;
          role?:
            | "user"
            | "health"
            | "scheduled"
            | "assistant"
            | "reasoning"
            | "tool";
          turn?: Message["turn"];
          turnId?: string | null;
        }
      | undefined;
    if (message.role === "user") {
      rows.push({
        attachments: tether?.attachments,
        createdAt: tether?.createdAt,
        kind: "message",
        id: message.id,
        role:
          tether?.role === "health" || tether?.role === "scheduled"
            ? tether.role
            : "user",
        text: messageText(message),
        toolName: null,
        streaming: false,
        turn: tether?.turn,
        turnId: tether?.turnId,
      });
      continue;
    }
    if (message.role !== "assistant") {
      continue;
    }
    for (const [index, part] of message.parts.entries()) {
      if (part.type === "thinking" && part.content.length > 0) {
        rows.push({
          kind: "reasoning",
          createdAt: tether?.createdAt,
          id:
            message.parts.length === 1
              ? message.id
              : `${message.id}:thinking:${index.toString()}`,
          text: part.content,
          streaming: false,
          done: tether?.role === "reasoning",
          turnId: tether?.turnId,
        });
      } else if (part.type === "tool-call") {
        rows.push({
          kind: "tool",
          id: part.id,
          toolName: part.name,
          status: part.state === "complete" ? "done" : "running",
          args: part.input ?? parseJson(part.arguments),
          result: part.output ?? null,
        });
      } else if (part.type === "text" && part.content.length > 0) {
        rows.push({
          kind: "message",
          createdAt: tether?.createdAt,
          id:
            message.parts.length === 1
              ? message.id
              : `${message.id}:text:${index.toString()}`,
          role: "assistant",
          text: part.content,
          toolName: null,
          streaming: false,
          turn: tether?.turn,
          turnId: tether?.turnId,
        });
      }
    }
  }
  return rows;
}

function parseJson(value: string): unknown {
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return value;
  }
}

export function createTanStackLiveChatTurn(
  dependencies: LiveChatTurnDependencies,
): TanStackLiveChatTurn {
  const [messages, setMessages] = createSignal<UIMessage[]>([]);
  const [error, setError] = createSignal<string>();
  const [contextUsage, setContextUsage] =
    createSignal<ReturnType<TanStackLiveChatTurn["contextUsage"]>>();
  const [loadedSkillCount, setLoadedSkillCount] = createSignal<number>();
  const [hostTurnId, setHostTurnId] = createSignal<string>();
  const [hydratedRunningTurnId, setHydratedRunningTurnId] =
    createSignal<string>();
  const [highestSettledSeq, setHighestSettledSeq] = createSignal(0);
  const [historyReady, setHistoryReady] = createSignal(false);
  const [hostQueue, setHostQueue] = createSignal<
    {
      attachments: Attachment[];
      content: string;
      id: number;
      replyMode: ReplyMode;
      requestId: string;
      retryable: boolean | undefined;
      turnId: string | undefined;
    }[]
  >([]);
  let nextHostQueueId = 1;
  let tanStackSubmissionActive = false;
  let settleHistory = () => undefined;
  const connection = new TetherFrameConnection(
    (content, replyMode, requestId, attachmentIds) => {
      const conversationId = dependencies.conversationId();
      if (conversationId !== undefined) {
        dependencies.transport.sendPrompt(
          conversationId,
          content,
          replyMode,
          requestId,
          attachmentIds,
        );
      }
    },
  );
  const client = new ChatClient({
    connection,
    queue: { whenBusy: "queue" },
    threadId: dependencies.conversationId(),
    onMessagesChange: setMessages,
    onErrorChange: (nextError) => {
      setError(nextError?.message);
      if (nextError !== undefined) {
        tanStackSubmissionActive = false;
      }
    },
    onFinish: () => {
      tanStackSubmissionActive = false;
      settleHistory();
    },
  });
  client.attach();
  let historyRequest = 0;
  const loadHistory = (replaceLive: boolean) => {
    const conversationId = dependencies.conversationId();
    if (conversationId === undefined) {
      setHistoryReady(true);
      return;
    }
    const request = ++historyRequest;
    const nonterminal =
      dependencies.history.listNonterminalTurns?.(conversationId) ??
      Promise.resolve([]);
    void Promise.all([
      dependencies.history.listMessages(conversationId, { limit: 30 }),
      nonterminal,
    ])
      .then(([stored, turns]) => {
        if (request !== historyRequest) {
          return;
        }
        if (replaceLive || client.getMessages().length === 0) {
          client.setMessagesManually(storedMessagesToUi(stored));
        }
        setHighestSettledSeq(
          stored.reduce(
            (highest, message) => Math.max(highest, message.seq),
            0,
          ),
        );
        setHydratedRunningTurnId(
          turns.find((turn) => turn.status === "running")?.id,
        );
        const pending = turns
          .filter((turn) => turn.status === "pending")
          .map((turn) => ({
            attachments: turn.attachments,
            content: turn.prompt,
            id: nextHostQueueId++,
            replyMode: turn.reply_mode,
            requestId: turn.request_id ?? `durable:${turn.id}`,
            retryable: undefined,
            turnId: turn.id,
          }));
        setHostQueue((current) => [
          ...pending,
          ...current.filter(
            (prompt) =>
              prompt.turnId === undefined &&
              !pending.some(
                (durable) => durable.requestId === prompt.requestId,
              ),
          ),
        ]);
        setHistoryReady(true);
      })
      .catch(() => {
        if (request === historyRequest) {
          setHistoryReady(true);
        }
      });
  };
  settleHistory = () => {
    dependencies.history.settled();
    loadHistory(true);
  };
  loadHistory(false);
  onCleanup(() => {
    client.dispose();
    connection.close();
  });

  const rows = createMemo(() => timelineRows(messages()));
  const queuedPrompts = createMemo(() => [
    ...hostQueue().map((prompt) => ({
      attachments: prompt.attachments,
      content: prompt.content,
      id: prompt.id,
      replyMode: prompt.replyMode,
      retryable: prompt.retryable,
      turnId: prompt.turnId,
    })),
    ...client.getQueue().map((queued, index) => ({
      attachments: [],
      content:
        typeof queued.content === "string"
          ? queued.content
          : typeof queued.content.content === "string"
            ? queued.content.content
            : queued.content.content
                .filter((part) => part.type === "text")
                .map((part) => part.content)
                .join(""),
      id: hostQueue().length + index + 1,
      replyMode: "text" as const,
      retryable: undefined,
      turnId: undefined,
    })),
  ]);
  const generating = createMemo(() => client.getIsLoading());
  const durablePendingCount = createMemo(
    () => dependencies.durablePendingCount?.() ?? queuedPrompts().length,
  );
  const durableRunningTurnId = createMemo(
    () => hydratedRunningTurnId() ?? dependencies.durableRunningTurnId?.(),
  );
  const busy = createMemo(
    () =>
      generating() ||
      durablePendingCount() > 0 ||
      durableRunningTurnId() !== undefined,
  );

  const handleFrame = (frame: ChatFrame) => {
    if (frame.type === "connection") {
      if (frame.status === "open") {
        loadHistory(false);
      }
      return;
    }
    if (frame.type === "invalidate") {
      if (frame.keys.includes("messages")) {
        loadHistory(true);
      }
      return;
    }
    if (frame.type !== "chat") {
      return;
    }
    if (frame.event === "user_message") {
      if (frame.turn_id !== undefined) {
        setHostQueue((current) =>
          current.filter((prompt) => prompt.turnId !== frame.turn_id),
        );
      }
      if (frame.message_id !== undefined) {
        const current = client.getMessages();
        const index = current.findLastIndex(
          (message) => message.role === "user",
        );
        if (index >= 0) {
          const remapped = [...current];
          remapped[index] = { ...remapped[index], id: frame.message_id };
          client.setMessagesManually(remapped);
        }
      }
      if (typeof frame.seq === "number") {
        setHighestSettledSeq(Math.max(highestSettledSeq(), frame.seq));
      }
    } else if (frame.event === "turn_queued" && frame.turn_id !== undefined) {
      setHostTurnId(frame.turn_id);
      if (frame.status === "running") {
        setHydratedRunningTurnId(frame.turn_id);
      }
      setHostQueue((current) => {
        const pendingIndex = current.findIndex(
          (prompt) => prompt.turnId === undefined,
        );
        if (pendingIndex < 0) {
          return current;
        }
        return current.map((prompt, index) =>
          index === pendingIndex
            ? { ...prompt, turnId: frame.turn_id }
            : prompt,
        );
      });
    } else if (frame.event === "session_status") {
      if (
        typeof frame.context_tokens === "number" &&
        typeof frame.context_window === "number" &&
        typeof frame.context_percent === "number"
      ) {
        setContextUsage({
          contextPercent: frame.context_percent,
          contextTokens: frame.context_tokens,
          contextWindow: frame.context_window,
        });
      } else {
        setContextUsage(undefined);
      }
    } else if (
      frame.event === "skill_status" &&
      typeof frame.loaded_count === "number"
    ) {
      setLoadedSkillCount(frame.loaded_count);
    }
    if (
      frame.event === "turn_ended" &&
      frame.turn_id === hydratedRunningTurnId()
    ) {
      setHydratedRunningTurnId(undefined);
    }
    if (frame.event === "agent_end" && frame.reply_mode === "spoken") {
      const fullText = frame.final_text ?? "";
      dependencies.spokenTurn?.settle(frame.tool_only ? "" : fullText, {
        fullText,
        toolOnly: frame.tool_only ?? false,
      });
    } else if (
      frame.event === "error" ||
      (frame.event === "turn_ended" && frame.status !== "succeeded")
    ) {
      dependencies.spokenTurn?.discard();
    }
    connection.feed(frame);
  };

  const sendPrompt = (content: string, attachments: Attachment[] = []) => {
    const replyMode = dependencies.replyMode?.() ?? "text";
    const conversationId = dependencies.conversationId();
    if (conversationId === undefined) {
      return;
    }
    const requestId = crypto.randomUUID();
    dependencies.transport.sendPrompt(
      conversationId,
      content,
      replyMode,
      requestId,
      attachments.map((attachment) => attachment.id),
    );
    if (tanStackSubmissionActive || client.getIsLoading()) {
      setHostQueue((current) => [
        ...current,
        {
          attachments,
          content,
          id: nextHostQueueId++,
          replyMode,
          requestId,
          retryable: undefined,
          turnId: undefined,
        },
      ]);
      return;
    }
    connection.markNextSendForwarded();
    tanStackSubmissionActive = true;
    void client.sendMessage(content, {
      body: {
        attachmentIds: attachments.map((attachment) => attachment.id),
        replyMode,
      },
    });
  };

  const result = {
    abort: () => {
      const conversationId = dependencies.conversationId();
      const turnId = hostTurnId() ?? durableRunningTurnId();
      if (conversationId !== undefined && turnId !== undefined) {
        dependencies.transport.abort(conversationId, turnId);
      }
      client.stop();
    },
    awaitingAgentEnd: createMemo(() => false),
    busy,
    cancelQueuedPrompt: (promptId: number) => {
      const queued = client.getQueue().at(promptId - 1);
      if (queued !== undefined) {
        client.cancelQueued(queued.id);
      }
    },
    clearContextUsage: () => {
      setContextUsage(undefined);
    },
    contextUsage,
    durablePendingCount,
    dismissError: () => {
      setError(undefined);
    },
    editQueuedPrompt: () => undefined,
    error,
    focusedMessageId: createMemo(() => undefined),
    focusedTurn: createMemo(() => undefined),
    focusedTurnError: createMemo(() => undefined),
    generating,
    handleFrame,
    highestSettledSeq,
    historyIncomplete: createMemo(() => false),
    historyReady,
    loadOlderMessages: () => false,
    loadedSkillCount,
    queuedPrompts,
    rows,
    sendPrompt,
    sendQueuedPromptNow: () => undefined,
    startedAt: createMemo(() => null),
    stopped: createMemo(() => false),
    working: createMemo(
      () =>
        (generating() && rows().length <= 1) ||
        (!generating() && durableRunningTurnId() !== undefined),
    ),
  } satisfies TanStackLiveChatTurn;

  return result;
}
