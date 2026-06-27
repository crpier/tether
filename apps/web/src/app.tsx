import {
  QueryClient,
  QueryClientProvider,
  createQuery,
  useQueryClient,
} from "@tanstack/solid-query";
import {
  For,
  Show,
  createEffect,
  createMemo,
  createSignal,
  onCleanup,
  onMount,
} from "solid-js";
import type { JSX } from "solid-js";

import { createRestApi } from "./api";
import type { Conversation, Message, TetherApi } from "./api";
import { createBrowserChatBus } from "./chat-bus";
import type { ChatBus, ChatFrame, CreateChatBus } from "./chat-bus";

export interface AppDependencies {
  api?: TetherApi;
  createChatBus?: CreateChatBus;
}

const queryKeys = {
  conversations: ["conversations"] as const,
  messages: (conversationId: string) => ["messages", conversationId] as const,
  models: ["models"] as const,
  session: ["session"] as const,
};

interface DisplayMessage {
  content: string;
  id: string;
  role: Message["role"];
  toolName?: string | null;
}

function deltaText(delta: unknown): string {
  if (typeof delta === "string") {
    return delta;
  }
  if (typeof delta === "object" && delta !== null && "text" in delta) {
    const text = (delta as { text?: unknown }).text;
    return typeof text === "string" ? text : "";
  }
  return "";
}

function invalidateNamedKey(queryClient: QueryClient, key: string): void {
  if (key === "messages") {
    void queryClient.invalidateQueries({ queryKey: ["messages"] });
    void queryClient.refetchQueries({ queryKey: ["messages"] });
    return;
  }
  void queryClient.invalidateQueries({ queryKey: [key] });
  void queryClient.refetchQueries({ queryKey: [key] });
}

function messageLabel(message: DisplayMessage): string {
  switch (message.role) {
    case "assistant":
      return "Tether";
    case "tool":
      return "Tool";
    case "user":
      return "You";
  }
}

function messageText(message: DisplayMessage): string {
  if (message.role === "tool") {
    return `used ${message.toolName ?? message.content}`;
  }
  return message.content;
}

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        staleTime: 0,
      },
    },
  });
}

function LoginScreen(props: { api: TetherApi }) {
  const queryClient = useQueryClient();
  const [password, setPassword] = createSignal("");
  const [error, setError] = createSignal<string | undefined>();
  const [submitting, setSubmitting] = createSignal(false);

  const submit = async () => {
    setSubmitting(true);
    setError(undefined);
    try {
      await props.api.login(password());
      setPassword("");
      await queryClient.invalidateQueries({ queryKey: queryKeys.session });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Login failed");
    } finally {
      setSubmitting(false);
    }
  };

  const onSubmit: JSX.EventHandler<HTMLFormElement, SubmitEvent> = (event) => {
    event.preventDefault();
    void submit();
  };

  return (
    <main aria-labelledby="login-title">
      <h1 id="login-title">Sign in to Tether</h1>
      <form onSubmit={onSubmit}>
        <label>
          Password
          <input
            autocomplete="current-password"
            name="password"
            onInput={(event) => {
              setPassword(event.currentTarget.value);
            }}
            type="password"
            value={password()}
          />
        </label>
        <button disabled={submitting()} type="submit">
          Log in
        </button>
      </form>
      <Show when={error()}>{(message) => <p role="alert">{message()}</p>}</Show>
    </main>
  );
}

