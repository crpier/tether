import type { ConversationSummary, ToolPart } from "@kitn.ai/ui/solid";

import { conversationLabel, type Conversation } from "./host/chat";
import type { TimelineRow } from "./live-chat-turn";

type MessageRole = "assistant" | "system" | "user";

type MessageRow = Extract<TimelineRow, { kind: "message" }>;
type ReasoningRow = Extract<TimelineRow, { kind: "reasoning" }>;
type ToolRow = Extract<TimelineRow, { kind: "tool" }>;

export type KitnTimelineItem =
  | {
      id: string;
      kind: "session-boundary";
    }
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
        : row.role === "health"
          ? "Health"
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

export function conversationHref(conversation: Conversation): string {
  return conversation.kind === "main" ? "/chat" : `/chat/${conversation.id}`;
}

export function projectConversations(
  conversations: readonly Conversation[],
): ConversationSummary[] {
  return conversations
    .filter((conversation) => conversation.status === "active")
    .map((conversation) => {
      const statuses: string[] = [];
      if (
        conversation.pending_turn_count > 0 ||
        conversation.running_turn_id !== null
      ) {
        statuses.push("Working");
      }
      if (conversation.has_unread) {
        statuses.push("Unread");
      }
      return {
        id: conversation.id,
        messageCount: conversation.latest_message_seq,
        title: conversationLabel(conversation, conversations),
        trailing: statuses.length > 0 ? statuses.join(" · ") : undefined,
        updatedAt: conversation.latest_activity ?? conversation.created_at,
      };
    });
}

export function projectTimelineRows(
  rows: TimelineRow[],
  options?: { sessionGapSeconds?: number },
): KitnTimelineItem[] {
  const projected: KitnTimelineItem[] = [];
  let previousCreatedAt: number | undefined;
  for (const row of rows) {
    const createdAt =
      row.createdAt === undefined ? undefined : Date.parse(row.createdAt);
    const startsTurn =
      row.kind === "message" &&
      (row.role === "user" ||
        row.role === "health" ||
        row.role === "scheduled");
    if (
      startsTurn &&
      previousCreatedAt !== undefined &&
      createdAt !== undefined &&
      Number.isFinite(createdAt) &&
      options?.sessionGapSeconds !== undefined &&
      createdAt - previousCreatedAt >= options.sessionGapSeconds * 1000
    ) {
      projected.push({
        id: `session-boundary-${row.id}`,
        kind: "session-boundary",
      });
    }
    if (createdAt !== undefined && Number.isFinite(createdAt)) {
      previousCreatedAt = createdAt;
    }
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
