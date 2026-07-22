// PROTOTYPE #246 — throwaway, do not ship
//
// Hand-authored mock data, deliberately dense — the whole point of this
// prototype is judging layouts under realistic item counts, not empty states.

import type {
  MockBucketTriageItem,
  MockChatMessage,
  MockCorpusMemory,
  MockFiredReminder,
  MockGrant,
  MockGrantSuggestion,
  MockMemoryReviewItem,
  MockProposal,
  MockProposalHistoryEntry,
  MockRecallPrompt,
  MockTodo,
  MockTrigger,
} from "./types";

export const mockProposals: MockProposal[] = [
  {
    actions: [
      {
        ageDays: 41,
        count: 23,
        display: "Archive 23 LinkedIn notification emails",
        id: "a1",
        kind: "archive",
        sender: "notifications@linkedin.com",
        subject: "You have 3 new connections",
      },
      {
        ageDays: 41,
        count: 23,
        display: "Label as Social",
        id: "a2",
        kind: "label",
        sender: "notifications@linkedin.com",
        subject: "You have 3 new connections",
      },
    ],
    category: "gmail-purge",
    confidence: 0.94,
    createdAt: "2026-07-21T09:12:00Z",
    id: "p1",
    summary: "23 LinkedIn notification emails older than 30 days, none opened.",
    title: "Archive stale LinkedIn notifications",
  },
  {
    actions: [
      {
        ageDays: 62,
        count: 11,
        display: "Trash 11 expired Groupon deals",
        id: "a3",
        kind: "trash",
        sender: "deals@groupon.com",
        subject: "50% off — ends tonight!",
      },
    ],
    category: "gmail-purge",
    confidence: 0.97,
    createdAt: "2026-07-21T09:12:04Z",
    id: "p2",
    summary: "Groupon promo emails whose deal windows have already closed.",
    title: "Trash expired Groupon deals",
  },
  {
    actions: [
      {
        ageDays: 8,
        count: 4,
        display: "Archive 4 GitHub CI notification emails",
        id: "a4",
        kind: "archive",
        sender: "notifications@github.com",
        subject: "[tether] Workflow run failed",
      },
      {
        ageDays: 8,
        count: 4,
        display: "Label as CI",
        id: "a5",
        kind: "label",
        sender: "notifications@github.com",
        subject: "[tether] Workflow run failed",
      },
    ],
    category: "gmail-purge",
    confidence: 0.72,
    createdAt: "2026-07-21T09:12:09Z",
    id: "p3",
    summary:
      "CI failure emails for a branch that was merged 6 days ago — likely stale.",
    title: "Archive stale CI failure notifications",
  },
  {
    actions: [
      {
        ageDays: 19,
        count: 7,
        display: "Unsubscribe from Medium Daily Digest",
        id: "a6",
        kind: "unsubscribe",
        sender: "noreply@medium.com",
        subject: "Today's highlights for you",
      },
      {
        ageDays: 19,
        count: 7,
        display: "Archive 7 Medium digest emails",
        id: "a7",
        kind: "archive",
        sender: "noreply@medium.com",
        subject: "Today's highlights for you",
      },
    ],
    category: "gmail-purge",
    confidence: 0.88,
    createdAt: "2026-07-21T09:12:14Z",
    id: "p4",
    summary: "Unopened Medium digests, 7 in a row unread over 19 days.",
    title: "Unsubscribe + archive Medium digests",
  },
  {
    actions: [
      {
        ageDays: 3,
        count: 1,
        display: "Label as Travel",
        id: "a8",
        kind: "label",
        sender: "confirmations@united.com",
        subject: "Your itinerary for UA 1882",
      },
    ],
    category: "gmail-purge",
    confidence: 0.55,
    createdAt: "2026-07-21T09:12:19Z",
    id: "p5",
    summary: "Recent flight confirmation — low confidence this needs action.",
    title: "Label flight confirmation as Travel",
  },
  {
    actions: [
      {
        ageDays: 90,
        count: 34,
        display: "Trash 34 Steam sale emails",
        id: "a9",
        kind: "trash",
        sender: "noreply@steampowered.com",
        subject: "Summer Sale — up to 80% off",
      },
    ],
    category: "gmail-purge",
    confidence: 0.99,
    createdAt: "2026-07-21T09:12:24Z",
    id: "p6",
    summary: "Steam sale spam, sale ended 60+ days ago, zero opens.",
    title: "Trash old Steam sale emails",
  },
  {
    actions: [
      {
        ageDays: 27,
        count: 2,
        display: "Archive 2 bank statement emails",
        id: "a10",
        kind: "archive",
        sender: "estatements@chase.com",
        subject: "Your July statement is ready",
      },
      {
        ageDays: 27,
        count: 2,
        display: "Label as Finance",
        id: "a11",
        kind: "label",
        sender: "estatements@chase.com",
        subject: "Your July statement is ready",
      },
    ],
    category: "gmail-purge",
    confidence: 0.81,
    createdAt: "2026-07-21T09:12:29Z",
    id: "p7",
    summary: "Statement notifications already read and downloaded.",
    title: "Archive + label bank statements",
  },
  {
    actions: [
      {
        ageDays: 5,
        count: 1,
        display: "Trash 1 phishing-flagged email",
        id: "a12",
        kind: "trash",
        sender: "security-alert@paypa1-secure.com",
        subject: "Your account has been limited",
      },
    ],
    category: "gmail-purge",
    confidence: 0.91,
    createdAt: "2026-07-21T09:12:34Z",
    id: "p8",
    summary: "Lookalike domain, matches known phishing pattern.",
    title: "Trash suspected phishing email",
  },
  {
    actions: [
      {
        ageDays: 45,
        count: 9,
        display: "Archive 9 Slack digest emails",
        id: "a13",
        kind: "archive",
        sender: "feedback@slack.com",
        subject: "You have unread messages in #general",
      },
    ],
    category: "gmail-purge",
    confidence: 0.85,
    createdAt: "2026-07-21T09:12:39Z",
    id: "p9",
    summary: "Slack email digests, all messages already read in-app.",
    title: "Archive Slack email digests",
  },
  {
    actions: [
      {
        ageDays: 12,
        count: 15,
        display: "Unsubscribe from Product Hunt Daily",
        id: "a14",
        kind: "unsubscribe",
        sender: "hello@producthunt.com",
        subject: "Today's top products",
      },
      {
        ageDays: 12,
        count: 15,
        display: "Trash 15 Product Hunt digests",
        id: "a15",
        kind: "trash",
        sender: "hello@producthunt.com",
        subject: "Today's top products",
      },
    ],
    category: "gmail-purge",
    confidence: 0.79,
    createdAt: "2026-07-21T09:12:44Z",
    id: "p10",
    summary: "15 consecutive unopened Product Hunt digests.",
    title: "Unsubscribe + trash Product Hunt digests",
  },
  {
    actions: [
      {
        ageDays: 70,
        count: 6,
        display: "Trash 6 abandoned-cart emails",
        id: "a16",
        kind: "trash",
        sender: "cart@zappos.com",
        subject: "You left something in your cart",
      },
    ],
    category: "gmail-purge",
    confidence: 0.96,
    createdAt: "2026-07-21T09:12:49Z",
    id: "p11",
    summary: "Abandoned cart nags, cart was cleared weeks ago.",
    title: "Trash abandoned-cart nag emails",
  },
  {
    actions: [
      {
        ageDays: 2,
        count: 1,
        display: "Label as Receipts",
        id: "a17",
        kind: "label",
        sender: "receipts@uber.com",
        subject: "Your Tuesday morning trip",
      },
      {
        ageDays: 2,
        count: 1,
        display: "Archive 1 Uber receipt",
        id: "a18",
        kind: "archive",
        sender: "receipts@uber.com",
        subject: "Your Tuesday morning trip",
      },
    ],
    category: "gmail-purge",
    confidence: 0.6,
    createdAt: "2026-07-21T09:12:54Z",
    id: "p12",
    summary: "Recent receipt — routine filing, low urgency.",
    title: "File + archive Uber receipt",
  },
];

