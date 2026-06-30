import {
  QueryClient,
  QueryClientProvider,
  createQuery,
  useQueryClient,
} from "@tanstack/solid-query";
import {
  For,
  Match,
  Show,
  Switch,
  createEffect,
  createMemo,
  createSignal,
  onCleanup,
  onMount,
  untrack,
} from "solid-js";
import type { JSX } from "solid-js";

import { createRestApi } from "./api";
import type {
  AnswerOutcome,
  Conversation,
  CreateTrigger,
  TetherApi,
  TriggerActionKind,
  TriggerRecurrence,
} from "./api";
import { createBrowserChatBus } from "./chat-bus";
import type {
  ChatBus,
  ChatFrame,
  ConnectionStatus,
  CreateChatBus,
} from "./chat-bus";
import {
  deriveRows,
  emptyTurn,
  isAwaitingFirstToken,
  reduceFrame,
  startTurn,
} from "./chat-timeline";
import type {
  ChatRole,
  LiveTurn,
  StoredMessage,
  TimelineRow,
} from "./chat-timeline";
import { MessageContent } from "./components/message-content";
import { Button } from "@/components/ui/button";
import {
  TextField,
  TextFieldInput,
  TextFieldLabel,
  TextFieldTextArea,
} from "@/components/ui/text-field";

const selectClass =
  "border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 h-9 rounded-md border px-3 py-1 text-sm shadow-xs transition-[color,box-shadow] outline-none focus-visible:ring-[3px]";
const panelClass =
  "bg-card text-card-foreground rounded-xl border p-4 shadow-sm";
const fieldLabelClass = "text-muted-foreground text-xs font-medium";

export interface AppDependencies {
  api?: TetherApi;
  createChatBus?: CreateChatBus;
}

const queryKeys = {
  conversations: ["conversations"] as const,
  messages: (conversationId: string) => ["messages", conversationId] as const,
  models: ["models"] as const,
  push: ["push"] as const,
  recall: ["recall"] as const,
  session: ["session"] as const,
  triggers: ["triggers"] as const,
};

function invalidateNamedKey(queryClient: QueryClient, key: string): void {
  if (key === "messages") {
    void queryClient.invalidateQueries({ queryKey: ["messages"] });
    void queryClient.refetchQueries({ queryKey: ["messages"] });
    return;
  }
  void queryClient.invalidateQueries({ queryKey: [key] });
  void queryClient.refetchQueries({ queryKey: [key] });
}

function messageLabel(role: ChatRole): string {
  switch (role) {
    case "assistant":
      return "Tether";
    case "tool":
      return "Tool";
    case "user":
      return "You";
  }
}

function bubbleClass(role: ChatRole): string {
  const base = "flex flex-col gap-1 rounded-lg text-sm";
  switch (role) {
    case "user":
      return `${base} bg-primary text-primary-foreground ml-auto max-w-[80%] px-3 py-2`;
    case "assistant":
      return `${base} bg-muted mr-auto max-w-[80%] px-3 py-2`;
    case "tool":
      return `${base} text-muted-foreground mx-auto py-0.5 text-xs italic`;
  }
}

const bubbleLabelClass =
  "text-[0.7rem] font-semibold tracking-wide uppercase opacity-70";

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
    <main
      aria-labelledby="login-title"
      class="flex min-h-screen items-center justify-center p-6"
    >
      <div class="bg-card text-card-foreground w-full max-w-sm space-y-6 rounded-xl border p-8 shadow-sm">
        <h1 id="login-title" class="text-xl font-semibold tracking-tight">
          Sign in to Tether
        </h1>
        <form onSubmit={onSubmit} class="space-y-4">
          <TextField value={password()} onChange={setPassword}>
            <TextFieldLabel>Password</TextFieldLabel>
            <TextFieldInput
              autocomplete="current-password"
              name="password"
              type="password"
            />
          </TextField>
          <Button class="w-full" disabled={submitting()} type="submit">
            Log in
          </Button>
        </form>
        <Show when={error()}>
          {(message) => (
            <p class="text-destructive text-sm" role="alert">
              {message()}
            </p>
          )}
        </Show>
      </div>
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
    <div aria-label="Model" class="flex items-center gap-1.5" role="group">
      <span class="text-muted-foreground text-xs">Model</span>
      <For each={modelsQuery.data?.models ?? []}>
        {(model) => (
          <Button
            aria-pressed={selectedModel() === model.id}
            disabled={modelsQuery.isLoading}
            onClick={() => {
              persistModel(model.id);
            }}
            size="sm"
            type="button"
            variant={selectedModel() === model.id ? "default" : "outline"}
          >
            {model.display_name}
          </Button>
        )}
      </For>
    </div>
  );
}

