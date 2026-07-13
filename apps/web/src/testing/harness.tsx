import { render } from "@solidjs/testing-library";

import { ApiError } from "../api";
import type {
  AddBucketItem,
  AnswerOutcome,
  BucketItem,
  BucketItemAdded,
  BucketItemState,
  BucketTriageReport,
  Conversation,
  CreateTrigger,
  DedupAdvisory,
  DuePrompt,
  EssayGradeProposal,
  Message,
  ModelList,
  Notification,
  PushStatus,
  RecallAnswerInput,
  TetherApi,
  Trigger,
  UpdateTrigger,
  YouTubeSyncStatus,
} from "../api";
import { App } from "../app";
import type {
  ChatBus,
  ChatBusHandlers,
  ChatFrame,
  CreateChatBus,
} from "../chat-bus";

export const conversation: Conversation = {
  created_at: "2026-01-01T00:00:00Z",
  id: "018f0000-0000-7000-8000-000000000001",
  pi_session_id: "018f0000-0000-7000-8000-000000000002",
  selected_model: "openai:gpt-4.1",
  title: null,
};

export const models: ModelList = {
  default_model: "openai:gpt-4.1",
  models: [
    {
      display_name: "GPT 4.1",
      id: "openai:gpt-4.1",
      model_id: "gpt-4.1",
      provider: "openai",
    },
    {
      display_name: "Claude Sonnet",
      id: "anthropic:claude-sonnet-4",
      model_id: "claude-sonnet-4",
      provider: "anthropic",
    },
  ],
};

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
      memory_id: "018f0000-0000-7000-8000-0000000000e1",
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
    next_attempt_at: null,
    next_fire_at: "2099-01-01T15:00:00Z",
    payload: "call the dentist",
    recurrence: "once",
    status: "active",
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

export const emptyTriageReport: BucketTriageReport = {
  active: [],
  duplicates: [],
  stale: [],
  under_specified: [],
};

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

export class FakeApi implements TetherApi {
  authenticated: boolean;
  createTriggerCalls: CreateTrigger[] = [];
  updateTriggerCalls: { body: UpdateTrigger; triggerId: string }[] = [];
  deleteTriggerCalls: { triggerId: string; version: number }[] = [];
  // Per-trigger version the fake "server" will accept on update/delete; a
  // mismatch (e.g. after the trigger fired) is rejected with a 409, like the host.
  serverTriggerVersions: Record<string, number> = {};
  // Definition fields the fake server reveals alongside the bumped version when
  // an update 409s — simulates a genuine concurrent edit (second tab, agent)
  // rather than a mere fire, which bumps the version but leaves the definition.
  serverTriggerEdits: Record<string, Partial<Trigger>> = {};
  // Forced per-call rejections, consumed FIFO before any version check. Lets a
  // test make the first call 409 and the retry fail with a different status.
  updateTriggerRejections: ApiError[] = [];
  deleteTriggerRejections: ApiError[] = [];
  dismissNotificationCalls: string[] = [];
  clearNotificationsCalls = 0;
  loginPassword: string | undefined;
  messageCalls = 0;
  clearConversationCalls = 0;
  pushSubscribed = false;
  selectedModel: string | undefined;
  storedConversation: Conversation = { ...conversation };
  storedMessages: Message[];
  storedNotifications: Notification[] = [];
  storedTriggers: Trigger[];
  subscribeCalls: { auth: string; endpoint: string; p256dh: string }[] = [];
  unsubscribeCalls: string[] = [];
  storedDuePrompts: DuePrompt[];
  storedBucketItems: BucketItem[];
  addBucketItemCalls: AddBucketItem[] = [];
  completeBucketItemCalls: { bucketItemId: string; version: number }[] = [];
  deleteBucketItemCalls: { bucketItemId: string; version: number }[] = [];
  searchBucketItemsCalls: string[] = [];
  listBucketItemsCalls = 0;
  // Per-item version the fake "server" will accept on complete/delete; a
  // mismatch (e.g. the agent touched the item) is a 409, like the host.
  serverBucketItemVersions: Record<string, number> = {};
  // Forced per-call rejections, consumed FIFO before any version check.
  completeBucketItemRejections: ApiError[] = [];
  deleteBucketItemRejections: ApiError[] = [];
  // The dedup advisory the next add returns; dedup informs, never blocks.
  nextDedup: DedupAdvisory = { duplicates: [], severity: "none" };
  triageReport: BucketTriageReport = emptyTriageReport;
  answerCalls: ({ promptId: string } & RecallAnswerInput)[] = [];
  proposeCalls: { answerText: string; promptId: string }[] = [];
  // Forced per-call rejections for grade proposals, consumed FIFO. Lets a test
  // make the proposal request fail while answering still works.
  proposeRejections: ApiError[] = [];
  correctIndices: Record<string, number> = {};

