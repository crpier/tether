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
      expect(turn.queuedPrompts()).toEqual([
        { content: "keep me", id: 1, replyMode: "text" },
      ]);
      dispose();
    });
  });

  test("a chosen queued prompt waits for agent_end after abort acknowledgement", () => {
    createRoot((dispose) => {
      const sent: (
        | {
            content: string;
            conversationId: string;
            replyMode: "spoken" | "text";
            type: "prompt";
          }
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
          sendPrompt: (conversationId, content, replyMode) => {
            sent.push({ content, conversationId, replyMode, type: "prompt" });
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
          replyMode: "text",
          type: "prompt",
        },
        { conversationId: "conversation-1", type: "abort" },
      ]);

      turn.handleFrame(chat({ event: "agent_end" }));

      expect(sent.at(-1)).toEqual({
        content: "chosen",
        conversationId: "conversation-1",
        replyMode: "text",
        type: "prompt",
      });
      dispose();
    });
  });

  test("queued prompts capture the reply mode at enqueue and keep it", () => {
    createRoot((dispose) => {
      const sent: {
        content: string;
        conversationId: string;
        replyMode: "spoken" | "text";
        type: "prompt";
      }[] = [];
      const replyMode = vi.fn<() => "spoken" | "text">(() => "spoken");
      const turn = createLiveChatTurn({
        conversationId: () => "conversation-1",
        history: {
          listMessages: () => Promise.resolve([]),
          settled: () => undefined,
        },
        replyMode,
        transport: {
          abort: () => undefined,
          sendPrompt: (conversationId, content, mode) => {
            sent.push({
              content,
              conversationId,
              replyMode: mode,
              type: "prompt",
            });
          },
        },
      });

      turn.sendPrompt("first");
      turn.sendPrompt("second");
      expect(turn.queuedPrompts()).toEqual([
        { content: "second", id: 2, replyMode: "spoken" },
      ]);

      // The toggle flips while the first turn runs; queued modes stay captured.
      replyMode.mockReturnValue("text");
      turn.editQueuedPrompt(2, "second edited");
      expect(turn.queuedPrompts()).toEqual([
        { content: "second edited", id: 2, replyMode: "spoken" },
      ]);

      turn.handleFrame(chat({ event: "agent_end", final_text: "answer one" }));

      expect(sent.at(-1)).toEqual({
        content: "second edited",
        conversationId: "conversation-1",
        replyMode: "spoken",
        type: "prompt",
      });
      dispose();
    });
  });

  test("only a settled spoken turn invokes playback, exactly once", () => {
    createRoot((dispose) => {
      const onSettledReply = vi.fn();
      const replyMode = vi.fn<() => "spoken" | "text">(() => "text");
      const turn = createLiveChatTurn({
        conversationId: () => "conversation-1",
        history: {
          listMessages: () => Promise.resolve([]),
          settled: () => undefined,
        },
        onSettledReply,
        replyMode,
        transport: {
          abort: () => undefined,
          sendPrompt: () => undefined,
        },
      });

      turn.sendPrompt("text question");
      turn.handleFrame(chat({ event: "agent_end", final_text: "text answer" }));
      expect(onSettledReply).not.toHaveBeenCalled();

      replyMode.mockReturnValue("spoken");
      turn.sendPrompt("spoken question");
      for (const frame of [
        chat({ event: "message_start" }),
        chat({ event: "text_delta", delta: "partial" }),
      ]) {
        turn.handleFrame(frame);
      }
      expect(onSettledReply).not.toHaveBeenCalled();

      turn.handleFrame(
        chat({ event: "agent_end", final_text: "spoken answer" }),
      );
      expect(onSettledReply).toHaveBeenCalledTimes(1);
      expect(onSettledReply).toHaveBeenCalledWith("spoken answer");
      dispose();
    });
  });

  test("aborted and errored turns never invoke playback", () => {
    createRoot((dispose) => {
      const onSettledReply = vi.fn();
      const turn = createLiveChatTurn({
        conversationId: () => "conversation-1",
        history: {
          listMessages: () => Promise.resolve([]),
          settled: () => undefined,
        },
        onSettledReply,
        replyMode: () => "spoken",
        transport: {
          abort: () => undefined,
          sendPrompt: () => undefined,
        },
      });

      turn.sendPrompt("aborted question");
      turn.handleFrame(chat({ event: "abort_ack" }));
      turn.handleFrame(
        chat({ event: "agent_end", final_text: "partial answer" }),
      );
      expect(onSettledReply).not.toHaveBeenCalled();

      turn.sendPrompt("errored question");
      turn.handleFrame(chat({ detail: "provider down", event: "error" }));
      turn.handleFrame(chat({ event: "agent_end", final_text: "whatever" }));
      expect(onSettledReply).not.toHaveBeenCalled();
      dispose();
    });
  });
});