export const mockProposalHistory: MockProposalHistoryEntry[] = [
  {
    actionCount: 3,
    decidedAt: "2026-07-20T14:02:00Z",
    id: "h1",
    state: "executed",
    title: "Archive old Amazon order confirmations",
  },
  {
    actionCount: 1,
    decidedAt: "2026-07-19T08:44:00Z",
    id: "h2",
    state: "rejected",
    title: "Trash newsletter from a domain expert follows",
  },
  {
    actionCount: 7,
    decidedAt: "2026-07-18T19:15:00Z",
    id: "h3",
    state: "executed",
    title: "Unsubscribe from expired conference CFPs",
  },
  {
    actionCount: 2,
    decidedAt: "2026-07-17T11:30:00Z",
    id: "h4",
    state: "failed",
    title: "Label + archive Delta boarding passes",
  },
  {
    actionCount: 12,
    decidedAt: "2026-07-16T07:05:00Z",
    id: "h5",
    state: "executed",
    title: "Trash duplicate password-reset emails",
  },
  {
    actionCount: 1,
    decidedAt: "2026-07-15T16:40:00Z",
    id: "h6",
    state: "approved",
    title: "Archive Twitch subscription renewal notice",
  },
];

export const mockGrants: MockGrant[] = [
  {
    autoApproveUnder: 20,
    grantedAt: "2026-06-01T00:00:00Z",
    id: "g1",
    kind: "archive",
    scope: "sender:notifications@github.com",
  },
  {
    autoApproveUnder: 5,
    grantedAt: "2026-06-10T00:00:00Z",
    id: "g2",
    kind: "trash",
    scope: "category:expired-deal",
  },
];

