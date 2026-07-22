// Normalization seam: turn raw pi chat frames into an ordered, typed timeline
// for the current in-flight turn. The UI renders from these rows instead of
// parsing wire payloads, so transport and rendering can change independently.
// Mirrors t3code's session-logic seam, scoped to Tether's single live turn.

import type { ChatFrame } from "./chat-bus";

export type ChatRole = "user" | "assistant" | "tool";

// Settled rows can also carry persisted reasoning, which renders as its own
// collapsible row rather than a chat bubble.
export type StoredRole = ChatRole | "reasoning";

export interface StoredMessage {
  id: string;
  role: StoredRole;
  content: string;
  toolName?: string | null;
  toolArgs?: unknown;
  toolResult?: unknown;
}

export type TimelineRow =
  | {
      kind: "message";
      id: string;
      role: ChatRole;
      text: string;
      toolName: string | null;
      streaming: boolean;
    }
  | {
      kind: "reasoning";
      id: string;
      text: string;
      streaming: boolean;
      // True once the producing turn has finished: the cue to compact the
      // trace. Live reasoning stays expanded (`done: false`) while generating.
      done: boolean;
    }
  | {
      kind: "tool";
      id: string;
      toolName: string;
      status: "running" | "done";
      args: unknown;
      result: unknown;
    };

type LiveRow =
  | {
      kind: "assistant";
      id: string;
      messageIndex: number;
      text: string;
      streaming: boolean;
    }
  | {
      kind: "reasoning";
      id: string;
      messageIndex: number;
      text: string;
      streaming: boolean;
    }
  | {
      kind: "tool";
      id: string;
      toolId: string | null;
      toolName: string;
      status: "running" | "done";
      args: unknown;
      result: unknown;
    };

export interface LiveTurn {
  generating: boolean;
  stopped: boolean;
  startedAt: number | null;
  endedAt: number | null;
  error: string | null;
  userText: string | null;
  messageIndex: number;
  counter: number;
  rows: LiveRow[];
}

export function emptyTurn(): LiveTurn {
  return {
    generating: false,
    stopped: false,
    startedAt: null,
    endedAt: null,
    error: null,
    userText: null,
    messageIndex: -1,
    counter: 0,
    rows: [],
  };
}

// Begin a fresh turn from a user prompt: optimistic user bubble plus a clean
// slate for streamed segments. Errors and the "stopped" flag reset so a retry
// after a failure does not inherit the previous turn's banners.
export function startTurn(text: string, now: number): LiveTurn {
  return {
    ...emptyTurn(),
    generating: true,
    startedAt: now,
    userText: text,
  };
}

function deltaText(delta: unknown): string {
  if (typeof delta === "string") {
    return delta;
  }
  if (typeof delta === "object" && delta !== null && "text" in delta) {
    const text = (delta as { text?: unknown }).text;
    return typeof text === "string" ? text : "";
  }
  return "";
}

function mintId(turn: LiveTurn): { id: string; counter: number } {
  const counter = turn.counter + 1;
  return { id: `live-${counter.toString()}`, counter };
}

function upsertStreamRow(
  turn: LiveTurn,
  kind: "assistant" | "reasoning",
  delta: string,
): LiveTurn {
  const rows = [...turn.rows];
  const index = rows.findIndex(
    (row) => row.kind === kind && row.messageIndex === turn.messageIndex,
  );
  if (index >= 0) {
    const existing = rows[index] as Extract<
      LiveRow,
      { kind: "assistant" | "reasoning" }
    >;
    rows[index] = { ...existing, text: existing.text + delta, streaming: true };
    return { ...turn, rows };
  }
  const { id, counter } = mintId(turn);
  rows.push({
    kind,
    id,
    messageIndex: Math.max(turn.messageIndex, 0),
    text: delta,
    streaming: true,
  });
  return { ...turn, counter, rows };
}

function settleStreamRows(turn: LiveTurn): LiveTurn {
  const rows = turn.rows.map((row) =>
    (row.kind === "assistant" || row.kind === "reasoning") &&
    row.messageIndex === turn.messageIndex
      ? { ...row, streaming: false }
      : row,
  );
  return { ...turn, rows };
}

function startTool(turn: LiveTurn, frame: ChatFrame): LiveTurn {
  if (frame.type !== "chat") {
    return turn;
  }
  const { id, counter } = mintId(turn);
  const rows: LiveRow[] = [
    ...turn.rows,
    {
      kind: "tool",
      id,
      toolId: frame.tool_id ?? null,
      toolName: frame.tool_name ?? "tool",
      status: "running",
      args: frame.tool_args ?? null,
      result: null,
    },
  ];
  return { ...turn, counter, rows };
}

function endTool(turn: LiveTurn, frame: ChatFrame): LiveTurn {
  if (frame.type !== "chat") {
    return turn;
  }
  const rows = [...turn.rows];
  // Prefer matching by the host-provided tool id; fall back to the most recent
  // still-running tool so a missing id never strands a spinner.
  let index = -1;
  if (frame.tool_id != null) {
    index = rows.findIndex(
      (row) => row.kind === "tool" && row.toolId === frame.tool_id,
    );
  }
  if (index < 0) {
    for (let cursor = rows.length - 1; cursor >= 0; cursor -= 1) {
      const row = rows[cursor];
      if (row.kind === "tool" && row.status === "running") {
        index = cursor;
        break;
      }
    }
  }
  if (index < 0) {
    return turn;
  }
  const tool = rows[index] as Extract<LiveRow, { kind: "tool" }>;
  rows[index] = {
    ...tool,
    status: "done",
    toolName: frame.tool_name ?? tool.toolName,
    result: frame.tool_result ?? tool.result,
  };
  return { ...turn, rows };
}