function toolText(row: Extract<TimelineRow, { kind: "tool" }>): string {
  return row.status === "running"
    ? `using ${row.toolName}…`
    : `used ${row.toolName}`;
}

// Elapsed-time label that ticks via a text node mutation rather than a signal,
// so a running turn never re-renders the whole transcript once a second.
function WorkingIndicator(props: { startedAt: number }) {
  let label: HTMLSpanElement | undefined;
  const render = () => {
    if (label) {
      const seconds = Math.max(
        0,
        Math.round((Date.now() - props.startedAt) / 1000),
      );
      label.textContent = `${seconds.toString()}s`;
    }
  };
  onMount(() => {
    render();
    const handle = window.setInterval(render, 1000);
    onCleanup(() => {
      window.clearInterval(handle);
    });
  });
  return (
    <article aria-label="Tether working" class={bubbleClass("assistant")}>
      <strong class={bubbleLabelClass}>Tether</strong>
      <p class="text-muted-foreground flex items-center gap-2 text-sm">
        <span
          aria-hidden="true"
          class="bg-muted-foreground/70 inline-block size-2 animate-pulse rounded-full"
        />
        <span>Working</span>
        <span
          ref={(element) => {
            label = element;
          }}
          class="tabular-nums opacity-70"
        />
      </p>
    </article>
  );
}

function MessageRow(props: { row: TimelineRow }) {
  return (
    <Switch>
      <Match when={props.row.kind === "tool" && props.row}>
        {(tool) => (
          <article
            aria-label="Tool activity"
            class="text-muted-foreground mx-auto flex items-center gap-2 py-0.5 text-xs italic"
          >
            <Show
              fallback={<span aria-hidden="true">✓</span>}
              when={tool().status === "running"}
            >
              <span
                aria-hidden="true"
                class="border-muted-foreground/40 border-t-muted-foreground inline-block size-3 animate-spin rounded-full border-2"
              />
            </Show>
            <span>{toolText(tool())}</span>
          </article>
        )}
      </Match>
      <Match when={props.row.kind === "reasoning" && props.row}>
        {(reasoning) => (
          <article
            aria-label="Tether reasoning"
            class="bg-muted/50 text-muted-foreground mr-auto max-w-[80%] rounded-lg px-3 py-2 text-xs"
          >
            <strong class={bubbleLabelClass}>Thinking</strong>
            <p class="mt-1 whitespace-pre-wrap break-words">
              {reasoning().text}
            </p>
          </article>
        )}
      </Match>
      <Match when={props.row.kind === "message" && props.row}>
        {(message) => (
          <article
            aria-label={`${messageLabel(message().role)} message`}
            class={bubbleClass(message().role)}
          >
            <strong class={bubbleLabelClass}>
              {messageLabel(message().role)}
            </strong>
            <Show
              fallback={
                <p class="whitespace-pre-wrap break-words">
                  {message().role === "tool"
                    ? `used ${message().toolName ?? message().text}`
                    : message().text}
                </p>
              }
              when={message().role === "assistant"}
            >
              <MessageContent text={message().text} />
            </Show>
          </article>
        )}
      </Match>
    </Switch>
  );
}

