// PROTOTYPE #246 — throwaway, do not ship
//
// Local mock types for the prototype only. Deliberately NOT reusing the
// generated `api.ts` schema types — this is throwaway UI, not a real
// integration, and inventing simpler shapes here keeps mock data easy to
// hand-write and keeps this directory fully decoupled from the real app.

export type ProtoPage = "chat" | "proposals" | "inbox" | "browse" | "settings";
export type ProtoVariant = "A" | "B" | "C";

// ---- Proposals -------------------------------------------------------

export type ActionKind = "archive" | "label" | "trash" | "unsubscribe";

export interface MockProposalAction {
  id: string;
  kind: ActionKind;
  display: string; // human-readable summary, e.g. "Archive 14 promo emails"
  sender: string;
  subject: string;
  ageDays: number;
  count: number; // how many messages this action covers
}

export interface MockProposal {
  id: string;
  title: string;
  summary: string;
  createdAt: string; // ISO
  category: string; // e.g. "gmail-purge"
  confidence: number; // 0-1, calibration score
  actions: MockProposalAction[];
}

export type ProposalHistoryState =
  "approved" | "executed" | "rejected" | "failed";

export interface MockProposalHistoryEntry {
  id: string;
  title: string;
  decidedAt: string;
  state: ProposalHistoryState;
  actionCount: number;
}

export interface MockGrant {
  id: string;
  kind: string;
  scope: string | null;
  grantedAt: string;
  autoApproveUnder: number; // count threshold
}

export interface MockGrantSuggestion {
  id: string;
  kind: string;
  scope: string | null;
  reason: string;
  approvalRate: number; // 0-1 over the sample
  sampleSize: number;
}

// ---- Inbox -------------------------------------------------------

export interface MockMemoryReviewItem {
  id: string;
  text: string;
  provenance: string; // e.g. "from chat, 2026-07-18"
  confidence: number;
}

export interface MockBucketTriageItem {
  id: string;
  raw: string;
  suggestedType: "todo" | "memory" | "reminder" | "discard";
  capturedAt: string;
}

export interface MockRecallPrompt {
  id: string;
  question: string;
  dueAt: string;
  memoryText: string;
}

export interface MockFiredReminder {
  id: string;
  title: string;
  firedAt: string;
  detail: string;
}

// ---- Browse -------------------------------------------------------

export interface MockCorpusMemory {
  id: string;
  text: string;
  state: "active" | "archived";
  createdAt: string;
}

export interface MockTodo {
  id: string;
  title: string;
  status: "ready" | "waiting";
  waitingOn: string | null;
}

export interface MockTrigger {
  id: string;
  label: string;
  recurrence: string;
  nextFireAt: string;
}

// ---- Chat -------------------------------------------------------

export interface MockChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  tableWidget?: { columns: string[]; rows: string[][] };
}
