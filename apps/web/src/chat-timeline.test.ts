import { describe, expect, test } from "vitest";

import type { ChatFrame } from "./chat-bus";
import {
  deriveRows,
  emptyTurn,
  isAwaitingFirstToken,
  reduceFrame,
  stabilizeRows,
  startTurn,
  type LiveTurn,
  type TimelineRow,
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

  test("live reasoning stays expanded while generating, compacts when done", () => {
    const turn = run([
      chat({ event: "message_start" }),
      chat({ event: "thinking_delta", delta: { text: "mulling" } }),
    ]);
    const streaming = deriveRows([], turn).find(
      (row) => row.kind === "reasoning",
    );
    expect(streaming).toMatchObject({ text: "mulling", done: false });

    const finished = run([chat({ event: "agent_end" })], turn);
    const compacted = deriveRows([], finished).find(
      (row) => row.kind === "reasoning",
    );
    expect(compacted).toMatchObject({ text: "mulling", done: true });
  });

  test("stored reasoning history renders as a compacted reasoning row", () => {
    const rows = deriveRows(
      [{ id: "r1", role: "reasoning", content: "earlier thought" }],
      emptyTurn(),
    );
    expect(rows).toEqual([
      {
        kind: "reasoning",
        id: "r1",
        text: "earlier thought",
        streaming: false,
        done: true,
      },
    ]);
  });

  test("stored tool history renders as an expandable, done tool row with args/result", () => {
    const rows = deriveRows(
      [
        {
          id: "t1",
          role: "tool",
          content: "search",
          toolName: "search",
          toolArgs: { q: "needle" },
          toolResult: { hits: 3 },
        },
      ],
      emptyTurn(),
    );
    expect(rows).toEqual([
      {
        kind: "tool",
        id: "t1",
        toolName: "search",
        status: "done",
        args: { q: "needle" },
        result: { hits: 3 },
      },
    ]);
  });

  test("stored tool history without persisted args/result still renders an expandable tool row", () => {
    const rows = deriveRows(
      [{ id: "t2", role: "tool", content: "capture", toolName: "capture" }],
      emptyTurn(),
    );
    expect(rows).toEqual([
      {
        kind: "tool",
        id: "t2",
        toolName: "capture",
        status: "done",
        args: null,
        result: null,
      },
    ]);
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

  test("carries tool args from start and result from end into the row", () => {
    const turn = run([
      chat({
        event: "tool_start",
        tool_name: "search",
        tool_id: "t1",
        tool_args: { q: "needle", limit: 5 },
      }),
      chat({
        event: "tool_end",
        tool_name: "search",
        tool_id: "t1",
        tool_result: { kind: "collection", hits: 3 },
      }),
    ]);
    const tool = deriveRows([], turn).find((row) => row.kind === "tool");
    expect(tool).toMatchObject({
      toolName: "search",
      status: "done",
      args: { q: "needle", limit: 5 },
      result: { kind: "collection", hits: 3 },
    });
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

describe("stabilizeRows", () => {
  const settled: TimelineRow = {
    kind: "message",
    id: "m1",
    role: "user",
    text: "hi",
    toolName: null,
    streaming: false,
  };

  test("reuses the prior object reference when a row's content is unchanged", () => {
    const first = stabilizeRows([], [settled]);
    // A fresh object with identical fields, as `deriveRows` would produce on
    // the next unrelated frame.
    const rederived: TimelineRow = { ...settled };
    const second = stabilizeRows(first, [rederived]);
    expect(second[0]).toBe(first[0]);
    expect(second[0]).not.toBe(rederived);
  });

  test("uses the new object when a row's content actually changed", () => {
    const running: TimelineRow = {
      kind: "tool",
      id: "t1",
      toolName: "search",
      status: "running",
      args: null,
      result: null,
    };
    const first = stabilizeRows([], [running]);
    const done: TimelineRow = { ...running, status: "done", result: "hit" };
    const second = stabilizeRows(first, [done]);
    expect(second[0]).toBe(done);
    expect(second[0]).not.toBe(first[0]);
  });

  test("does not confuse rows with the same id but a different kind", () => {
    const asMessage: TimelineRow = {
      kind: "message",
      id: "x",
      role: "assistant",
      text: "hi",
      toolName: null,
      streaming: false,
    };
    const asTool: TimelineRow = {
      kind: "tool",
      id: "x",
      toolName: "search",
      status: "done",
      args: null,
      result: null,
    };
    const first = stabilizeRows([], [asMessage]);
    const second = stabilizeRows(first, [asTool]);
    expect(second[0]).toBe(asTool);
  });

  test("leaves brand-new rows (no prior id match) untouched", () => {
    const result = stabilizeRows([], [settled]);
    expect(result[0]).toBe(settled);
  });
});
