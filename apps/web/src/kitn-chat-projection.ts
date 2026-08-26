import type { ToolPart } from "@kitn.ai/ui/solid";

import type { TimelineRow } from "./live-chat-turn";

type MessageRole = "assistant" | "system" | "user";

type MessageRow = Extract<TimelineRow, { kind: "message" }>;
type ReasoningRow = Extract<TimelineRow, { kind: "reasoning" }>;
type ToolRow = Extract<TimelineRow, { kind: "tool" }>;

export type KitnTimelineItem =
  | {
      ariaLabel: string;
      id: string;
      kind: "message";
      message: MessageRow;
      role: MessageRole;
    }
  | {
      id: string;
      kind: "reasoning";
      reasoning: ReasoningRow;
    }
  | {
      id: string;
      kind: "tool-group";
      tools: { row: ToolRow; toolPart: ToolPart }[];
    };

function recordOf(value: unknown): Record<string, unknown> | undefined {
  if (value === null || value === undefined) {
    return undefined;
  }
  return typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : { value };
}

function projectTool(row: ToolRow): { row: ToolRow; toolPart: ToolPart } {
  return {
    row,
    toolPart: {
      input: recordOf(row.args),
      ...(row.status === "done" ? { output: recordOf(row.result) } : {}),
      state: row.status === "running" ? "input-streaming" : "output-available",
      toolCallId: row.id,
      type: row.toolName,
    },
  };
}

function projectMessage(row: MessageRow): KitnTimelineItem {
  const role: MessageRole =
    row.role === "user"
      ? "user"
      : row.role === "assistant"
        ? "assistant"
        : "system";
  const label =
    row.role === "user"
      ? "You"
      : row.role === "assistant"
        ? "Tether"
        : row.role === "scheduled"
          ? "Scheduled"
          : "Tool";
  return {
    ariaLabel: `${label} message`,
    id: row.id,
    kind: "message",
    message: row,
    role,
  };
}

export function projectTimelineRows(rows: TimelineRow[]): KitnTimelineItem[] {
  const projected: KitnTimelineItem[] = [];
  for (const row of rows) {
    if (row.kind === "tool") {
      const previous = projected.at(-1);
      if (previous?.kind === "tool-group") {
        previous.tools.push(projectTool(row));
      } else {
        projected.push({
          id: `tool-group-${row.id}`,
          kind: "tool-group",
          tools: [projectTool(row)],
        });
      }
      continue;
    }
    projected.push(
      row.kind === "message"
        ? projectMessage(row)
        : { id: row.id, kind: "reasoning", reasoning: row },
    );
  }
  return projected;
}
