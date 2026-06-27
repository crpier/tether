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
import type {
  Conversation,
  CreateTrigger,
  Message,
  TetherApi,
  TriggerActionKind,
  TriggerRecurrence,
} from "./api";
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
  push: ["push"] as const,
  session: ["session"] as const,
  triggers: ["triggers"] as const,
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

interface NotifyItem {
  body: string;
  id: string;
  title?: string | null;
}

const PUSH_ENDPOINT_KEY = "tether-push-endpoint";

function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

let cachedPushEndpoint: string | undefined;

function browserPushEndpoint(): string {
  if (cachedPushEndpoint !== undefined) {
    return cachedPushEndpoint;
  }
  let endpoint: string | null;
  try {
    endpoint = window.localStorage.getItem(PUSH_ENDPOINT_KEY);
  } catch {
    endpoint = null;
  }
  if (endpoint === null) {
    endpoint = `urn:tether:browser:${crypto.randomUUID()}`;
    try {
      window.localStorage.setItem(PUSH_ENDPOINT_KEY, endpoint);
    } catch {
      // localStorage unavailable (e.g. opaque origin); keep the in-memory value.
    }
  }
  cachedPushEndpoint = endpoint;
  return endpoint;
}

function formatFireTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

const WEEKDAYS = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
];

function NotificationsPanel(props: { notifications: NotifyItem[] }) {
  return (
    <section aria-label="Notifications">
      <h2>Notifications</h2>
      <Show
        fallback={<p>No notifications yet</p>}
        when={props.notifications.length > 0}
      >
        <ul>
          <For each={props.notifications}>
            {(item) => (
              <li>
                <Show when={item.title}>
                  {(title) => <strong>{title()} </strong>}
                </Show>
                <span>{item.body}</span>
              </li>
            )}
          </For>
        </ul>
      </Show>
    </section>
  );
}

function TriggersPanel(props: { api: TetherApi }) {
  const queryClient = useQueryClient();
  const triggersQuery = createQuery(() => ({
    queryFn: () => props.api.listTriggers(),
    queryKey: queryKeys.triggers,
  }));

  const [recurrence, setRecurrence] = createSignal<TriggerRecurrence>("once");
  const [actionKind, setActionKind] =
    createSignal<TriggerActionKind>("message");
  const [payload, setPayload] = createSignal("");
  const [fireAt, setFireAt] = createSignal("");
  const [timeOfDay, setTimeOfDay] = createSignal("09:00");
  const [timezone, setTimezone] = createSignal(browserTimezone());
  const [weekday, setWeekday] = createSignal(0);
  const [error, setError] = createSignal<string | undefined>();

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.triggers });
    void queryClient.refetchQueries({ queryKey: queryKeys.triggers });
  };

  const submit = () => {
    const rec = recurrence();
    if (payload().trim().length === 0) {
      setError("Add a reminder message");
      return;
    }
    let fireAtIso: string | null = null;
    if (rec === "once") {
      const parsed = new Date(fireAt());
      if (Number.isNaN(parsed.getTime())) {
        setError("Pick a date and time");
        return;
      }
      fireAtIso = parsed.toISOString();
    }
    const body: CreateTrigger = {
      action_kind: actionKind(),
      fire_at: fireAtIso,
      payload: payload().trim(),
      recurrence: rec,
      time_of_day: rec === "once" ? null : timeOfDay(),
      timezone: rec === "once" ? null : timezone(),
      weekday: rec === "weekly" ? weekday() : null,
    };
    void (async () => {
      setError(undefined);
      try {
        await props.api.createTrigger(body);
        setPayload("");
        setFireAt("");
        refresh();
      } catch (caught) {
        setError(
          caught instanceof Error
            ? caught.message
            : "Could not create reminder",
        );
      }
    })();
  };

  const remove = (triggerId: string, version: number) => {
    void (async () => {
      setError(undefined);
      try {
        await props.api.deleteTrigger(triggerId, version);
        refresh();
      } catch (caught) {
        setError(
          caught instanceof Error
            ? caught.message
            : "Could not delete reminder",
        );
      }
    })();
  };

  const onSubmit: JSX.EventHandler<HTMLFormElement, SubmitEvent> = (event) => {
    event.preventDefault();
    submit();
  };

  return (
    <section aria-label="Reminders">
      <h2>Reminders</h2>
      <form onSubmit={onSubmit}>
        <label>
          Reminder
          <input
            name="payload"
            onInput={(event) => {
              setPayload(event.currentTarget.value);
            }}
            value={payload()}
          />
        </label>
        <label>
          Repeat
          <select
            name="recurrence"
            onChange={(event) => {
              setRecurrence(event.currentTarget.value as TriggerRecurrence);
            }}
            value={recurrence()}
          >
            <option value="once">Once</option>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
          </select>
        </label>
        <label>
          Action
          <select
            name="action_kind"
            onChange={(event) => {
              setActionKind(event.currentTarget.value as TriggerActionKind);
            }}
            value={actionKind()}
          >
            <option value="message">Send this message</option>
            <option value="prompt">Run as agent prompt</option>
          </select>
        </label>
        <Show when={recurrence() === "once"}>
          <label>
            Date and time
            <input
              name="fire_at"
              onInput={(event) => {
                setFireAt(event.currentTarget.value);
              }}
              type="datetime-local"
              value={fireAt()}
            />
          </label>
        </Show>
        <Show when={recurrence() !== "once"}>
          <label>
            Time of day
            <input
              name="time_of_day"
              onInput={(event) => {
                setTimeOfDay(event.currentTarget.value);
              }}
              type="time"
              value={timeOfDay()}
            />
          </label>
          <label>
            Time zone
            <input
              name="timezone"
              onInput={(event) => {
                setTimezone(event.currentTarget.value);
              }}
              value={timezone()}
            />
          </label>
        </Show>
        <Show when={recurrence() === "weekly"}>
          <label>
            Day of week
            <select
              name="weekday"
              onChange={(event) => {
                setWeekday(Number(event.currentTarget.value));
              }}
              value={weekday()}
            >
              <For each={WEEKDAYS}>
                {(day, index) => <option value={index()}>{day}</option>}
              </For>
            </select>
          </label>
        </Show>
        <button type="submit">Add reminder</button>
      </form>
      <Show when={error()}>{(message) => <p role="alert">{message()}</p>}</Show>
      <ul>
        <For each={triggersQuery.data ?? []}>
          {(trigger) => (
            <li aria-label={`Reminder: ${trigger.payload}`}>
              <span>{trigger.payload}</span>
              <span>{` · ${trigger.recurrence} · ${trigger.status}`}</span>
              <span>{` · next ${formatFireTime(trigger.next_fire_at)}`}</span>
              <button
                onClick={() => {
                  remove(trigger.id, trigger.version);
                }}
                type="button"
              >
                Delete
              </button>
            </li>
          )}
        </For>
      </ul>
    </section>
  );
}

