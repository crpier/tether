import { createEffect, createRoot, createSignal } from "solid-js";
import { describe, expect, test, vi } from "vitest";

import type { ConversationTurn, Message } from "./host/chat";
import type { ChatFrame } from "./chat-bus";
import { createLiveChatTurn } from "./live-chat-turn";

function chat(
  partial: Partial<Extract<ChatFrame, { type: "chat" }>>,
): ChatFrame {
  return { conversation_id: "conversation-1", type: "chat", ...partial };
}

function durableTurn(
  overrides: Partial<ConversationTurn> = {},
): ConversationTurn {
  return {
    completed_at: null,
    conversation_id: "conversation-1",
    created_at: "2026-01-01T00:00:00Z",
    failure_code: null,
    failure_summary: null,
    id: "turn-1",
    origin: "interactive",
    prompt: "queued prompt",
    reply_mode: "text",
    request_id: "request-1",
    started_at: null,
    status: "pending",
    ...overrides,
  };
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
    turn: null,
    turn_id: null,
    turn_message_seq: null,
  };
}

// Records every spoken-turn sink callback so tests can assert the exact
// sentence/restart/settle/discard sequence (#545).
function recordSpokenTurn() {
  const sentences: string[] = [];
  const settled: string[] = [];
  let restarted = 0;
  let discarded = 0;
  let toolOnlySettles = 0;
  return {
    sentences,
    settled,
    get restarted() {
      return restarted;
    },
    get discarded() {
      return discarded;
    },
    get toolOnlySettles() {
      return toolOnlySettles;
    },
    sink: {
      sentence: (text: string) => {
        sentences.push(text);
      },
      restart: () => {
        restarted += 1;
      },
      settle: (unspokenTail: string, info: { toolOnly: boolean }) => {
        if (info.toolOnly) {
          toolOnlySettles += 1;
          return;
        }
        if (unspokenTail.length > 0) {
          settled.push(unspokenTail);
        }
      },
      discard: () => {
        discarded += 1;
      },
    },
  };
}

