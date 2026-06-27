import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { App } from "./app";
import type {
  AnswerOutcome,
  Conversation,
  CreateTrigger,
  DuePrompt,
  Message,
  ModelList,
  PushStatus,
  TetherApi,
  Trigger,
} from "./api";
import type {
  ChatBus,
  ChatBusHandlers,
  ChatFrame,
  CreateChatBus,
} from "./chat-bus";

const conversation: Conversation = {
  created_at: "2026-01-01T00:00:00Z",
  id: "018f0000-0000-7000-8000-000000000001",
  pi_session_id: "018f0000-0000-7000-8000-000000000002",
  selected_model: "openai:gpt-4.1",
  title: null,
};

const models: ModelList = {
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

function message(overrides: Partial<Message>): Message {
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

function duePrompt(overrides: {
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

function trigger(overrides: Partial<Trigger>): Trigger {
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

class FakeApi implements TetherApi {
  authenticated: boolean;
  createTriggerCalls: CreateTrigger[] = [];
  deleteTriggerCalls: { triggerId: string; version: number }[] = [];
  loginPassword: string | undefined;
  messageCalls = 0;
  pushSubscribed = false;
  selectedModel: string | undefined;
  storedConversation: Conversation = { ...conversation };
  storedMessages: Message[];
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

function input(element: HTMLElement): HTMLInputElement {
  if (!(element instanceof HTMLInputElement)) {
    throw new Error("expected input");
  }
  return element;
}

function textarea(element: HTMLElement): HTMLTextAreaElement {
  if (!(element instanceof HTMLTextAreaElement)) {
    throw new Error("expected textarea");
  }
  return element;
}

function createBusHarness(): {
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

function renderApp(api: FakeApi, bus = createBusHarness()) {
  render(() => <App api={api} createChatBus={bus.createChatBus} />);
  return bus;
}

afterEach(cleanup);

describe("Tether SPA", () => {
  test("unauthenticated users log in before seeing chat", async () => {
    const api = new FakeApi({ authenticated: false });
    renderApp(api);

    expect(
      await screen.findByRole("heading", { name: "Sign in to Tether" }),
    ).toBeInTheDocument();

    fireEvent.input(input(screen.getByLabelText("Password")), {
      target: { value: "correct horse battery staple" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Log in" }));

    expect(
      await screen.findByRole("heading", { name: "Tether chat" }),
    ).toBeInTheDocument();
    expect(api.loginPassword).toBe("correct horse battery staple");
  });

  test("rehydrates settled chat history", async () => {
    const api = new FakeApi({
      authenticated: true,
      messages: [
        message({ content: "remember aisle seats", role: "user", seq: 1 }),
        message({
          content: "capture",
          role: "tool",
          seq: 2,
          tool_name: "capture",
        }),
        message({
          content: "Captured that preference.",
          role: "assistant",
          seq: 3,
        }),
      ],
    });
    renderApp(api);

    expect(await screen.findByText("remember aisle seats")).toBeInTheDocument();
    expect(screen.getByText("used capture")).toBeInTheDocument();
    expect(screen.getByText("Captured that preference.")).toBeInTheDocument();
  });

  test("sends prompts and renders streamed assistant deltas", async () => {
    const api = new FakeApi({ authenticated: true });
    const bus = renderApp(api);

    const messageBox = textarea(await screen.findByLabelText("Message"));
    fireEvent.input(messageBox, { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(bus.sent).toEqual([
      { content: "Hello", conversationId: conversation.id, type: "prompt" },
    ]);
    expect(screen.getByText("Hello")).toBeInTheDocument();

    bus.emit({
      conversation_id: conversation.id,
      event: "message_start",
      type: "chat",
    });
    bus.emit({
      conversation_id: conversation.id,
      delta: { text: "Hi" },
      event: "text_delta",
      type: "chat",
    });
    bus.emit({
      conversation_id: conversation.id,
      delta: " there",
      event: "text_delta",
      type: "chat",
    });

    expect(await screen.findByText("Hi there")).toBeInTheDocument();
  });

  test("stop aborts an in-flight generation", async () => {
    const api = new FakeApi({ authenticated: true });
    const bus = renderApp(api);

    fireEvent.input(textarea(await screen.findByLabelText("Message")), {
      target: { value: "Keep going" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));

    expect(bus.sent).toEqual([
      {
        content: "Keep going",
        conversationId: conversation.id,
        type: "prompt",
      },
      { conversationId: conversation.id, type: "abort" },
    ]);
  });

  test("invalidate frames refetch named query keys", async () => {
    const api = new FakeApi({ authenticated: true });
    const bus = renderApp(api);

    await waitFor(() => {
      expect(api.messageCalls).toBe(1);
    });

    bus.emit({ keys: ["messages"], type: "invalidate" });

    await waitFor(() => {
      expect(api.messageCalls).toBeGreaterThan(1);
    });
  });

  test("selecting a model persists it for the conversation", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    fireEvent.click(
      await screen.findByRole("button", { name: "Claude Sonnet" }),
    );

    await waitFor(() => {
      expect(api.selectedModel).toBe("anthropic:claude-sonnet-4");
    });
  });

  test("lists existing reminders", async () => {
    const api = new FakeApi({
      authenticated: true,
      triggers: [trigger({ payload: "water the plants" })],
    });
    renderApp(api);

    expect(
      await screen.findByLabelText("Reminder: water the plants"),
    ).toBeInTheDocument();
  });

  test("creating a one-off reminder posts the right body", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    fireEvent.input(input(await screen.findByLabelText("Reminder")), {
      target: { value: "stretch" },
    });
    fireEvent.input(input(screen.getByLabelText("Date and time")), {
      target: { value: "2099-01-01T15:00" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add reminder" }));

    await waitFor(() => {
      expect(api.createTriggerCalls).toHaveLength(1);
    });
    const body = api.createTriggerCalls[0];
    expect(body.payload).toBe("stretch");
    expect(body.recurrence).toBe("once");
    expect(body.action_kind).toBe("message");
    expect(body.fire_at).not.toBeNull();
    expect(body.time_of_day).toBeNull();
    expect(
      await screen.findByLabelText("Reminder: stretch"),
    ).toBeInTheDocument();
  });

  test("deleting a reminder calls the API with its version", async () => {
    const api = new FakeApi({
      authenticated: true,
      triggers: [
        trigger({ id: "trig-1", payload: "renew passport", version: 3 }),
      ],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Reminder: renew passport");
    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(api.deleteTriggerCalls).toEqual([
        { triggerId: "trig-1", version: 3 },
      ]);
    });
  });

  test("enabling push subscribes the browser", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    fireEvent.click(
      await screen.findByRole("button", { name: "Enable notifications" }),
    );

    await waitFor(() => {
      expect(api.subscribeCalls).toHaveLength(1);
    });
    expect(
      await screen.findByRole("button", { name: "Disable notifications" }),
    ).toBeInTheDocument();
  });

  test("notify frames surface in the notifications panel", async () => {
    const api = new FakeApi({ authenticated: true });
    const bus = renderApp(api);

    await screen.findByRole("heading", { name: "Tether chat" });
    bus.emit({
      body: "call the dentist",
      title: "Reminder",
      trigger_id: "trig-9",
      type: "notify",
    });

    expect(await screen.findByText("call the dentist")).toBeInTheDocument();
  });

  test("lists outstanding recall prompts with their choices", async () => {
    const api = new FakeApi({
      authenticated: true,
      duePrompts: [duePrompt({ question: "What does async IO multiplex?" })],
    });
    renderApp(api);

    const row = await screen.findByLabelText(
      "Recall prompt: What does async IO multiplex?",
    );
    expect(
      within(row).getByRole("button", { name: "One thread" }),
    ).toBeInTheDocument();
    expect(
      within(row).getByRole("button", { name: "Many threads" }),
    ).toBeInTheDocument();
  });

  test("answering a recall prompt submits the chosen option", async () => {
    const api = new FakeApi({
      authenticated: true,
      duePrompts: [
        duePrompt({
          choices: ["One thread", "Many threads"],
          promptId: "018f0000-0000-7000-8000-0000000000c9",
        }),
      ],
    });
    api.correctIndices["018f0000-0000-7000-8000-0000000000c9"] = 0;
    renderApp(api);

    const row = await screen.findByLabelText(
      "Recall prompt: What does async IO multiplex?",
    );
    fireEvent.click(within(row).getByRole("button", { name: "One thread" }));

    await waitFor(() => {
      expect(api.answerCalls).toHaveLength(1);
    });
    expect(api.answerCalls[0].promptId).toBe(
      "018f0000-0000-7000-8000-0000000000c9",
    );
    expect(api.answerCalls[0].selectedIndex).toBe(0);
    expect(
      await screen.findByText("Correct — see you next round."),
    ).toBeInTheDocument();
  });

  test("shows an empty state when no recall prompts are due", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    await screen.findByRole("heading", { name: "Tether chat" });
    expect(
      await screen.findByText("No recall prompts due"),
    ).toBeInTheDocument();
  });
});