  constructor(options: {
    authenticated: boolean;
    bucketItems?: BucketItem[];
    duePrompts?: DuePrompt[];
    messages?: Message[];
    triggers?: Trigger[];
  }) {
    this.authenticated = options.authenticated;
    this.storedMessages = options.messages ?? [];
    this.storedTriggers = options.triggers ?? [];
    this.storedDuePrompts = options.duePrompts ?? [];
    this.storedBucketItems = options.bucketItems ?? [];
  }

  getSession() {
    return Promise.resolve({ authenticated: this.authenticated });
  }

  login(password: string) {
    this.loginPassword = password;
    this.authenticated = true;
    return Promise.resolve();
  }

  logout() {
    this.authenticated = false;
    return Promise.resolve();
  }

  listConversations() {
    return Promise.resolve([this.storedConversation]);
  }

  listMessages() {
    this.messageCalls += 1;
    return Promise.resolve(this.storedMessages);
  }

  clearConversation() {
    this.clearConversationCalls += 1;
    this.storedMessages = [];
    this.storedConversation = {
      ...this.storedConversation,
      pi_session_id: `018f0000-0000-7000-8000-00000000c${this.clearConversationCalls
        .toString()
        .padStart(3, "0")}`,
    };
    return Promise.resolve(this.storedConversation);
  }

  listModels() {
    return Promise.resolve(models);
  }

  setConversationModel(_conversationId: string, selectedModel: string) {
    this.selectedModel = selectedModel;
    this.storedConversation = {
      ...this.storedConversation,
      selected_model: selectedModel,
    };
    return Promise.resolve(this.storedConversation);
  }

  listTriggers() {
    return Promise.resolve(this.storedTriggers);
  }

  createTrigger(body: CreateTrigger) {
    this.createTriggerCalls.push(body);
    const created = trigger({
      action_kind: body.action_kind,
      id: `018f0000-0000-7000-8000-0000000000${this.createTriggerCalls.length
        .toString()
        .padStart(2, "0")}`,
      payload: body.payload,
      recurrence: body.recurrence,
    });
    this.storedTriggers = [...this.storedTriggers, created];
    return Promise.resolve(created);
  }

  updateTrigger(triggerId: string, body: UpdateTrigger) {
    this.updateTriggerCalls.push({ body, triggerId });
    const forced = this.updateTriggerRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    const serverVersion = this.serverTriggerVersions[triggerId];
    if (
      Object.hasOwn(this.serverTriggerVersions, triggerId) &&
      serverVersion !== body.version
    ) {
      // The server bumped the version (e.g. the trigger fired, or a concurrent
      // edit landed). Reveal the fresh state to future list fetches, then
      // reject exactly as the host does.
      this.storedTriggers = this.storedTriggers.map((existing) =>
        existing.id === triggerId
          ? {
              ...existing,
              ...this.serverTriggerEdits[triggerId],
              version: serverVersion,
            }
          : existing,
      );
      return Promise.reject(new ApiError(409));
    }
    const current = this.storedTriggers.find(
      (existing) => existing.id === triggerId,
    );
    if (current === undefined) {
      return Promise.reject(new ApiError(404));
    }
    const updated: Trigger = {
      ...current,
      action_kind: body.action_kind,
      payload: body.payload,
      recurrence: body.recurrence,
      timezone: body.timezone ?? "UTC",
      version: body.version + 1,
      wall_time: body.time_of_day,
      weekday: body.weekday,
    };
    this.serverTriggerVersions[triggerId] = updated.version;
    this.storedTriggers = this.storedTriggers.map((existing) =>
      existing.id === triggerId ? updated : existing,
    );
    return Promise.resolve(updated);
  }

