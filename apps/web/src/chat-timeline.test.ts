import { describe, expect, test } from "vitest";

import type { ChatFrame } from "./chat-bus";
import {
  deriveRows,
  emptyTurn,
  isAwaitingFirstToken,
  reduceFrame,
  startTurn,
  type LiveTurn,
} from "./chat-timeline";

function chat(
  partial: Partial<Extract<ChatFrame, { type: "chat" }>>,
): ChatFrame {
  return { type: "chat", ...partial };
}

function run(frames: ChatFrame[], from?: LiveTurn): LiveTurn {
  let turn = from ?? startTurn("hi", 0);
  let now = 1;
  for (const frame of frames) {
    turn = reduceFrame(turn, frame, now);
    now += 1;
  }
  return turn;
}

describe("chat-timeline seam", () => {
  test("accumulates text deltas into a single assistant row", () => {
    const turn = run([
      chat({ event: "message_start" }),
      chat({ event: "text_delta", delta: { text: "Hi" } }),
      chat({ event: "text_delta", delta: " there" }),
    ]);
    const rows = deriveRows([], turn);
    const assistant = rows.filter(
      (row) => row.kind === "message" && row.role === "assistant",
    );
    expect(assistant).toHaveLength(1);
    expect(assistant[0]).toMatchObject({ text: "Hi there", streaming: true });
  });

  test("keeps reasoning in its own row, never merged into the answer", () => {
    const turn = run([
      chat({ event: "message_start" }),
      chat({
        event: "thinking_delta",
        delta: { text: "secret" },
        content_index: 0,
      }),
      chat({
        event: "text_delta",
        delta: { text: "answer" },
        content_index: 1,
      }),
    ]);
    const rows = deriveRows([], turn);
    const reasoning = rows.find((row) => row.kind === "reasoning");
    const assistant = rows.find(
      (row) => row.kind === "message" && row.role === "assistant",
    );
    expect(reasoning).toMatchObject({ text: "secret" });
    expect(assistant).toMatchObject({ text: "answer" });
  });

  test("renders reasoning, tool, then answer in arrival order", () => {
    const turn = run([
      chat({ event: "message_start" }),
      chat({ event: "thinking_delta", delta: "mulling" }),
      chat({ event: "tool_start", tool_name: "search", tool_id: "t1" }),
      chat({ event: "tool_end", tool_name: "search", tool_id: "t1" }),
      chat({ event: "text_delta", delta: "done" }),
    ]);
    const rows = deriveRows([], turn);
    expect(rows.map((row) => row.kind)).toEqual([
      "message", // optimistic user
      "reasoning",
      "tool",
      "message", // assistant answer
    ]);
  });

  test("matches tool_end to its tool_start by id and marks it done", () => {
    const turn = run([
      chat({ event: "tool_start", tool_name: "search", tool_id: "t1" }),
      chat({ event: "tool_start", tool_name: "capture", tool_id: "t2" }),
      chat({ event: "tool_end", tool_name: "capture", tool_id: "t2" }),
    ]);
    const tools = turn.rows.filter((row) => row.kind === "tool");
    expect(tools).toMatchObject([
      { toolName: "search", status: "running" },
      { toolName: "capture", status: "done" },
    ]);
  });

  test("error frame stops generation and records detail", () => {
    const turn = run([chat({ event: "error", detail: "boom" })]);
    expect(turn.generating).toBe(false);
    expect(turn.error).toBe("boom");
  });

  test("abort_ack marks the turn stopped", () => {
    const turn = run([chat({ event: "abort_ack" })]);
    expect(turn.generating).toBe(false);
    expect(turn.stopped).toBe(true);
  });

  test("agent_end ends generation without an error", () => {
    const turn = run([chat({ event: "agent_end" })]);
    expect(turn.generating).toBe(false);
    expect(turn.error).toBeNull();
  });

  test("deriveRows prepends settled history before the live turn", () => {
    const turn = run([
      chat({ event: "message_start" }),
      chat({ event: "text_delta", delta: "reply" }),
    ]);
    const rows = deriveRows(
      [{ id: "m1", role: "user", content: "earlier" }],
      turn,
    );
    expect(rows[0]).toMatchObject({ id: "m1", text: "earlier" });
    expect(rows[1]).toMatchObject({ role: "user", text: "hi" });
  });

  test("awaiting-first-token is true before any answer text arrives", () => {
    let turn = startTurn("hi", 0);
    expect(isAwaitingFirstToken(turn)).toBe(true);
    turn = reduceFrame(turn, chat({ event: "tool_start", tool_name: "x" }), 1);
    expect(isAwaitingFirstToken(turn)).toBe(true);
    turn = reduceFrame(turn, chat({ event: "text_delta", delta: "yo" }), 2);
    expect(isAwaitingFirstToken(turn)).toBe(false);
  });

  test("drops empty settled assistant rows from the render list", () => {
    const turn = run([
      chat({ event: "message_start" }),
      chat({ event: "message_end" }),
    ]);
    const rows = deriveRows([], turn);
    expect(
      rows.some((row) => row.kind === "message" && row.role === "assistant"),
    ).toBe(false);
  });

  test("empty turn renders nothing", () => {
    expect(deriveRows([], emptyTurn())).toEqual([]);
  });
});