function PushControl(props: { api: TetherApi }) {
  const queryClient = useQueryClient();
  const endpoint = browserPushEndpoint();
  const statusQuery = createQuery(() => ({
    queryFn: () => props.api.getPushStatus(endpoint),
    queryKey: queryKeys.push,
  }));
  const [busy, setBusy] = createSignal(false);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.push });
    void queryClient.refetchQueries({ queryKey: queryKeys.push });
  };

  const enable = () => {
    void (async () => {
      setBusy(true);
      try {
        await props.api.subscribePush(endpoint, "browser-key", "browser-auth");
        refresh();
      } finally {
        setBusy(false);
      }
    })();
  };

  const disable = () => {
    void (async () => {
      setBusy(true);
      try {
        await props.api.unsubscribePush(endpoint);
        refresh();
      } finally {
        setBusy(false);
      }
    })();
  };

  return (
    <section aria-label="Notification delivery">
      <h2>Push notifications</h2>
      <Show fallback={<p>Checking…</p>} when={statusQuery.data}>
        {(status) => (
          <Show
            fallback={
              <>
                <p>Not subscribed</p>
                <button disabled={busy()} onClick={enable} type="button">
                  Enable notifications
                </button>
              </>
            }
            when={status().subscribed}
          >
            <p>Subscribed</p>
            <button disabled={busy()} onClick={disable} type="button">
              Disable notifications
            </button>
          </Show>
        )}
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
  const [notifications, setNotifications] = createSignal<NotifyItem[]>([]);

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
    if (frame.type === "notify") {
      setNotifications((items) => [
        {
          body: frame.body,
          id: `${frame.trigger_id}:${Date.now().toString()}`,
          title: frame.title,
        },
        ...items,
      ]);
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
      <NotificationsPanel notifications={notifications()} />
      <TriggersPanel api={props.api} />
      <PushControl api={props.api} />
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
