import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { App } from "./app";
import type { Conversation, Message, ModelList, TetherApi } from "./api";
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

class FakeApi implements TetherApi {
  authenticated: boolean;
  loginPassword: string | undefined;
  messageCalls = 0;
  selectedModel: string | undefined;
  storedConversation: Conversation = { ...conversation };
  storedMessages: Message[];

  constructor(options: { authenticated: boolean; messages?: Message[] }) {
    this.authenticated = options.authenticated;
    this.storedMessages = options.messages ?? [];
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
});
