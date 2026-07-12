import { render } from "@solidjs/testing-library";

import { ApiError } from "../api";
import type {
  AnswerOutcome,
  Conversation,
  CreateTrigger,
  DuePrompt,
  Message,
  ModelList,
  Notification,
  PushStatus,
  TetherApi,
  Trigger,
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
  promptId?: string;
  question?: string;
  sourceTitle?: string;
}): DuePrompt {
  const promptId = overrides.promptId ?? "018f0000-0000-7000-8000-0000000000c1";
  return {
    prompt: {
      choices: overrides.choices ?? ["One thread", "Many threads"],
      due_at: "2026-01-01T00:00:00Z",
      id: promptId,
      kind: "multiple_choice",
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
  deleteTriggerCalls: { triggerId: string; version: number }[] = [];
  // Per-trigger version the fake "server" will accept on delete; a mismatch
  // (e.g. after the trigger fired) is rejected with a 409, like the host.
  serverTriggerVersions: Record<string, number> = {};
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
  answerCalls: {
    promptId: string;
    responseMs: number;
    selectedIndex: number;
  }[] = [];
  correctIndices: Record<string, number> = {};

  constructor(options: {
    authenticated: boolean;
    duePrompts?: DuePrompt[];
    messages?: Message[];
    triggers?: Trigger[];
  }) {
    this.authenticated = options.authenticated;
    this.storedMessages = options.messages ?? [];
    this.storedTriggers = options.triggers ?? [];
    this.storedDuePrompts = options.duePrompts ?? [];
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

  deleteTrigger(triggerId: string, version: number) {
    this.deleteTriggerCalls.push({ triggerId, version });
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
    selectedIndex: number,
    responseMs: number,
  ): Promise<AnswerOutcome> {
    const answered = this.storedDuePrompts.find(
      (due) => due.prompt.id === promptId,
    );
    this.answerCalls.push({ promptId, responseMs, selectedIndex });
    this.storedDuePrompts = this.storedDuePrompts.filter(
      (due) => due.prompt.id !== promptId,
    );
    const correct = selectedIndex === this.correctIndices[promptId];
    return Promise.resolve({
      completed: false,
      correct,
      prompt: answered?.prompt ?? this.placeholderPrompt(promptId),
      quality: correct ? 5 : 1,
      tethered: false,
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
