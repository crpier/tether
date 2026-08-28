import { createEffect, createRoot } from "solid-js";
import { expect, test, vi } from "vitest";

import type { ChatFrame } from "../../chat-bus";
import type { ConversationTurn, Message } from "../../host/chat";
import { createTanStackLiveChatTurn } from "./tanstack-live-chat-turn";

function chat(
  partial: Partial<Extract<ChatFrame, { type: "chat" }>>,
): ChatFrame {
  return { conversation_id: "conversation-1", type: "chat", ...partial };
}

function message(
  id: string,
  role: Message["role"],
  content: string,
  seq: number,
): Message {
  return {
    attachments: [],
    content,
    conversation_id: "conversation-1",
    created_at: "2026-01-01T00:00:00Z",
    id,
    pi_message_id: null,
    role,
    seq,
    tool_args: null,
    tool_name: null,
    tool_result: null,
    turn: null,
    turn_id: "turn-1",
    turn_message_seq: seq,
  };
}

test("TanStack path exposes Tether's ordered live transcript contract", async () => {
  let dispose: () => void = () => undefined;
  const turn = createRoot((rootDispose) => {
    dispose = rootDispose;
    return createTanStackLiveChatTurn({
      conversationId: () => "conversation-1",
      history: {
        listMessages: () => Promise.resolve([]),
        settled: () => undefined,
      },
      transport: {
        abort: () => undefined,
        sendPrompt: () => undefined,
      },
    });
  });

  turn.sendPrompt("investigate");
  for (const frame of [
    chat({
      event: "turn_queued",
      status: "running",
      turn_id: "turn-1",
    }),
    chat({
      event: "user_message",
      message_id: "message-user",
      seq: 1,
      turn_id: "turn-1",
    }),
    chat({ event: "message_start", turn_id: "turn-1" }),
    chat({
      event: "thinking_delta",
      delta: "pondering",
      turn_id: "turn-1",
    }),
    chat({
      event: "tool_start",
      tool_args: { q: "needle" },
      tool_id: "tool-1",
      tool_name: "search",
      turn_id: "turn-1",
    }),
    chat({
      event: "tool_end",
      tool_id: "tool-1",
      tool_name: "search",
      tool_result: { hits: 1 },
      turn_id: "turn-1",
    }),
    chat({ event: "text_delta", delta: "answer", turn_id: "turn-1" }),
  ]) {
    turn.handleFrame(frame);
  }

  await vi.waitFor(() => {
    expect(turn.rows()).toMatchObject([
      {
        id: "message-user",
        kind: "message",
        role: "user",
        text: "investigate",
      },
      { kind: "reasoning", text: "pondering" },
      {
        args: { q: "needle" },
        kind: "tool",
        result: { hits: 1 },
        status: "done",
        toolName: "search",
      },
      { kind: "message", role: "assistant", text: "answer" },
    ]);
  });
  dispose();
});

test("follow-ups enter Tether's durable FIFO immediately", async () => {
  const sentContents: string[] = [];
  const sendPrompt = vi.fn(
    (
      _conversationId: string,
      content: string,
      _replyMode: "spoken" | "text",
      _requestId: string,
      _attachmentIds: readonly string[],
    ) => {
      void _conversationId;
      void _replyMode;
      void _requestId;
      void _attachmentIds;
      sentContents.push(content);
    },
  );
  let dispose: () => void = () => undefined;
  const turn = createRoot((rootDispose) => {
    dispose = rootDispose;
    return createTanStackLiveChatTurn({
      conversationId: () => "conversation-1",
      history: {
        listMessages: () => Promise.resolve([]),
        settled: () => undefined,
      },
      transport: {
        abort: () => undefined,
        sendPrompt,
      },
    });
  });

  turn.sendPrompt("first");
  turn.sendPrompt("second");

  await vi.waitFor(() => {
    expect(sendPrompt).toHaveBeenCalledTimes(2);
  });
  expect(sentContents).toEqual(["first", "second"]);
  dispose();
});