function ModelSelector(props: { api: TetherApi; conversation: Conversation }) {
  const queryClient = useQueryClient();
  const modelsQuery = createQuery(() => ({
    queryFn: () => props.api.listModels(),
    queryKey: queryKeys.models,
  }));
  const selectedModel = createMemo(
    () =>
      props.conversation.selected_model ??
      modelsQuery.data?.default_model ??
      "",
  );

  const persistModel = (model: string) => {
    if (model.length === 0 || model === selectedModel()) {
      return;
    }
    void (async () => {
      await props.api.setConversationModel(props.conversation.id, model);
      await queryClient.invalidateQueries({
        queryKey: queryKeys.conversations,
      });
    })();
  };

  return (
    <div aria-label="Model" role="group">
      <span>Model</span>
      <For each={modelsQuery.data?.models ?? []}>
        {(model) => (
          <button
            aria-pressed={selectedModel() === model.id}
            disabled={modelsQuery.isLoading}
            onClick={() => {
              persistModel(model.id);
            }}
            type="button"
          >
            {model.display_name}
          </button>
        )}
      </For>
    </div>
  );
}

function MessageRows(props: {
  messages: DisplayMessage[];
  streamText: string;
}) {
  return (
    <section aria-label="Chat transcript">
      <For each={props.messages}>
        {(message) => (
          <article aria-label={`${messageLabel(message)} message`}>
            <strong>{messageLabel(message)}</strong>
            <p>{messageText(message)}</p>
          </article>
        )}
      </For>
      <Show when={props.streamText.length > 0}>
        <article aria-label="Tether message">
          <strong>Tether</strong>
          <p>{props.streamText}</p>
        </article>
      </Show>
    </section>
  );
}