function MessageRows(props: {
  rows: TimelineRow[];
  working: boolean;
  startedAt: number | null;
  stopped: boolean;
}) {
  let viewport: HTMLElement | undefined;
  const [following, setFollowing] = createSignal(true);

  const atBottom = () => {
    if (!viewport) {
      return true;
    }
    return (
      viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 48
    );
  };
  const scrollToEnd = () => {
    if (viewport) {
      viewport.scrollTop = viewport.scrollHeight;
    }
  };

  // Follow the stream while the user sits at the bottom; the moment they scroll
  // up we stop yanking them back and offer an explicit jump affordance instead.
  createEffect(() => {
    void props.rows;
    void props.working;
    if (following()) {
      queueMicrotask(scrollToEnd);
    }
  });

  return (
    <div class="relative flex min-h-0 flex-1 flex-col">
      <section
        ref={(element) => {
          viewport = element;
        }}
        aria-label="Chat transcript"
        class="bg-card flex-1 space-y-3 overflow-y-auto [overflow-anchor:none] rounded-xl border p-4 shadow-sm"
        onScroll={() => {
          setFollowing(atBottom());
        }}
      >
        <For each={props.rows}>{(row) => <MessageRow row={row} />}</For>
        <Show when={props.working && props.startedAt !== null}>
          <WorkingIndicator startedAt={props.startedAt ?? Date.now()} />
        </Show>
        <Show when={props.stopped}>
          <p class="text-muted-foreground mx-auto py-1 text-xs">
            Generation stopped.
          </p>
        </Show>
      </section>
      <Show when={!following()}>
        <Button
          class="absolute bottom-3 left-1/2 -translate-x-1/2 shadow"
          onClick={() => {
            setFollowing(true);
            scrollToEnd();
          }}
          size="sm"
          type="button"
          variant="secondary"
        >
          Jump to latest ↓
        </Button>
      </Show>
    </div>
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
    <section aria-label="Notifications" class={panelClass}>
      <h2 class="mb-3 text-sm font-semibold">Notifications</h2>
      <Show
        fallback={
          <p class="text-muted-foreground text-sm">No notifications yet</p>
        }
        when={props.notifications.length > 0}
      >
        <ul class="space-y-2">
          <For each={props.notifications}>
            {(item) => (
              <li class="bg-muted rounded-md border px-3 py-2 text-sm">
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
    <section aria-label="Reminders" class={panelClass}>
      <h2 class="mb-3 text-sm font-semibold">Reminders</h2>
      <form class="space-y-3" onSubmit={onSubmit}>
        <TextField onChange={setPayload} value={payload()}>
          <TextFieldLabel>Reminder</TextFieldLabel>
          <TextFieldInput name="payload" />
        </TextField>
        <label class="grid gap-1">
          <span class={fieldLabelClass}>Repeat</span>
          <select
            class={selectClass}
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
        <label class="grid gap-1">
          <span class={fieldLabelClass}>Action</span>
          <select
            class={selectClass}
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
          <TextField onChange={setFireAt} value={fireAt()}>
            <TextFieldLabel>Date and time</TextFieldLabel>
            <TextFieldInput name="fire_at" type="datetime-local" />
          </TextField>
        </Show>
        <Show when={recurrence() !== "once"}>
          <TextField onChange={setTimeOfDay} value={timeOfDay()}>
            <TextFieldLabel>Time of day</TextFieldLabel>
            <TextFieldInput name="time_of_day" type="time" />
          </TextField>
          <TextField onChange={setTimezone} value={timezone()}>
            <TextFieldLabel>Time zone</TextFieldLabel>
            <TextFieldInput name="timezone" />
          </TextField>
        </Show>
        <Show when={recurrence() === "weekly"}>
          <label class="grid gap-1">
            <span class={fieldLabelClass}>Day of week</span>
            <select
              class={selectClass}
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
        <Button type="submit">Add reminder</Button>
      </form>
      <Show when={error()}>
        {(message) => (
          <p class="text-destructive mt-2 text-sm" role="alert">
            {message()}
          </p>
        )}
      </Show>
      <ul class="mt-3 space-y-2">
        <For each={triggersQuery.data ?? []}>
          {(trigger) => (
            <li
              aria-label={`Reminder: ${trigger.payload}`}
              class="bg-muted flex flex-wrap items-center gap-1 rounded-md border px-3 py-2 text-sm"
            >
              <span class="font-medium">{trigger.payload}</span>
              <span class="text-muted-foreground text-xs">{` · ${trigger.recurrence} · ${trigger.status}`}</span>
              <span class="text-muted-foreground text-xs">{` · next ${formatFireTime(trigger.next_fire_at)}`}</span>
              <Button
                class="ml-auto"
                onClick={() => {
                  remove(trigger.id, trigger.version);
                }}
                size="sm"
                type="button"
                variant="ghost"
              >
                Delete
              </Button>
            </li>
          )}
        </For>
      </ul>
    </section>
  );
}

function recallFeedback(outcome: AnswerOutcome): string {
  if (!outcome.correct) {
    return "Not quite — this prompt will come back sooner.";
  }
  if (outcome.completed) {
    return outcome.tethered
      ? "Correct — fully recalled, the memory is now tethered!"
      : "Correct — fully recalled, study item complete!";
  }
  return "Correct — see you next round.";
}

function RecallPanel(props: { api: TetherApi }) {
  const queryClient = useQueryClient();
  const promptsQuery = createQuery(() => ({
    queryFn: () => props.api.listDueRecallPrompts(),
    queryKey: queryKeys.recall,
  }));
  const [shownAt, setShownAt] = createSignal(Date.now());
  const [feedback, setFeedback] = createSignal<string | undefined>();
  const [error, setError] = createSignal<string | undefined>();

  // Restart the response timer whenever the set of due prompts changes, so each
  // prompt is timed from when it became visible (response time feeds scheduling).
  createEffect(() => {
    void promptsQuery.data;
    setShownAt(Date.now());
  });

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.recall });
    void queryClient.refetchQueries({ queryKey: queryKeys.recall });
  };

  const answer = (promptId: string, choiceIndex: number) => {
    const responseMs = Math.max(0, Date.now() - shownAt());
    void (async () => {
      setError(undefined);
      try {
        const outcome = await props.api.answerRecallPrompt(
          promptId,
          choiceIndex,
          responseMs,
        );
        setFeedback(recallFeedback(outcome));
        refresh();
      } catch (caught) {
        setError(
          caught instanceof Error ? caught.message : "Could not submit answer",
        );
      }
    })();
  };

  return (
    <section aria-label="Recall" class={panelClass}>
      <h2 class="mb-3 text-sm font-semibold">Recall</h2>
      <Show when={feedback()}>
        {(message) => (
          <p class="mb-2 text-sm text-emerald-600" role="status">
            {message()}
          </p>
        )}
      </Show>
      <Show when={error()}>
        {(message) => (
          <p class="text-destructive mb-2 text-sm" role="alert">
            {message()}
          </p>
        )}
      </Show>
      <Show
        fallback={
          <p class="text-muted-foreground text-sm">No recall prompts due</p>
        }
        when={(promptsQuery.data ?? []).length > 0}
      >
        <ul class="space-y-2">
          <For each={promptsQuery.data ?? []}>
            {(due) => (
              <li
                aria-label={`Recall prompt: ${due.prompt.question}`}
                class="bg-muted space-y-2 rounded-md border px-3 py-2"
              >
                <p class="text-sm font-medium">{due.prompt.question}</p>
                <span class="text-muted-foreground text-xs">{`from ${due.study_item.source_title}`}</span>
                <div class="flex flex-wrap gap-2" role="group">
                  <For each={due.prompt.choices}>
                    {(choice, choiceIndex) => (
                      <Button
                        onClick={() => {
                          answer(due.prompt.id, choiceIndex());
                        }}
                        size="sm"
                        type="button"
                        variant="outline"
                      >
                        {choice}
                      </Button>
                    )}
                  </For>
                </div>
              </li>
            )}
          </For>
        </ul>
      </Show>
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
    <section aria-label="Notification delivery" class={panelClass}>
      <h2 class="mb-3 text-sm font-semibold">Push notifications</h2>
      <Show
        fallback={<p class="text-muted-foreground text-sm">Checking…</p>}
        when={statusQuery.data}
      >
        {(status) => (
          <Show
            fallback={
              <div class="space-y-2">
                <p class="text-muted-foreground text-sm">Not subscribed</p>
                <Button disabled={busy()} onClick={enable} type="button">
                  Enable notifications
                </Button>
              </div>
            }
            when={status().subscribed}
          >
            <div class="space-y-2">
              <p class="text-sm">Subscribed</p>
              <Button
                disabled={busy()}
                onClick={disable}
                type="button"
                variant="outline"
              >
                Disable notifications
              </Button>
            </div>
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
  const [connection, setConnection] = createSignal<ConnectionStatus>("open");
  const [turn, setTurn] = createSignal<LiveTurn>(emptyTurn());
  const [messagesRefresh, setMessagesRefresh] = createSignal(0);
  const [notifications, setNotifications] = createSignal<NotifyItem[]>([]);
  const generating = createMemo(() => turn().generating);

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
  const storedMessages = createMemo<StoredMessage[]>(() =>
    (messagesQuery.data ?? []).map((message) => ({
      id: message.id,
      role: message.role,
      content: message.content,
      toolName: message.tool_name,
    })),
  );
  const rows = createMemo(() => deriveRows(storedMessages(), turn()));
  const working = createMemo(() => isAwaitingFirstToken(turn()));

  // Clear the live turn once settled history catches up. Tracks only the stored
  // messages so a refetch after `agent_end` retires the streamed rows without
  // an effect loop, while a mid-stream invalidation (generating) is ignored.
  createEffect(() => {
    const stored = messagesQuery.data;
    if (stored === undefined) {
      return;
    }
    untrack(() => {
      if (!turn().generating) {
        setTurn(emptyTurn());
      }
    });
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
    setTurn((current) => reduceFrame(current, frame, Date.now()));
    if (frame.event === "error") {
      setError(frame.detail ?? "Chat error");
    }
    // Settle from authoritative storage only when the turn finishes; per-event
    // refetching caused the flicker the seam now avoids.
    if (frame.event === "agent_end" || frame.event === "error") {
      rehydrate();
    }
  };

  onMount(() => {
    const chatBus = props.createChatBus({
      onDisconnect: rehydrate,
      onFrame: handleFrame,
      onStatus: setConnection,
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
    setError(undefined);
    setTurn(startTurn(content, Date.now()));
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

  // Enter sends; Shift+Enter keeps the default newline. Single-tenant app, so a
  // bare Enter is the expected fast path rather than chasing a submit button.
  const onMessageKeyDown: JSX.EventHandler<
    HTMLTextAreaElement,
    KeyboardEvent
  > = (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendPrompt();
    }
  };

  return (
    <main aria-labelledby="chat-title" class="flex h-screen flex-col">
      <header class="bg-card flex items-center gap-4 border-b px-5 py-3">
        <h1
          id="chat-title"
          class="mr-auto text-lg font-semibold tracking-tight"
        >
          Tether chat
        </h1>
        <Show when={conversation()}>
          {(currentConversation) => (
            <ModelSelector
              api={props.api}
              conversation={currentConversation()}
            />
          )}
        </Show>
        <Button onClick={logout} size="sm" type="button" variant="outline">
          Log out
        </Button>
      </header>
      <div class="mx-auto grid w-full max-w-6xl flex-1 grid-cols-[minmax(0,1fr)_22rem] gap-5 overflow-hidden p-5">
        <div class="flex min-h-0 flex-col gap-3">
          <Show when={connection() !== "open"}>
            <p
              class="bg-muted text-muted-foreground flex items-center gap-2 rounded-md border px-3 py-2 text-sm"
              role="status"
            >
              <span
                aria-hidden="true"
                class="bg-amber-500 inline-block size-2 animate-pulse rounded-full"
              />
              {connection() === "connecting"
                ? "Reconnecting to Tether…"
                : "Disconnected — retrying…"}
            </p>
          </Show>
          <Show when={error()}>
            {(message) => (
              <div
                class="border-destructive/40 bg-destructive/10 text-destructive flex items-start gap-2 rounded-md border px-3 py-2 text-sm"
                role="alert"
              >
                <p class="line-clamp-3 flex-1" title={message()}>
                  {message()}
                </p>
                <button
                  aria-label="Dismiss error"
                  class="shrink-0 opacity-70 hover:opacity-100"
                  onClick={() => {
                    setError(undefined);
                  }}
                  type="button"
                >
                  ✕
                </button>
              </div>
            )}
          </Show>
          <Show
            fallback={<p class="text-muted-foreground">Loading chat…</p>}
            when={!conversationsQuery.isLoading && conversation() !== undefined}
          >
            <MessageRows
              rows={rows()}
              startedAt={turn().startedAt}
              stopped={turn().stopped}
              working={working()}
            />
            <form class="space-y-2" onSubmit={onSubmit}>
              <TextField onChange={setDraft} value={draft()}>
                <TextFieldLabel>Message</TextFieldLabel>
                <TextFieldTextArea onKeyDown={onMessageKeyDown} />
              </TextField>
              <div class="flex justify-end gap-2">
                <Button disabled={generating()} type="submit">
                  Send
                </Button>
                <Button
                  disabled={!generating()}
                  onClick={abort}
                  type="button"
                  variant="outline"
                >
                  Stop
                </Button>
              </div>
            </form>
          </Show>
        </div>
        <aside class="flex min-h-0 flex-col gap-4 overflow-y-auto">
          <NotificationsPanel notifications={notifications()} />
          <RecallPanel api={props.api} />
          <TriggersPanel api={props.api} />
          <PushControl api={props.api} />
        </aside>
      </div>
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