describe("live chat turn", () => {
  test("session status exposes pi context usage", () => {
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

      turn.handleFrame(
        chat({
          context_percent: 31.55,
          context_tokens: 63_100,
          context_window: 200_000,
          event: "session_status",
        }),
      );

      expect(turn.contextUsage()).toEqual({
        contextPercent: 31.55,
        contextTokens: 63_100,
        contextWindow: 200_000,
      });
      dispose();
    });
  });

  test("unavailable session status clears stale context usage", () => {
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
      turn.handleFrame(
        chat({
          context_percent: 80,
          context_tokens: 160_000,
          context_window: 200_000,
          event: "session_status",
        }),
      );

      turn.handleFrame(
        chat({
          context_percent: null,
          context_tokens: null,
          context_window: null,
          event: "session_status",
        }),
      );

      expect(turn.contextUsage()).toBeUndefined();
      dispose();
    });
  });

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
      turn.handleFrame(chat({ event: "user_message", turn_id: "turn-1" }));
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

  test("settlement history replaces its optimistic user row without overlap", async () => {
    let messages: Message[] = [];
    const observedUserCounts: number[] = [];
    let dispose: () => void = () => undefined;
    const turn = createRoot((rootDispose) => {
      dispose = rootDispose;
      const liveTurn = createLiveChatTurn({
        conversationId: () => "conversation-1",
        history: {
          listMessages: () => Promise.resolve(messages),
          settled: () => undefined,
        },
        transport: {
          abort: () => undefined,
          sendPrompt: () => undefined,
        },
      });
      createEffect(() => {
        observedUserCounts.push(
          liveTurn
            .rows()
            .filter((row) => row.kind === "message" && row.role === "user")
            .length,
        );
      });
      return liveTurn;
    });
    await vi.waitFor(() => {
      expect(turn.historyReady()).toBe(true);
    });

    turn.sendPrompt("new turn");
    turn.handleFrame(chat({ event: "user_message", turn_id: "turn-1" }));
    messages = [{ ...message("new turn", 1), turn_id: "turn-1" }];
    turn.handleFrame(
      chat({ event: "turn_ended", status: "succeeded", turn_id: "turn-1" }),
    );

    await vi.waitFor(() => {
      expect(turn.rows().filter((row) => row.kind === "message")).toEqual([
        expect.objectContaining({ id: "message-1", text: "new turn" }),
      ]);
    });
    expect(Math.max(...observedUserCounts)).toBe(1);
    dispose();
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
      expect(turn.queuedPrompts()).toMatchObject([
        { content: "keep me", id: 1, replyMode: "text", retryable: true },
      ]);
      dispose();
    });
  });

  test("reconnect reattaches the same durable request and refreshes history", () => {
    createRoot((dispose) => {
      const sent: { content: string; requestId: string }[] = [];
      const listMessages = vi.fn(() => Promise.resolve<Message[]>([]));
      const settled = vi.fn();
      const turn = createLiveChatTurn({
        conversationId: () => "conversation-1",
        history: {
          listMessages,
          settled,
        },
        transport: {
          abort: () => undefined,
          sendPrompt: (_conversationId, content, _replyMode, requestId) => {
            sent.push({ content, requestId });
          },
        },
      });
      turn.sendPrompt("survive reconnect");
      turn.handleFrame(chat({ event: "user_message", turn_id: "turn-1" }));

      turn.handleFrame({ type: "connection", status: "closed" });
      turn.handleFrame({ type: "connection", status: "open" });

      expect(sent).toHaveLength(2);
      expect(sent[1]).toEqual(sent[0]);
      expect(settled).toHaveBeenCalledOnce();
      expect(listMessages).toHaveBeenCalled();
      expect(turn.busy()).toBe(true);
      dispose();
    });
  });

  test("submits every follow-up to the durable FIFO immediately", () => {
    createRoot((dispose) => {
      const sent: string[] = [];
      const turn = createLiveChatTurn({
        conversationId: () => "conversation-1",
        history: {
          listMessages: () => Promise.resolve([]),
          settled: () => undefined,
        },
        transport: {
          abort: () => undefined,
          sendPrompt: (_conversationId, content) => sent.push(content),
        },
      });

      turn.sendPrompt("first");
      turn.handleFrame(chat({ event: "user_message", turn_id: "turn-1" }));
      turn.sendPrompt("durable follow-up");

      expect(sent).toEqual(["first", "durable follow-up"]);
      expect(turn.queuedPrompts()).toMatchObject([
        { content: "durable follow-up" },
      ]);
      dispose();
    });
  });

  test("turn deep-link pagination expands from the latest page through its gap", async () => {
    let dispose: () => void = () => undefined;
    const calls: { beforeSeq?: number; turnId?: string }[] = [];
    const all = Array.from({ length: 60 }, (_, index) =>
      message(`message ${String(index + 1)}`, index + 1),
    );
    const turn = createRoot((rootDispose) => {
      dispose = rootDispose;
      return createLiveChatTurn({
        conversationId: () => "conversation-1",
        focusTurnId: () => "focused-turn",
        history: {
          listMessages: (_conversationId, options) => {
            calls.push(options);
            if (options.turnId !== undefined) {
              return Promise.resolve([message("focused", 5)]);
            }
            const before = options.beforeSeq ?? Number.POSITIVE_INFINITY;
            const page = all.filter((item) => item.seq < before);
            return Promise.resolve(page.slice(-30));
          },
          settled: () => undefined,
        },
        transport: {
          abort: () => undefined,
          sendPrompt: () => undefined,
        },
      });
    });

    await vi.waitFor(() => expect(turn.rows()).toHaveLength(31));
    expect(turn.loadOlderMessages()).toBe(true);
    await vi.waitFor(() => {
      expect(calls).toContainEqual({ beforeSeq: 31, limit: 30 });
      expect(turn.rows()).toHaveLength(60);
    });
    dispose();
  });

  test("a focused old turn does not mark the separately rendered latest page read", async () => {
    let dispose: () => void = () => undefined;
    const latest = Array.from({ length: 30 }, (_, index) =>
      message(`latest ${String(index + 31)}`, index + 31),
    );
    const turn = createRoot((rootDispose) => {
      dispose = rootDispose;
      return createLiveChatTurn({
        conversationId: () => "conversation-1",
        focusTurnId: () => "focused-turn",
        history: {
          listMessages: (_conversationId, options) =>
            Promise.resolve(
              options.turnId === undefined ? latest : [message("focused", 5)],
            ),
          settled: () => undefined,
        },
        transport: {
          abort: () => undefined,
          sendPrompt: () => undefined,
        },
      });
    });

    await vi.waitFor(() => expect(turn.historyReady()).toBe(true));
    expect(turn.highestSettledSeq()).toBe(5);
    dispose();
  });

  test("durable running state restores working and Stop targets its turn", () => {
    createRoot((dispose) => {
      const aborted: string[] = [];
      const turn = createLiveChatTurn({
        conversationId: () => "conversation-1",
        durablePendingCount: () => 2,
        durableRunningTurnId: () => "durable-running-turn",
        history: {
          listMessages: () => Promise.resolve([]),
          settled: () => undefined,
        },
        transport: {
          abort: (_conversationId, turnId) => aborted.push(turnId),
          sendPrompt: () => undefined,
        },
      });

      expect(turn.generating()).toBe(true);
      expect(turn.working()).toBe(true);
      expect(turn.durablePendingCount()).toBe(2);
      turn.abort();
      expect(aborted).toEqual(["durable-running-turn"]);
      dispose();
    });
  });

  test("a chosen queued prompt waits for durable turn settlement", () => {
    createRoot((dispose) => {
      const sent: (
        | {
            content: string;
            conversationId: string;
            replyMode: "spoken" | "text";
            type: "prompt";
          }
        | { conversationId: string; turnId: string; type: "abort" }
      )[] = [];
      const turn = createLiveChatTurn({
        conversationId: () => "conversation-1",
        history: {
          listMessages: () => Promise.resolve([]),
          settled: () => undefined,
        },
        now: () => 100,
        transport: {
          abort: (conversationId, turnId) => {
            sent.push({ conversationId, turnId, type: "abort" });
          },
          sendPrompt: (conversationId, content, replyMode) => {
            sent.push({ content, conversationId, replyMode, type: "prompt" });
          },
        },
      });

      turn.sendPrompt("active");
      turn.handleFrame(chat({ event: "user_message", turn_id: "turn-1" }));
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
        {
          content: "chosen",
          conversationId: "conversation-1",
          replyMode: "text",
          type: "prompt",
        },
        { conversationId: "conversation-1", turnId: "turn-1", type: "abort" },
      ]);

      turn.handleFrame(chat({ event: "agent_end" }));
      turn.handleFrame(
        chat({ event: "turn_ended", status: "cancelled", turn_id: "turn-1" }),
      );

      expect(sent.filter((entry) => entry.type === "prompt")).toHaveLength(2);
      dispose();
    });
  });

  test("editing a durable queued prompt cancels and replaces it with its captured reply mode", () => {
    createRoot((dispose) => {
      const sent: {
        content: string;
        conversationId: string;
        replyMode: "spoken" | "text";
        type: "prompt";
      }[] = [];
      const aborted: string[] = [];
      const replyMode = vi.fn<() => "spoken" | "text">(() => "spoken");
      const turn = createLiveChatTurn({
        conversationId: () => "conversation-1",
        history: {
          listMessages: () => Promise.resolve([]),
          settled: () => undefined,
        },
        replyMode,
        transport: {
          abort: (_conversationId, turnId) => aborted.push(turnId),
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
      turn.handleFrame(
        chat({ event: "turn_queued", status: "running", turn_id: "turn-1" }),
      );
      turn.handleFrame(chat({ event: "user_message", turn_id: "turn-1" }));
      turn.handleFrame(
        chat({ event: "turn_queued", status: "pending", turn_id: "turn-2" }),
      );
      expect(turn.queuedPrompts()).toMatchObject([
        { content: "second", id: 2, replyMode: "spoken", turnId: "turn-2" },
      ]);

      replyMode.mockReturnValue("text");
      turn.editQueuedPrompt(2, "second edited");
      expect(turn.queuedPrompts()).toMatchObject([
        { content: "second edited", id: 2, replyMode: "spoken" },
      ]);
      expect(aborted).toEqual(["turn-2"]);

      turn.handleFrame(chat({ event: "agent_end", final_text: "answer one" }));
      turn.handleFrame(
        chat({ event: "turn_ended", status: "succeeded", turn_id: "turn-1" }),
      );

      expect(sent.at(-1)).toEqual({
        content: "second edited",
        conversationId: "conversation-1",
        replyMode: "spoken",
        type: "prompt",
      });
      dispose();
    });
  });

  test("only a settled spoken turn settles playback, exactly once", () => {
    createRoot((dispose) => {
      const spoken = recordSpokenTurn();
      const replyMode = vi.fn<() => "spoken" | "text">(() => "text");
      const turn = createLiveChatTurn({
        conversationId: () => "conversation-1",
        history: {
          listMessages: () => Promise.resolve([]),
          settled: () => undefined,
        },
        replyMode,
        spokenTurn: spoken.sink,
        transport: {
          abort: () => undefined,
          sendPrompt: () => undefined,
        },
      });

      turn.sendPrompt("text question");
      turn.handleFrame(chat({ event: "user_message", turn_id: "turn-text" }));
      turn.handleFrame(chat({ event: "agent_end", final_text: "text answer" }));
      turn.handleFrame(
        chat({
          event: "turn_ended",
          status: "succeeded",
          turn_id: "turn-text",
        }),
      );
      expect(spoken.settled).toEqual([]);
      expect(spoken.discarded).toBe(0);

      replyMode.mockReturnValue("spoken");
      turn.sendPrompt("spoken question");
      turn.handleFrame(chat({ event: "user_message", turn_id: "turn-spoken" }));
      for (const frame of [
        chat({ event: "message_start" }),
        chat({ event: "text_delta", delta: "partial" }),
      ]) {
        turn.handleFrame(frame);
      }
      // No sentence boundary yet: nothing provisional has been spoken.
      expect(spoken.sentences).toEqual([]);

      turn.handleFrame(
        chat({ event: "agent_end", final_text: "spoken answer" }),
      );
      expect(spoken.settled).toEqual(["spoken answer"]);
      dispose();
    });
  });

  test("aborted and errored turns discard playback", () => {
    createRoot((dispose) => {
      const spoken = recordSpokenTurn();
      const turn = createLiveChatTurn({
        conversationId: () => "conversation-1",
        history: {
          listMessages: () => Promise.resolve([]),
          settled: () => undefined,
        },
        replyMode: () => "spoken",
        spokenTurn: spoken.sink,
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
      expect(spoken.settled).toEqual([]);
      expect(spoken.discarded).toBeGreaterThanOrEqual(1);

      turn.sendPrompt("errored question");
      turn.handleFrame(chat({ detail: "provider down", event: "error" }));
      turn.handleFrame(chat({ event: "agent_end", final_text: "whatever" }));
      expect(spoken.settled).toEqual([]);
      dispose();
    });
  });

  test("streams complete sentences as they arrive (#545)", () => {
    createRoot((dispose) => {
      const spoken = recordSpokenTurn();
      const turn = createLiveChatTurn({
        conversationId: () => "conversation-1",
        history: {
          listMessages: () => Promise.resolve([]),
          settled: () => undefined,
        },
        replyMode: () => "spoken",
        spokenTurn: spoken.sink,
        transport: {
          abort: () => undefined,
          sendPrompt: () => undefined,
        },
      });

      turn.sendPrompt("question");
      turn.handleFrame(
        chat({ event: "text_delta", delta: "First one. Second" }),
      );
      expect(spoken.sentences).toEqual(["First one. "]);

      turn.handleFrame(chat({ event: "text_delta", delta: " one. Tail" }));
      expect(spoken.sentences).toEqual(["First one. ", "Second one. "]);

      turn.handleFrame(
        chat({
          event: "agent_end",
          final_text: "First one. Second one. Tail.",
        }),
      );
      expect(spoken.settled).toEqual(["Tail."]);
      dispose();
    });
  });

  test("tool activity restarts speech so the settled answer plays whole", () => {
    createRoot((dispose) => {
      const spoken = recordSpokenTurn();
      const turn = createLiveChatTurn({
        conversationId: () => "conversation-1",
        history: {
          listMessages: () => Promise.resolve([]),
          settled: () => undefined,
        },
        replyMode: () => "spoken",
        spokenTurn: spoken.sink,
        transport: {
          abort: () => undefined,
          sendPrompt: () => undefined,
        },
      });

      turn.sendPrompt("question");
      turn.handleFrame(chat({ event: "text_delta", delta: "Let me check. " }));
      expect(spoken.sentences).toEqual(["Let me check. "]);

      turn.handleFrame(
        chat({ event: "tool_start", tool_name: "search", tool_id: "t1" }),
      );
      expect(spoken.restarted).toBeGreaterThanOrEqual(1);

      turn.handleFrame(chat({ event: "tool_end", tool_id: "t1" }));
      turn.handleFrame(chat({ event: "text_delta", delta: "Found it." }));
      turn.handleFrame(
        chat({ event: "agent_end", final_text: "Found it. Done." }),
      );

      // Prefix was invalidated by the tool start: the full settled text is
      // re-delivered at settle (the player cancels the stale queue first).
      expect(spoken.settled).toEqual(["Found it. Done."]);
      dispose();
    });
  });

  test("parallel tools restart spoken output once per tool phase", () => {
    createRoot((dispose) => {
      const spoken = recordSpokenTurn();
      const turn = createLiveChatTurn({
        conversationId: () => "conversation-1",
        history: {
          listMessages: () => Promise.resolve([]),
          settled: () => undefined,
        },
        replyMode: () => "spoken",
        spokenTurn: spoken.sink,
        transport: {
          abort: () => undefined,
          sendPrompt: () => undefined,
        },
      });

      turn.sendPrompt("question");
      turn.handleFrame(
        chat({ event: "tool_start", tool_name: "search", tool_id: "t1" }),
      );
      turn.handleFrame(
        chat({ event: "tool_start", tool_name: "fetch", tool_id: "t2" }),
      );
      expect(spoken.restarted).toBe(1);

      turn.handleFrame(chat({ event: "text_delta", delta: "Checking again." }));
      turn.handleFrame(
        chat({ event: "tool_start", tool_name: "search", tool_id: "t3" }),
      );
      expect(spoken.restarted).toBe(2);
      dispose();
    });
  });

  test("a provisional prompt stays queued outside the canonical transcript", () => {
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

      turn.sendPrompt("not accepted yet");

      expect(turn.rows()).toEqual([]);
      expect(turn.queuedPrompts()).toMatchObject([
        { content: "not accepted yet" },
      ]);

      turn.handleFrame(chat({ detail: "rejected", event: "error" }));

      expect(turn.rows()).toEqual([]);
      expect(turn.queuedPrompts()).toMatchObject([
        { content: "not accepted yet", retryable: true },
      ]);
      dispose();
    });
  });

  test("Conversation navigation cannot resubmit or abort the prior runtime", () => {
    createRoot((dispose) => {
      const [conversationId, setConversationId] =
        createSignal("conversation-1");
      const sent: string[] = [];
      const aborted: { conversationId: string; turnId: string }[] = [];
      const turn = createLiveChatTurn({
        conversationId,
        history: {
          listMessages: () => Promise.resolve([]),
          settled: () => undefined,
        },
        transport: {
          abort: (id, turnId) => aborted.push({ conversationId: id, turnId }),
          sendPrompt: (id, content) => sent.push(`${id}:${content}`),
        },
      });
      turn.sendPrompt("belongs to A");
      turn.handleFrame(
        chat({ event: "turn_queued", status: "running", turn_id: "turn-a" }),
      );
      turn.handleFrame(chat({ event: "user_message", turn_id: "turn-a" }));

      setConversationId("conversation-2");
      turn.handleFrame({ status: "open", type: "connection" });
      turn.abort();

      expect(sent).toEqual(["conversation-1:belongs to A"]);
      expect(aborted).toEqual([]);
      expect(turn.queuedPrompts()).toEqual([]);
      expect(turn.rows()).toEqual([]);
      dispose();
    });
  });

  test("reconnect terminal duplicate settles its matching request", () => {
    createRoot((dispose) => {
      const sent: string[] = [];
      const turn = createLiveChatTurn({
        conversationId: () => "conversation-1",
        durableRunningTurnId: () => "turn-1",
        history: {
          listMessages: () => Promise.resolve([]),
          settled: () => undefined,
        },
        transport: {
          abort: () => undefined,
          sendPrompt: (_id, _content, _mode, requestId) => sent.push(requestId),
        },
      });
      turn.sendPrompt("survive reconnect");
      turn.handleFrame(
        chat({ event: "turn_queued", status: "running", turn_id: "turn-1" }),
      );
      turn.handleFrame(chat({ event: "user_message", turn_id: "turn-1" }));

      turn.handleFrame({ status: "open", type: "connection" });
      turn.handleFrame(
        chat({ event: "turn_queued", status: "failed", turn_id: "turn-1" }),
      );
      turn.handleFrame(
        chat({
          event: "turn_ended",
          failure_summary: "settled while disconnected",
          status: "failed",
          turn_id: "turn-1",
        }),
      );

      expect(sent[1]).toBe(sent[0]);
      expect(turn.error()).toBe("settled while disconnected");
      expect(turn.busy()).toBe(false);
      dispose();
    });
  });

  test("pending duplicate resumes when its canonical user Message arrives", () => {
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
      turn.sendPrompt("pending through reconnect");
      turn.handleFrame(
        chat({ event: "turn_queued", status: "pending", turn_id: "turn-1" }),
      );

      turn.handleFrame({ status: "open", type: "connection" });
      turn.handleFrame(
        chat({ event: "turn_queued", status: "pending", turn_id: "turn-1" }),
      );
      turn.handleFrame(chat({ event: "user_message", turn_id: "turn-1" }));

      expect(turn.rows()).toMatchObject([
        { kind: "message", role: "user", text: "pending through reconnect" },
      ]);
      expect(turn.generating()).toBe(true);
      expect(turn.queuedPrompts()).toEqual([]);
      dispose();
    });
  });

  test("durable pending turns hydrate editable queue controls", async () => {
    let dispose: () => void = () => undefined;
    const turn = createRoot((rootDispose) => {
      dispose = rootDispose;
      return createLiveChatTurn({
        conversationId: () => "conversation-1",
        durablePendingCount: () => 1,
        history: {
          listMessages: () => Promise.resolve([]),
          listNonterminalTurns: () =>
            Promise.resolve([
              durableTurn({
                id: "durable-pending",
                prompt: "restored after refresh",
                reply_mode: "spoken",
              }),
            ]),
          settled: () => undefined,
        },
        transport: {
          abort: () => undefined,
          sendPrompt: () => undefined,
        },
      });
    });

    await vi.waitFor(() => {
      expect(turn.queuedPrompts()).toMatchObject([
        {
          content: "restored after refresh",
          replyMode: "spoken",
          turnId: "durable-pending",
        },
      ]);
    });
    dispose();
  });

  test("tool-only turns settle flagged so nonsense markers are never spoken", () => {
    createRoot((dispose) => {
      const spoken = recordSpokenTurn();
      const turn = createLiveChatTurn({
        conversationId: () => "conversation-1",
        history: {
          listMessages: () => Promise.resolve([]),
          settled: () => undefined,
        },
        replyMode: () => "spoken",
        spokenTurn: spoken.sink,
        transport: {
          abort: () => undefined,
          sendPrompt: () => undefined,
        },
      });

      turn.sendPrompt("do a thing");
      turn.handleFrame(chat({ event: "tool_start", tool_name: "x" }));
      turn.handleFrame(chat({ event: "agent_end", tool_only: true }));

      expect(spoken.settled).toEqual([]);
      expect(spoken.toolOnlySettles).toBe(1);
      dispose();
    });
  });
});