function ChatView(props: { api: TetherApi; createChatBus: CreateChatBus }) {
  const queryClient = useQueryClient();
  const [bus, setBus] = createSignal<ChatBus | undefined>();
  const [draft, setDraft] = createSignal("");
  const [error, setError] = createSignal<string | undefined>();
  const [generating, setGenerating] = createSignal(false);
  const [optimisticMessages, setOptimisticMessages] = createSignal<
    DisplayMessage[]
  >([]);
  const [streamText, setStreamText] = createSignal("");
  const [liveToolMessages, setLiveToolMessages] = createSignal<
    DisplayMessage[]
  >([]);
  const [messagesRefresh, setMessagesRefresh] = createSignal(0);

  const conversationsQuery = createQuery(() => ({
    queryFn: () => props.api.listConversations(),
    queryKey: queryKeys.conversations,
  }));
  const conversation = createMemo(() => conversationsQuery.data?.[0]);
  const conversationId = createMemo(() => conversation()?.id);
  const messagesQuery = createQuery(() => ({
    enabled: conversationId() !== undefined,
    queryFn: async () => {
      const id = conversationId();
      return id === undefined ? [] : props.api.listMessages(id);
    },
    queryKey: [
      ...queryKeys.messages(conversationId() ?? "pending"),
      messagesRefresh(),
    ] as const,
  }));
  const displayMessages = createMemo<DisplayMessage[]>(() => [
    ...(messagesQuery.data ?? []).map((message) => ({
      content: message.content,
      id: message.id,
      role: message.role,
      toolName: message.tool_name,
    })),
    ...optimisticMessages(),
    ...liveToolMessages(),
  ]);

  createEffect(() => {
    const storedMessages = messagesQuery.data;
    if (storedMessages !== undefined) {
      setOptimisticMessages([]);
      setLiveToolMessages([]);
      setStreamText("");
    }
  });

  const rehydrate = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.conversations });
    void queryClient.invalidateQueries({ queryKey: ["messages"] });
    setMessagesRefresh((refresh) => refresh + 1);
  };

  const handleFrame = (frame: ChatFrame) => {
    if (frame.type === "invalidate") {
      for (const key of frame.keys) {
        invalidateNamedKey(queryClient, key);
      }
      if (frame.keys.includes("messages")) {
        setMessagesRefresh((refresh) => refresh + 1);
      }
      return;
    }
    const currentConversationId = conversationId();
    if (
      frame.conversation_id !== undefined &&
      currentConversationId !== undefined &&
      frame.conversation_id !== currentConversationId
    ) {
      return;
    }
    switch (frame.event) {
      case "abort_ack":
        setGenerating(false);
        break;
      case "agent_end":
        setGenerating(false);
        rehydrate();
        break;
      case "error":
        setError(frame.detail ?? "Chat error");
        setGenerating(false);
        break;
      case "message_end":
        rehydrate();
        break;
      case "message_start":
        setStreamText("");
        break;
      case "tool_end":
        setLiveToolMessages((messages) => [
          ...messages,
          {
            content: frame.tool_name ?? "tool",
            id: `tool-${messages.length.toString()}`,
            role: "tool",
            toolName: frame.tool_name,
          },
        ]);
        rehydrate();
        break;
      default: {
        const nextDelta = deltaText(frame.delta);
        if (nextDelta.length > 0) {
          setStreamText((text) => `${text}${nextDelta}`);
        }
      }
    }
  };

  onMount(() => {
    const chatBus = props.createChatBus({
      onDisconnect: rehydrate,
      onFrame: handleFrame,
    });
    setBus(chatBus);
    onCleanup(() => {
      chatBus.close();
    });
  });

  const sendPrompt = () => {
    const content = draft().trim();
    const id = conversationId();
    if (content.length === 0 || id === undefined) {
      return;
    }
    setDraft("");
    setGenerating(true);
    setError(undefined);
    setOptimisticMessages((messages) => [
      ...messages,
      {
        content,
        id: `optimistic-${Date.now().toString()}`,
        role: "user",
      },
    ]);
    bus()?.sendPrompt(id, content);
  };

  const abort = () => {
    const id = conversationId();
    if (id !== undefined) {
      bus()?.abort(id);
    }
  };

  const logout = () => {
    void (async () => {
      await props.api.logout();
      await queryClient.invalidateQueries({ queryKey: queryKeys.session });
    })();
  };

  const onSubmit: JSX.EventHandler<HTMLFormElement, SubmitEvent> = (event) => {
    event.preventDefault();
    sendPrompt();
  };

  return (
    <main aria-labelledby="chat-title">
      <header>
        <h1 id="chat-title">Tether chat</h1>
        <Show when={conversation()}>
          {(currentConversation) => (
            <ModelSelector
              api={props.api}
              conversation={currentConversation()}
            />
          )}
        </Show>
        <button onClick={logout} type="button">
          Log out
        </button>
      </header>
      <Show when={error()}>{(message) => <p role="alert">{message()}</p>}</Show>
      <Show
        fallback={<p>Loading chat…</p>}
        when={!conversationsQuery.isLoading && conversation() !== undefined}
      >
        <MessageRows messages={displayMessages()} streamText={streamText()} />
        <form onSubmit={onSubmit}>
          <label>
            Message
            <textarea
              onInput={(event) => {
                setDraft(event.currentTarget.value);
              }}
              value={draft()}
            />
          </label>
          <button disabled={generating()} type="submit">
            Send
          </button>
          <button disabled={!generating()} onClick={abort} type="button">
            Stop
          </button>
        </form>
      </Show>
    </main>
  );
}

function AppBody(props: Required<AppDependencies>) {
  const sessionQuery = createQuery(() => ({
    queryFn: () => props.api.getSession(),
    queryKey: queryKeys.session,
  }));

  return (
    <Show
      fallback={<p>Loading…</p>}
      when={!sessionQuery.isLoading && sessionQuery.data}
    >
      {(session) => (
        <Show
          fallback={<LoginScreen api={props.api} />}
          when={session().authenticated}
        >
          <ChatView api={props.api} createChatBus={props.createChatBus} />
        </Show>
      )}
    </Show>
  );
}

export function App(props: AppDependencies = {}) {
  const dependencies: Required<AppDependencies> = {
    api: props.api ?? createRestApi(),
    createChatBus: props.createChatBus ?? createBrowserChatBus,
  };
  const queryClient = makeQueryClient();

  return (
    <QueryClientProvider client={queryClient}>
      <AppBody {...dependencies} />
    </QueryClientProvider>
  );
}