// Fold one wire frame into the live turn. Pure: returns a new turn so callers
// can store it in a signal and let fine-grained reactivity diff the rows.
export function reduceFrame(
  turn: LiveTurn,
  frame: ChatFrame,
  now: number,
): LiveTurn {
  if (frame.type !== "chat") {
    return turn;
  }
  switch (frame.event) {
    case "message_start":
      return { ...turn, messageIndex: turn.messageIndex + 1 };
    case "text_start":
    case "text_delta":
      return upsertStreamRow(turn, "assistant", deltaText(frame.delta));
    case "thinking_start":
    case "thinking_delta":
      return upsertStreamRow(turn, "reasoning", deltaText(frame.delta));
    case "text_end":
    case "thinking_end":
    case "message_end":
      return settleStreamRows(turn);
    case "tool_start":
      return startTool(turn, frame);
    case "tool_end":
      return endTool(turn, frame);
    case "error":
      return {
        ...turn,
        generating: false,
        error: frame.detail ?? "Chat error",
        endedAt: now,
      };
    case "abort_ack":
      return { ...turn, generating: false, stopped: true, endedAt: now };
    case "agent_end":
      return { ...turn, generating: false, endedAt: now };
    default:
      return turn;
  }
}

// Merge settled history with the live turn into a flat render list. Stored rows
// come first; the optimistic user bubble and streamed segments follow in order.
export function deriveRows(
  stored: readonly StoredMessage[],
  turn: LiveTurn,
): TimelineRow[] {
  const rows: TimelineRow[] = stored.map((message) => {
    if (message.role === "reasoning") {
      return {
        kind: "reasoning",
        id: message.id,
        text: message.content,
        streaming: false,
        // Settled history is always a finished turn, so it renders compact.
        done: true,
      };
    }
    if (message.role === "tool") {
      // Route settled tool rows through the same "tool" kind a live turn uses,
      // so history keeps the expandable arguments/result disclosure instead
      // of collapsing to a bare "used X" line once persisted (see
      // `MessageRow`'s "tool" branch in chat-page.tsx).
      return {
        kind: "tool",
        id: message.id,
        toolName: message.toolName ?? message.content,
        status: "done",
        args: message.toolArgs ?? null,
        result: message.toolResult ?? null,
      };
    }
    return {
      kind: "message",
      id: message.id,
      role: message.role,
      text: message.content,
      toolName: message.toolName ?? null,
      streaming: false,
    };
  });
  if (turn.userText !== null) {
    rows.push({
      kind: "message",
      id: "live-user",
      role: "user",
      text: turn.userText,
      toolName: null,
      streaming: false,
    });
  }
  for (const row of turn.rows) {
    if (row.kind === "tool") {
      rows.push({
        kind: "tool",
        id: row.id,
        toolName: row.toolName,
        status: row.status,
        args: row.args,
        result: row.result,
      });
      continue;
    }
    if (row.text.length === 0 && !row.streaming) {
      continue;
    }
    if (row.kind === "reasoning") {
      rows.push({
        kind: "reasoning",
        id: row.id,
        text: row.text,
        streaming: row.streaming,
        // Stay expanded for the duration of the turn; compact once it ends.
        done: !turn.generating,
      });
      continue;
    }
    rows.push({
      kind: "message",
      id: row.id,
      role: "assistant",
      text: row.text,
      toolName: null,
      streaming: row.streaming,
    });
  }
  return rows;
}

function rowContentEqual(a: TimelineRow, b: TimelineRow): boolean {
  if (a.kind !== b.kind) {
    return false;
  }
  switch (a.kind) {
    case "message": {
      const other = b as Extract<TimelineRow, { kind: "message" }>;
      return (
        a.role === other.role &&
        a.text === other.text &&
        a.toolName === other.toolName &&
        a.streaming === other.streaming
      );
    }
    case "reasoning": {
      const other = b as Extract<TimelineRow, { kind: "reasoning" }>;
      return (
        a.text === other.text &&
        a.streaming === other.streaming &&
        a.done === other.done
      );
    }
    case "tool": {
      const other = b as Extract<TimelineRow, { kind: "tool" }>;
      return (
        a.toolName === other.toolName &&
        a.status === other.status &&
        a.args === other.args &&
        a.result === other.result
      );
    }
  }
}

// `deriveRows` rebuilds the *entire* row list — settled history and all —
// every time it runs, which is every single streamed token or tool event.
// Rendered as-is, that hands `<For>` a brand-new object for every row on
// every frame, so it tears down and remounts the whole transcript (hundreds
// of settled messages included) each time a delta arrives: visible
// flicker/scroll-jitter, and any DOM-local state — like an expanded tool-call
// `<details>` — gets wiped the instant something unrelated streams in.
//
// This reconciles a freshly derived row list against the previously rendered
// one by id, keeping the *same object reference* for any row whose rendered
// content hasn't actually changed. `<For>` diffs by reference, so untouched
// rows (which is almost all of them, most of the time) keep their DOM node —
// and whatever local state it holds — completely undisturbed.
export function stabilizeRows(
  previous: readonly TimelineRow[],
  next: readonly TimelineRow[],
): TimelineRow[] {
  const priorById = new Map(previous.map((row) => [row.id, row]));
  return next.map((row) => {
    const prior = priorById.get(row.id);
    return prior !== undefined && rowContentEqual(prior, row) ? prior : row;
  });
}

// True once a turn is running but has produced no visible assistant text yet —
// the cue for a "working…" indicator instead of an empty bubble.
export function isAwaitingFirstToken(turn: LiveTurn): boolean {
  if (!turn.generating) {
    return false;
  }
  return !turn.rows.some(
    (row) => row.kind === "assistant" && row.text.length > 0,
  );
}
