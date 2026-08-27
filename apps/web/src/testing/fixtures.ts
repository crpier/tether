import type {
  Artifact,
  BucketItem,
  BucketTriageReport,
  Conversation,
  DuePrompt,
  DreamRun,
  Message,
  ModelList,
  Notification,
  Panel,
  ProductObservation,
  Todo,
  TranscriptDecision,
  Trigger,
} from "../host";

export const conversation: Conversation = {
  archived_at: null,
  created_at: "2026-01-01T00:00:00Z",
  display_name: null,
  has_unread: false,
  id: "018f0000-0000-7000-8000-000000000001",
  kind: "main",
  last_read_seq: 0,
  latest_activity: null,
  latest_message_seq: 0,
  pending_turn_count: 0,
  pi_session_id: "018f0000-0000-7000-8000-000000000002",
  running_turn_id: null,
  scope_brief: null,
  scope_revision: 1,
  selected_model: "gpt-5.6-luna",
  session_gap_seconds: 300,
  status: "active",
  title: null,
};

export const models: ModelList = {
  default_model: "gpt-5.6-luna",
  models: [
    {
      display_name: "GPT-5.6 Luna · no thinking",
      id: "gpt-5.6-luna",
      model_id: "gpt-5.6-luna",
      provider: "openai-codex",
      thinking_level: "off",
    },
    {
      display_name: "GPT-5.6 Luna · low thinking",
      id: "gpt-5.6-luna-low",
      model_id: "gpt-5.6-luna",
      provider: "openai-codex",
      thinking_level: "low",
    },
    {
      display_name: "GPT-5.6 Terra · low thinking",
      id: "gpt-5.6-terra-low",
      model_id: "gpt-5.6-terra",
      provider: "openai-codex",
      thinking_level: "low",
    },
    {
      display_name: "GPT-5.6 Terra · medium thinking",
      id: "gpt-5.6-terra",
      model_id: "gpt-5.6-terra",
      provider: "openai-codex",
      thinking_level: "medium",
    },
    {
      display_name: "GPT-5.6 Sol · medium thinking",
      id: "gpt-5.6-sol",
      model_id: "gpt-5.6-sol",
      provider: "openai-codex",
      thinking_level: "medium",
    },
  ],
};

export function dreamRun(overrides: Partial<DreamRun> = {}): DreamRun {
  return {
    attempts: 1,
    completed_at: "2026-08-21T08:01:05Z",
    conversation_id: conversation.id,
    conversation_title: "Default conversation",
    created_at: "2026-08-21T08:01:00Z",
    error: null,
    evidence_end_seq: 2,
    evidence_start_seq: 1,
    id: `019f0000-0000-7000-8000-${Math.random().toString().slice(2, 14).padEnd(12, "0")}`,
    kind: "assimilation",
    mutation_count: 0,
    status: "no_op",
    updated_at: "2026-08-21T08:01:05Z",
    ...overrides,
  };
}

export function message(overrides: Partial<Message>): Message {
  return {
    content: "",
    conversation_id: conversation.id,
    created_at: "2026-01-01T00:00:00Z",
    id: `018f0000-0000-7000-8000-${Math.random().toString().slice(2, 14).padEnd(12, "0")}`,
    pi_message_id: null,
    role: "assistant",
    seq: 1,
    tool_args: null,
    tool_name: null,
    tool_result: null,
    turn: null,
    turn_id: null,
    turn_message_seq: null,
    ...overrides,
  };
}

export function duePrompt(overrides: {
  choices?: string[];
  kind?: DuePrompt["prompt"]["kind"];
  promptId?: string;
  question?: string;
  sourceTitle?: string;
}): DuePrompt {
  const promptId = overrides.promptId ?? "018f0000-0000-7000-8000-0000000000c1";
  const kind = overrides.kind ?? "multiple_choice";
  return {
    prompt: {
      choices:
        overrides.choices ??
        (kind === "multiple_choice" ? ["One thread", "Many threads"] : []),
      due_at: "2026-01-01T00:00:00Z",
      id: promptId,
      kind,
      question: overrides.question ?? "What does async IO multiplex?",
      study_item_id: "018f0000-0000-7000-8000-0000000000d1",
    },
    study_item: {
      completed_at: null,
      created_at: "2026-01-01T00:00:00Z",
      id: "018f0000-0000-7000-8000-0000000000d1",
      distilled_learnings: "Async IO multiplexes waits.",
      source_title: overrides.sourceTitle ?? "Async IO Explained",
      source_video_id: "v1",
      state: "studying",
      updated_at: "2026-01-01T00:00:00Z",
    },
  };
}