export const mockGrantSuggestions: MockGrantSuggestion[] = [
  {
    approvalRate: 1,
    id: "s1",
    kind: "archive",
    reason: "Every LinkedIn notification proposal for 30 days was approved.",
    sampleSize: 9,
    scope: "sender:notifications@linkedin.com",
  },
  {
    approvalRate: 0.92,
    id: "s2",
    kind: "trash",
    reason: "Steam sale purges approved 11 of 12 times.",
    sampleSize: 12,
    scope: "sender:noreply@steampowered.com",
  },
];

// ---- Inbox -------------------------------------------------------

export const mockMemoryReviewItems: MockMemoryReviewItem[] = [
  {
    confidence: 0.62,
    id: "m1",
    provenance: "extracted from chat, 2026-07-18",
    text: "Prefers window seats on flights over 3 hours.",
  },
  {
    confidence: 0.88,
    id: "m2",
    provenance: "extracted from chat, 2026-07-19",
    text: "Working on issue #246 — UI rethink for Tether.",
  },
  {
    confidence: 0.4,
    id: "m3",
    provenance: "extracted from voice note, 2026-07-20",
    text: "Might be allergic to shellfish — mentioned in passing, unconfirmed.",
  },
  {
    confidence: 0.95,
    id: "m4",
    provenance: "extracted from chat, 2026-07-15",
    text: "Uses snektest for all Python tests in this repo.",
  },
  {
    confidence: 0.55,
    id: "m5",
    provenance: "extracted from chat, 2026-07-12",
    text: "Considering switching from Chase to a credit union.",
  },
  {
    confidence: 0.77,
    id: "m6",
    provenance: "extracted from recall answer, 2026-07-21",
    text: "Weekly review happens Sunday evenings.",
  },
];

export const mockBucketTriageItems: MockBucketTriageItem[] = [
  {
    capturedAt: "2026-07-22T07:10:00Z",
    id: "b1",
    raw: "look into whether snekql supports transactions across multiple statements",
    suggestedType: "todo",
  },
  {
    capturedAt: "2026-07-21T22:41:00Z",
    id: "b2",
    raw: "dentist appointment rescheduled to Aug 4, 10am",
    suggestedType: "reminder",
  },
  {
    capturedAt: "2026-07-21T18:05:00Z",
    id: "b3",
    raw: "idea: badge counts on nav could pulse briefly when they increase",
    suggestedType: "memory",
  },
  {
    capturedAt: "2026-07-20T13:22:00Z",
    id: "b4",
    raw: "random thought while walking, not sure it's worth keeping",
    suggestedType: "discard",
  },
];

export const mockRecallPrompts: MockRecallPrompt[] = [
  {
    dueAt: "2026-07-22T10:00:00Z",
    id: "r1",
    memoryText: "Uses snektest for all Python tests in this repo.",
    question: "Still true — do you still use snektest for Python tests?",
  },
  {
    dueAt: "2026-07-22T14:00:00Z",
    id: "r2",
    memoryText: "Considering switching from Chase to a credit union.",
    question: "Did you end up switching banks?",
  },
  {
    dueAt: "2026-07-23T09:00:00Z",
    id: "r3",
    memoryText: "Prefers window seats on flights over 3 hours.",
    question: "Confirm: window seat preference still holds?",
  },
];

export const mockFiredReminders: MockFiredReminder[] = [
  {
    detail: "Was scheduled for 22 Jul, 09:00 — weekly recurrence.",
    firedAt: "2026-07-22T09:00:00Z",
    id: "f1",
    title: "Stand-up notes review",
  },
  {
    detail: "One-off reminder set 3 days ago.",
    firedAt: "2026-07-22T08:30:00Z",
    id: "f2",
    title: "Renew domain for side project",
  },
];

// ---- Browse -------------------------------------------------------