  deleteTrigger(triggerId: string, version: number) {
    this.deleteTriggerCalls.push({ triggerId, version });
    const forced = this.deleteTriggerRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    const serverVersion = this.serverTriggerVersions[triggerId];
    if (
      Object.hasOwn(this.serverTriggerVersions, triggerId) &&
      serverVersion !== version
    ) {
      // The server bumped the version (e.g. the trigger fired). Reveal the fresh
      // state to future list fetches, then reject exactly as the host does.
      this.storedTriggers = this.storedTriggers.map((existing) =>
        existing.id === triggerId
          ? { ...existing, status: "completed", version: serverVersion }
          : existing,
      );
      return Promise.reject(new ApiError(409));
    }
    this.storedTriggers = this.storedTriggers.filter(
      (existing) => existing.id !== triggerId,
    );
    return Promise.resolve();
  }

  getPushStatus(): Promise<PushStatus> {
    return Promise.resolve({
      count: this.pushSubscribed ? 1 : 0,
      subscribed: this.pushSubscribed,
    });
  }

  subscribePush(endpoint: string, p256dh: string, auth: string) {
    this.subscribeCalls.push({ auth, endpoint, p256dh });
    this.pushSubscribed = true;
    return Promise.resolve();
  }

  unsubscribePush(endpoint: string): Promise<PushStatus> {
    this.unsubscribeCalls.push(endpoint);
    this.pushSubscribed = false;
    return Promise.resolve({ count: 0, subscribed: false });
  }

  getYouTubeSyncStatus(): Promise<YouTubeSyncStatus> {
    return Promise.resolve({
      api_paused_until: null,
      last_synced_at: null,
      quota: { limit: 10000, remaining: 10000, used: 0 },
      transcript_providers_paused: [],
      transcripts_done: 0,
      transcripts_pending: 0,
      transcripts_unavailable: 0,
      videos_total: 0,
    });
  }

  listDueRecallPrompts(): Promise<DuePrompt[]> {
    return Promise.resolve(this.storedDuePrompts);
  }

  answerRecallPrompt(
    promptId: string,
    input: RecallAnswerInput,
  ): Promise<AnswerOutcome> {
    const answered = this.storedDuePrompts.find(
      (due) => due.prompt.id === promptId,
    );
    this.answerCalls.push({ promptId, ...input });
    this.storedDuePrompts = this.storedDuePrompts.filter(
      (due) => due.prompt.id !== promptId,
    );
    const correct =
      input.confirmed_correct ??
      (input.selected_index !== undefined
        ? input.selected_index === this.correctIndices[promptId]
        : true);
    return Promise.resolve({
      completed: false,
      correct,
      prompt: answered?.prompt ?? this.placeholderPrompt(promptId),
      quality: correct ? 5 : 1,
      tethered: false,
    });
  }

  proposeEssayGrade(
    promptId: string,
    answerText: string,
  ): Promise<EssayGradeProposal> {
    this.proposeCalls.push({ answerText, promptId });
    const forced = this.proposeRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    return Promise.resolve({
      prompt_id: promptId,
      proposed_correct: true,
      reasoning: "Covers the rubric.",
      rubric: "Mentions readiness and cooperative yielding.",
    });
  }

  listNotificationsCalls = 0;

  listNotifications(): Promise<Notification[]> {
    this.listNotificationsCalls += 1;
    return Promise.resolve(this.storedNotifications);
  }

  dismissNotification(notificationId: string): Promise<void> {
    this.dismissNotificationCalls.push(notificationId);
    this.storedNotifications = this.storedNotifications.filter(
      (item) => item.id !== notificationId,
    );
    return Promise.resolve();
  }

  clearNotifications(): Promise<void> {
    this.clearNotificationsCalls += 1;
    this.storedNotifications = [];
    return Promise.resolve();
  }

  listBucketItems(state: BucketItemState): Promise<BucketItem[]> {
    this.listBucketItemsCalls += 1;
    return Promise.resolve(
      this.storedBucketItems.filter((item) => item.state === state),
    );
  }

  searchBucketItems(q: string): Promise<BucketItem[]> {
    this.searchBucketItemsCalls.push(q);
    const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
    return Promise.resolve(
      this.storedBucketItems.filter(
        (item) =>
          item.state === "active" &&
          terms.every((term) => item.title.toLowerCase().includes(term)),
      ),
    );
  }