test("host pending and running turns hydrate through the shared contract", async () => {
  const abort = vi.fn();
  const turns: ConversationTurn[] = [
    {
      attachments: [],
      completed_at: null,
      conversation_id: "conversation-1",
      created_at: "2026-01-01T00:00:00Z",
      failure_code: null,
      failure_summary: null,
      id: "turn-running",
      origin: "interactive",
      prompt: "running",
      reply_mode: "text",
      request_id: "request-running",
      started_at: "2026-01-01T00:00:01Z",
      status: "running",
    },
    {
      attachments: [],
      completed_at: null,
      conversation_id: "conversation-1",
      created_at: "2026-01-01T00:00:02Z",
      failure_code: null,
      failure_summary: null,
      id: "turn-pending",
      origin: "interactive",
      prompt: "waiting",
      reply_mode: "spoken",
      request_id: "request-pending",
      started_at: null,
      status: "pending",
    },
  ];
  let dispose: () => void = () => undefined;
  const turn = createRoot((rootDispose) => {
    dispose = rootDispose;
    return createTanStackLiveChatTurn({
      conversationId: () => "conversation-1",
      history: {
        listMessages: () => Promise.resolve([]),
        listNonterminalTurns: () => Promise.resolve(turns),
        settled: () => undefined,
      },
      transport: {
        abort,
        sendPrompt: () => undefined,
      },
    });
  });

  await vi.waitFor(() => {
    expect(turn.working()).toBe(true);
    expect(turn.queuedPrompts()).toMatchObject([
      { content: "waiting", replyMode: "spoken", turnId: "turn-pending" },
    ]);
  });
  turn.abort();
  expect(abort).toHaveBeenCalledWith("conversation-1", "turn-running");
  dispose();
});

test("spoken settlement still uses Tether's authoritative terminal frame", async () => {
  const settle = vi.fn();
  let dispose: () => void = () => undefined;
  const turn = createRoot((rootDispose) => {
    dispose = rootDispose;
    return createTanStackLiveChatTurn({
      conversationId: () => "conversation-1",
      history: {
        listMessages: () => Promise.resolve([]),
        settled: () => undefined,
      },
      replyMode: () => "spoken",
      spokenTurn: {
        discard: () => undefined,
        restart: () => undefined,
        sentence: () => undefined,
        settle,
      },
      transport: {
        abort: () => undefined,
        sendPrompt: () => undefined,
      },
    });
  });

  turn.sendPrompt("speak");
  turn.handleFrame(
    chat({
      event: "agent_end",
      final_text: "authoritative answer",
      reply_mode: "spoken",
      tool_only: false,
      turn_id: "turn-1",
    }),
  );

  await vi.waitFor(() => {
    expect(settle).toHaveBeenCalledWith("authoritative answer", {
      fullText: "authoritative answer",
      toolOnly: false,
    });
  });
  dispose();
});

test("settlement replaces live rows with canonical Tether Message identities", async () => {
  let stored: Message[] = [];
  const userRowCounts: number[] = [];
  let dispose: () => void = () => undefined;
  const turn = createRoot((rootDispose) => {
    dispose = rootDispose;
    const liveTurn = createTanStackLiveChatTurn({
      conversationId: () => "conversation-1",
      history: {
        listMessages: () => Promise.resolve(stored),
        settled: () => undefined,
      },
      transport: {
        abort: () => undefined,
        sendPrompt: () => undefined,
      },
    });
    createEffect(() => {
      userRowCounts.push(
        liveTurn
          .rows()
          .filter((row) => row.kind === "message" && row.role === "user")
          .length,
      );
    });
    return liveTurn;
  });

  turn.sendPrompt("remember me");
  for (const frame of [
    chat({
      event: "turn_queued",
      status: "running",
      turn_id: "turn-1",
    }),
    chat({
      event: "user_message",
      message_id: "message-user",
      seq: 1,
      turn_id: "turn-1",
    }),
    chat({ event: "message_start", turn_id: "turn-1" }),
    chat({ event: "text_delta", delta: "settled", turn_id: "turn-1" }),
    chat({ event: "message_end", turn_id: "turn-1" }),
  ]) {
    turn.handleFrame(frame);
  }
  stored = [
    message("message-user", "user", "remember me", 1),
    message("message-assistant", "assistant", "settled", 2),
  ];
  turn.handleFrame(
    chat({
      event: "agent_end",
      final_text: "settled",
      reply_mode: "text",
      tool_only: false,
      turn_id: "turn-1",
    }),
  );

  await vi.waitFor(() => {
    expect(turn.rows().map((row) => row.id)).toEqual([
      "message-user",
      "message-assistant",
    ]);
  });
  expect(Math.max(...userRowCounts)).toBe(1);
  dispose();
});