export const mockCorpus: MockCorpusMemory[] = [
  ...mockMemoryReviewItems.map((m) => ({
    createdAt: "2026-07-18T00:00:00Z",
    id: `corpus-${m.id}`,
    state: "active" as const,
    text: m.text,
  })),
  {
    createdAt: "2026-05-02T00:00:00Z",
    id: "c1",
    state: "active",
    text: "Prefers dark mode in every app.",
  },
  {
    createdAt: "2026-04-11T00:00:00Z",
    id: "c2",
    state: "archived",
    text: "Old job title: Backend Engineer II (promoted since).",
  },
  {
    createdAt: "2026-06-30T00:00:00Z",
    id: "c3",
    state: "active",
    text: "Runs Omarchy on Hyprland as daily driver.",
  },
  {
    createdAt: "2026-03-14T00:00:00Z",
    id: "c4",
    state: "active",
    text: "Coffee, no sugar, oat milk.",
  },
  {
    createdAt: "2026-02-01T00:00:00Z",
    id: "c5",
    state: "archived",
    text: "Was learning Rust — paused in favor of other priorities.",
  },
  {
    createdAt: "2026-07-01T00:00:00Z",
    id: "c6",
    state: "active",
    text: "Repo tether: single-tenant, one host process, local calls.",
  },
];

export const mockTodos: MockTodo[] = [
  {
    id: "t1",
    status: "ready",
    title: "Write ADR for proposal UI shell",
    waitingOn: null,
  },
  {
    id: "t2",
    status: "waiting",
    title: "Merge #246 UI shell once variant chosen",
    waitingOn: "design decision on variant",
  },
  {
    id: "t3",
    status: "ready",
    title: "Update docs/development.md logs section",
    waitingOn: null,
  },
  {
    id: "t4",
    status: "waiting",
    title: "Ship push notification toggle",
    waitingOn: "backend endpoint",
  },
  {
    id: "t5",
    status: "ready",
    title: "Review Gmail purge grant suggestions",
    waitingOn: null,
  },
  {
    id: "t6",
    status: "waiting",
    title: "Add recall due badge to bottom tab bar",
    waitingOn: "nav badge counts finalized",
  },
  {
    id: "t7",
    status: "ready",
    title: "Clean up bucket triage backlog",
    waitingOn: null,
  },
  {
    id: "t8",
    status: "ready",
    title: "Renew domain for side project",
    waitingOn: null,
  },
];

export const mockTriggers: MockTrigger[] = [
  {
    id: "tr1",
    label: "Weekly review prompt",
    nextFireAt: "2026-07-27T18:00:00Z",
    recurrence: "weekly, Sunday 18:00",
  },
  {
    id: "tr2",
    label: "Gmail purge scan",
    nextFireAt: "2026-07-23T09:00:00Z",
    recurrence: "daily, 09:00",
  },
  {
    id: "tr3",
    label: "Stand-up notes review",
    nextFireAt: "2026-07-29T09:00:00Z",
    recurrence: "weekly, Monday 09:00",
  },
  {
    id: "tr4",
    label: "Memory recall batch",
    nextFireAt: "2026-07-22T14:00:00Z",
    recurrence: "twice daily",
  },
  {
    id: "tr5",
    label: "Bucket triage sweep",
    nextFireAt: "2026-07-23T07:00:00Z",
    recurrence: "daily, 07:00",
  },
];

// ---- Chat -------------------------------------------------------

export const mockChatTranscript: MockChatMessage[] = [
  { id: "cm1", role: "user", text: "What's on my plate for today?" },
  {
    id: "cm2",
    role: "assistant",
    text: "You've got 2 fired reminders in the inbox, 3 recall prompts due, and 12 Gmail-purge proposals waiting. Here's the todo breakdown:",
  },
  {
    id: "cm3",
    role: "assistant",
    tableWidget: {
      columns: ["Todo", "Status"],
      rows: [
        ["Write ADR for proposal UI shell", "ready"],
        ["Update docs/development.md logs section", "ready"],
        ["Review Gmail purge grant suggestions", "ready"],
        ["Merge #246 UI shell once variant chosen", "waiting"],
      ],
    },
    text: "",
  },
  {
    id: "cm4",
    role: "user",
    text: "Any of those proposals safe to just auto-approve?",
  },
  {
    id: "cm5",
    role: "assistant",
    text: "Two grant suggestions look strong: archiving LinkedIn notifications (9/9 approved) and trashing Steam sale emails (11/12 approved). Want me to grant those?",
  },
  { id: "cm6", role: "user", text: "Yeah, grant both." },
  {
    id: "cm7",
    role: "assistant",
    text: "Done — both grants are live. Future proposals matching those patterns will auto-approve under the thresholds you set.",
  },
];