  addBucketItem(body: AddBucketItem): Promise<BucketItemAdded> {
    this.addBucketItemCalls.push(body);
    const data = body.data as Record<string, unknown>;
    const named = data.title ?? data.name ?? data.destination;
    const title = typeof named === "string" ? named : "untitled";
    const created = bucketItem({
      data: body.data,
      id: `018f0000-0000-7000-8000-0000000001${this.addBucketItemCalls.length
        .toString()
        .padStart(2, "0")}`,
      intent_context: body.intent_context,
      item_type: body.item_type,
      title,
    });
    this.storedBucketItems = [created, ...this.storedBucketItems];
    const dedup = this.nextDedup;
    this.nextDedup = { duplicates: [], severity: "none" };
    return Promise.resolve({ dedup, item: created });
  }

  completeBucketItem(
    bucketItemId: string,
    version: number,
  ): Promise<BucketItem> {
    this.completeBucketItemCalls.push({ bucketItemId, version });
    return this.terminateBucketItem(
      bucketItemId,
      version,
      "completed",
      this.completeBucketItemRejections,
    );
  }

  deleteBucketItem(bucketItemId: string, version: number): Promise<BucketItem> {
    this.deleteBucketItemCalls.push({ bucketItemId, version });
    return this.terminateBucketItem(
      bucketItemId,
      version,
      "deleted",
      this.deleteBucketItemRejections,
    );
  }

  getBucketTriage(): Promise<BucketTriageReport> {
    return Promise.resolve(this.triageReport);
  }

  private terminateBucketItem(
    bucketItemId: string,
    version: number,
    state: "completed" | "deleted",
    rejections: ApiError[],
  ): Promise<BucketItem> {
    const forced = rejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    const serverVersion = this.serverBucketItemVersions[bucketItemId];
    if (
      Object.hasOwn(this.serverBucketItemVersions, bucketItemId) &&
      serverVersion !== version
    ) {
      // The server bumped the version; reveal the fresh state to future list
      // fetches, then reject exactly as the host does.
      this.storedBucketItems = this.storedBucketItems.map((existing) =>
        existing.id === bucketItemId
          ? { ...existing, version: serverVersion }
          : existing,
      );
      return Promise.reject(new ApiError(409));
    }
    const current = this.storedBucketItems.find(
      (existing) => existing.id === bucketItemId,
    );
    if (current === undefined) {
      return Promise.reject(new ApiError(404));
    }
    if (current.state !== "active") {
      return Promise.reject(new ApiError(409));
    }
    const stamp = "2026-01-02T00:00:00Z";
    const terminal: BucketItem = {
      ...current,
      completed_at: state === "completed" ? stamp : current.completed_at,
      deleted_at: state === "deleted" ? stamp : current.deleted_at,
      state,
      updated_at: stamp,
      version: version + 1,
    };
    this.serverBucketItemVersions[bucketItemId] = terminal.version;
    this.storedBucketItems = this.storedBucketItems.map((existing) =>
      existing.id === bucketItemId ? terminal : existing,
    );
    return Promise.resolve(terminal);
  }

  private placeholderPrompt(promptId: string): DuePrompt["prompt"] {
    return {
      choices: [],
      due_at: "2026-01-01T00:00:00Z",
      id: promptId,
      kind: "multiple_choice",
      question: "",
      study_item_id: promptId,
    };
  }
}

export function input(element: HTMLElement): HTMLInputElement {
  if (!(element instanceof HTMLInputElement)) {
    throw new Error("expected input");
  }
  return element;
}

export function textarea(element: HTMLElement): HTMLTextAreaElement {
  if (!(element instanceof HTMLTextAreaElement)) {
    throw new Error("expected textarea");
  }
  return element;
}

export function createBusHarness(): {
  createChatBus: CreateChatBus;
  emit(frame: ChatFrame): void;
  sent: {
    content?: string;
    conversationId: string;
    type: "abort" | "prompt";
  }[];
} {
  let closed = false;
  let handlers: ChatBusHandlers | undefined;
  const sent: {
    content?: string;
    conversationId: string;
    type: "abort" | "prompt";
  }[] = [];
  const bus: ChatBus = {
    abort(conversationId) {
      sent.push({ conversationId, type: "abort" });
    },
    close() {
      closed = true;
    },
    sendPrompt(conversationId, content) {
      sent.push({ content, conversationId, type: "prompt" });
    },
  };
  return {
    createChatBus(nextHandlers) {
      handlers = nextHandlers;
      return bus;
    },
    emit(frame) {
      if (!closed) {
        handlers?.onFrame(frame);
      }
    },
    sent,
  };
}

export function renderApp(api: FakeApi, bus = createBusHarness()) {
  render(() => <App api={api} createChatBus={bus.createChatBus} />);
  return bus;
}
