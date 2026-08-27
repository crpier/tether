import { describe, expect, test } from "vitest";

import { projectTimelineRows } from "./kitn-chat-projection";
import type { TimelineRow } from "./live-chat-turn";

describe("Kitn chat projection", () => {
  test("preserves host identities while mapping transcript roles and tool state", () => {
    const rows: TimelineRow[] = [
      {
        id: "message-user-1",
        kind: "message",
        role: "user",
        streaming: false,
        text: "Keep this identity",
        toolName: null,
      },
      {
        done: false,
        id: "reasoning-1",
        kind: "reasoning",
        streaming: true,
        text: "Checking records",
      },
      {
        args: { query: "receipt" },
        id: "tool-1",
        kind: "tool",
        result: null,
        status: "running",
        toolName: "search_gmail",
      },
      {
        args: { id: "abc" },
        id: "tool-2",
        kind: "tool",
        result: { ok: true },
        status: "done",
        toolName: "archive_gmail_message",
      },
      {
        id: "message-assistant-1",
        kind: "message",
        role: "assistant",
        streaming: false,
        text: "Done",
        toolName: null,
      },
    ];

    expect(projectTimelineRows(rows)).toEqual([
      {
        ariaLabel: "You message",
        id: "message-user-1",
        kind: "message",
        message: rows[0],
        role: "user",
      },
      {
        id: "reasoning-1",
        kind: "reasoning",
        reasoning: rows[1],
      },
      {
        id: "tool-group-tool-1",
        kind: "tool-group",
        tools: [
          {
            row: rows[2],
            toolPart: {
              input: { query: "receipt" },
              state: "input-streaming",
              toolCallId: "tool-1",
              type: "search_gmail",
            },
          },
          {
            row: rows[3],
            toolPart: {
              input: { id: "abc" },
              output: { ok: true },
              state: "output-available",
              toolCallId: "tool-2",
              type: "archive_gmail_message",
            },
          },
        ],
      },
      {
        ariaLabel: "Tether message",
        id: "message-assistant-1",
        kind: "message",
        message: rows[4],
        role: "assistant",
      },
    ]);
  });

  test("inserts a Pi session boundary before the first turn after a cold gap", () => {
    const rows: TimelineRow[] = [
      {
        createdAt: "2026-01-01T00:00:00Z",
        id: "assistant-old",
        kind: "message",
        role: "assistant",
        streaming: false,
        text: "Earlier answer",
        toolName: null,
        turnId: "turn-old",
      },
      {
        createdAt: "2026-01-01T00:10:00Z",
        id: "user-new",
        kind: "message",
        role: "user",
        streaming: false,
        text: "New question",
        toolName: null,
        turnId: "turn-new",
      },
    ];

    expect(projectTimelineRows(rows, { sessionGapSeconds: 300 })).toEqual([
      expect.objectContaining({ id: "assistant-old", kind: "message" }),
      {
        id: "session-boundary-user-new",
        kind: "session-boundary",
      },
      expect.objectContaining({ id: "user-new", kind: "message" }),
    ]);
  });

  test("maps scheduled prompts to Kitn system rows without losing Tether role", () => {
    const row: TimelineRow = {
      id: "scheduled-1",
      kind: "message",
      role: "scheduled",
      streaming: false,
      text: "Daily review",
      toolName: null,
    };

    expect(projectTimelineRows([row])).toEqual([
      {
        ariaLabel: "Scheduled message",
        id: "scheduled-1",
        kind: "message",
        message: row,
        role: "system",
      },
    ]);
  });
});