export function trigger(overrides: Partial<Trigger>): Trigger {
  return {
    action_kind: "message",
    attempts: 0,
    created_at: "2026-01-01T00:00:00Z",
    id: "018f0000-0000-7000-8000-0000000000aa",
    last_error: null,
    latest_occurrence: null,
    model_profile: null,
    next_attempt_at: null,
    next_fire_at: "2099-01-01T15:00:00Z",
    payload: "call the dentist",
    recurrence: "once",
    status: "active",
    target_conversation_id: null,
    target_conversation_name: null,
    timezone: "UTC",
    updated_at: "2026-01-01T00:00:00Z",
    version: 1,
    wall_time: null,
    weekday: null,
    ...overrides,
  };
}

export function bucketItem(overrides: Partial<BucketItem>): BucketItem {
  const title = overrides.title ?? "Dune";
  return {
    completed_at: null,
    created_at: "2026-01-01T00:00:00Z",
    data: { title },
    deleted_at: null,
    id: `018f0000-0000-7000-8000-${Math.random().toString().slice(2, 14).padEnd(12, "0")}`,
    intent_context: "saved on a whim",
    item_type: "movie",
    state: "active",
    title,
    updated_at: "2026-01-01T00:00:00Z",
    version: 1,
    ...overrides,
  };
}

export function todo(overrides: Partial<Todo>): Todo {
  return {
    action: "call the dentist",
    condition: null,
    created_at: "2026-01-01T00:00:00Z",
    deadline: null,
    id: `018f0000-0000-7000-8000-${Math.random().toString().slice(2, 14).padEnd(12, "0")}`,
    status: "active",
    trigger_id: null,
    updated_at: "2026-01-01T00:00:00Z",
    version: 1,
    waiting: false,
    ...overrides,
  };
}

export function productObservation(
  overrides: Partial<ProductObservation>,
): ProductObservation {
  return {
    conversation_id: conversation.id,
    created_at: "2026-01-01T00:00:00Z",
    id: `018f0000-0000-7000-8000-${Math.random().toString().slice(2, 14).padEnd(12, "0")}`,
    interpretation: "Tether should capture explicit product feedback.",
    message_id: "018f0000-0000-7000-8000-000000000003",
    resolved_at: null,
    status: "open",
    updated_at: "2026-01-01T00:00:00Z",
    version: 1,
    wording: "Log that as feedback.",
    ...overrides,
  };
}

export function panel(overrides: Partial<Panel>): Panel {
  return {
    columns: [],
    created_at: "2026-01-01T00:00:00Z",
    facets: { domain: "finance" },
    id: `018f0000-0000-7000-8000-${Math.random().toString().slice(2, 14).padEnd(12, "0")}`,
    name: "finance",
    position: 0,
    query: null,
    render_kind: "table",
    updated_at: "2026-01-01T00:00:00Z",
    vega_lite_spec: null,
    version: 1,
    window_days: null,
    ...overrides,
  };
}

export function artifact(overrides: Partial<Artifact>): Artifact {
  return {
    created_at: "2026-01-01T00:00:00Z",
    html: "<p>hello</p>",
    id: "018f0000-0000-7000-8000-0000000003aa",
    title: "Untitled artifact",
    version: 1,
    ...overrides,
  };
}

export const emptyTriageReport: BucketTriageReport = {
  active: [],
  duplicates: [],
  purchase: { buy_now: [], missing_price_context: [], stale_watches: [] },
  stale: [],
  under_specified: [],
};

export function transcriptDecision(
  overrides: Partial<TranscriptDecision>,
): TranscriptDecision {
  return {
    attempts: 1,
    channel: "PyConf",
    last_error: "transcript unavailable",
    title: "Captionless talk",
    transcript_status: "needs_review",
    video_id: "v1",
    ...overrides,
  };
}

export function notification(overrides: Partial<Notification>): Notification {
  return {
    action_kind: "message",
    body: "call the dentist",
    created_at: "2026-01-01T00:00:00Z",
    id: "018f0000-0000-7000-8000-0000000000f1",
    source_label: "call the dentist",
    trigger_id: "018f0000-0000-7000-8000-0000000000aa",
    ...overrides,
  };
}
