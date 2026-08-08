import { createRoot } from "solid-js";
import { describe, expect, test, vi } from "vitest";

import type { Message } from "./host/chat";
import type { ChatFrame } from "./chat-bus";
import { createLiveChatTurn } from "./live-chat-turn";

function chat(
  partial: Partial<Extract<ChatFrame, { type: "chat" }>>,
): ChatFrame {
  return { conversation_id: "conversation-1", type: "chat", ...partial };
}

function message(content: string, seq: number): Message {
  return {
    content,
    conversation_id: "conversation-1",
    created_at: "2026-01-01T00:00:00Z",
    id: `message-${seq.toString()}`,
    pi_message_id: null,
    role: "user",
    seq,
    tool_args: null,
    tool_name: null,
    tool_result: null,
  };
}

describe("live chat turn", () => {
  test("frames form one ordered reasoning, tool, and answer transcript", () => {
    createRoot((dispose) => {
      const turn = createLiveChatTurn({
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

      turn.sendPrompt("investigate");
      for (const frame of [
        chat({ event: "message_start" }),
        chat({ event: "thinking_delta", delta: "pondering" }),
        chat({
          event: "tool_start",
          tool_args: { q: "needle" },
          tool_id: "tool-1",
          tool_name: "search",
        }),
        chat({
          event: "tool_end",
          tool_id: "tool-1",
          tool_name: "search",
          tool_result: { hits: 1 },
        }),
        chat({ event: "text_delta", delta: "answer" }),
      ]) {
        turn.handleFrame(frame);
      }

      expect(turn.rows()).toMatchObject([
        { kind: "message", role: "user", text: "investigate" },
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
      dispose();
    });
  });

  test("unchanged settled rows retain identity while a turn streams", async () => {
    let dispose: () => void = () => undefined;
    const turn = createRoot((rootDispose) => {
      dispose = rootDispose;
      return createLiveChatTurn({
        conversationId: () => "conversation-1",
        history: {
          listMessages: () => Promise.resolve([message("settled", 1)]),
          settled: () => undefined,
        },
        transport: {
          abort: () => undefined,
          sendPrompt: () => undefined,
        },
      });
    });
    await vi.waitFor(() => {
      expect(turn.rows()).toHaveLength(1);
    });
    const settled = turn.rows()[0];

    turn.sendPrompt("new turn");
    turn.handleFrame(chat({ event: "text_delta", delta: "streaming" }));

    expect(turn.rows()[0]).toBe(settled);
    dispose();
  });

  test("older history prepends without disturbing transcript order", async () => {
    let dispose: () => void = () => undefined;
    const latest = Array.from({ length: 30 }, (_, index) =>
      message(`recent ${index.toString()}`, index + 31),
    );
    const turn = createRoot((rootDispose) => {
      dispose = rootDispose;
      return createLiveChatTurn({
        conversationId: () => "conversation-1",
        history: {
          listMessages: (_conversationId, options) =>
            Promise.resolve(
              options.beforeSeq === undefined ? latest : [message("oldest", 1)],
            ),
          settled: () => undefined,
        },
        transport: {
          abort: () => undefined,
          sendPrompt: () => undefined,
        },
      });
    });

    await vi.waitFor(() => {
      expect(turn.rows()).toHaveLength(30);
    });
    expect(turn.loadOlderMessages()).toBe(true);
    await vi.waitFor(() => {
      const first = turn.rows()[0];
      expect(first).toMatchObject({ text: "oldest" });
    });
    dispose();
  });

  test("settled history appears in transcript order", async () => {
    let dispose: () => void = () => undefined;
    const turn = createRoot((rootDispose) => {
      dispose = rootDispose;
      return createLiveChatTurn({
        conversationId: () => "conversation-1",
        history: {
          listMessages: () =>
            Promise.resolve([message("second", 2), message("first", 1)]),
          settled: () => undefined,
        },
        transport: {
          abort: () => undefined,
          sendPrompt: () => undefined,
        },
      });
    });

    await vi.waitFor(() => {
      expect(
        turn.rows().map((row) => (row.kind === "message" ? row.text : "")),
      ).toEqual(["first", "second"]);
    });
    dispose();
  });

  test("a rejected prompt remains queued for retry", () => {
    createRoot((dispose) => {
      const turn = createLiveChatTurn({
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

      turn.sendPrompt("keep me");
      turn.handleFrame({
        conversation_id: "conversation-1",
        detail: "provider unavailable",
        event: "error",
        type: "chat",
      });

      expect(turn.error()).toBe("provider unavailable");
      expect(turn.queuedPrompts()).toEqual([{ content: "keep me", id: 1 }]);
      dispose();
    });
  });

  test("a chosen queued prompt waits for agent_end after abort acknowledgement", () => {
    createRoot((dispose) => {
      const sent: (
        | { content: string; conversationId: string; type: "prompt" }
        | { conversationId: string; type: "abort" }
      )[] = [];
      const turn = createLiveChatTurn({
        conversationId: () => "conversation-1",
        history: {
          listMessages: () => Promise.resolve([]),
          settled: () => undefined,
        },
        now: () => 100,
        transport: {
          abort: (conversationId) => {
            sent.push({ conversationId, type: "abort" });
          },
          sendPrompt: (conversationId, content) => {
            sent.push({ content, conversationId, type: "prompt" });
          },
        },
      });

      turn.sendPrompt("active");
      turn.sendPrompt("chosen");
      const chosen = turn.queuedPrompts()[0];
      turn.sendQueuedPromptNow(chosen.id);
      turn.handleFrame(chat({ event: "abort_ack" }));

      expect(sent).toEqual([
        {
          content: "active",
          conversationId: "conversation-1",
          type: "prompt",
        },
        { conversationId: "conversation-1", type: "abort" },
      ]);

      turn.handleFrame(chat({ event: "agent_end" }));

      expect(sent.at(-1)).toEqual({
        content: "chosen",
        conversationId: "conversation-1",
        type: "prompt",
      });
      dispose();
    });
  });
});
