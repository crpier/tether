import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test, vi } from "vitest";

import { ApiError } from "../host/error";
import {
  FakeHost,
  conversation,
  message,
  renderApp,
  textarea,
} from "../testing/harness";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// A scripted stand-in for the browser `MediaRecorder`, driving the voice
// composer's `VoiceComposerControls` (issue #19) without a real microphone.
// `stop()` synchronously delivers a chunk and fires `onstop`, matching how a
// real recorder flushes its final `dataavailable` before stopping.
class FakeMediaRecorder {
  static instances: FakeMediaRecorder[] = [];
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;

  constructor() {
    FakeMediaRecorder.instances.push(this);
  }

  start(): void {
    // No-op: the fake doesn't actually capture audio.
  }

  stop(): void {
    this.ondataavailable?.({ data: new Blob(["chunk"]) });
    this.onstop?.();
  }
}

function stubVoiceRecording(): void {
  FakeMediaRecorder.instances = [];
  vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
  const fakeStream = {
    getTracks: () => [],
  } as unknown as MediaStream;
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia: () => Promise.resolve(fakeStream) },
  });
}

function stubSpeechEndDetection(): {
  sample: (level: number, nowMs: number) => void;
} {
  let level = 0;
  let nowMs = 0;
  let nextTimer = 0;
  const timers = new Map<number, () => void>();

  vi.spyOn(performance, "now").mockImplementation(() => nowMs);
  vi.spyOn(window, "setInterval").mockImplementation(((
    handler: TimerHandler,
  ) => {
    const timer = ++nextTimer;
    timers.set(timer, handler as () => void);
    return timer;
  }) as unknown as typeof window.setInterval);
  vi.spyOn(window, "clearInterval").mockImplementation(((timer?: number) => {
    if (timer !== undefined) {
      timers.delete(timer);
    }
  }) as unknown as typeof window.clearInterval);

  class FakeAudioContext {
    createAnalyser() {
      return {
        fftSize: 0,
        getFloatTimeDomainData: (samples: Float32Array) => {
          samples.fill(level);
        },
      };
    }

    createMediaStreamSource() {
      return {
        connect: () => undefined,
        disconnect: () => undefined,
      };
    }

    close(): Promise<void> {
      return Promise.resolve();
    }

    resume(): Promise<void> {
      return Promise.resolve();
    }
  }

  vi.stubGlobal("AudioContext", FakeAudioContext);
  return {
    sample: (nextLevel, nextNowMs) => {
      level = nextLevel;
      nowMs = nextNowMs;
      for (const handler of [...timers.values()]) {
        handler();
      }
    },
  };
}

function latestFakeRecorder(): FakeMediaRecorder {
  const recorder = FakeMediaRecorder.instances.at(-1);
  if (recorder === undefined) {
    throw new Error("expected a recorder to have been created");
  }
  return recorder;
}

class FakeAudio {
  currentTime = 0;
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(
    public readonly text: string,
    private readonly playback: FakeSpeechPlayback,
  ) {}

  pause(): void {
    this.playback.cancellations += 1;
  }

  play(): Promise<void> {
    return Promise.resolve();
  }
}

class FakeSpeechPlayback {
  cancellations = 0;
  spoken: FakeAudio[] = [];
  private objectUrls = new Map<string, string>();
  private sequence = 0;

  createObjectURL(blob: Blob & { speechText?: string }): string {
    const url = `blob:speech-${String(++this.sequence)}`;
    this.objectUrls.set(url, blob.speechText ?? "");
    return url;
  }

  createAudio(source: string): FakeAudio {
    const audio = new FakeAudio(this.objectUrls.get(source) ?? "", this);
    this.spoken.push(audio);
    return audio;
  }

  finishSpeaking(): void {
    this.spoken.at(-1)?.onended?.();
  }
}

function stubSpeech(): FakeSpeechPlayback {
  const fake = new FakeSpeechPlayback();
  vi.spyOn(URL, "createObjectURL").mockImplementation((blob) =>
    fake.createObjectURL(blob instanceof Blob ? blob : new Blob()),
  );
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
  function AudioStub(source: string): FakeAudio {
    return fake.createAudio(source);
  }
  vi.stubGlobal("Audio", AudioStub);
  return fake;
}

describe("Chat view", () => {
  test("keeps the page title accessible without visible header chrome", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);

    const title = await screen.findByRole("heading", { name: "Tether chat" });

    expect(title).toHaveClass("sr-only");
    expect(title.closest("header")).toBeNull();
  });

  test("renders generic chat rows and composer through Kitn", async () => {
    const host = new FakeHost({
      authenticated: true,
      messages: [
        message({ content: "hello", id: "user-host-id", role: "user", seq: 1 }),
        message({
          content: "hi there",
          id: "assistant-host-id",
          role: "assistant",
          seq: 2,
        }),
      ],
    });
    renderApp(host);

    const user = await screen.findByLabelText("You message");
    const assistant = screen.getByLabelText("Tether message");
    const composer = screen.getByRole("group", { name: "Message composer" });

    expect(user).toHaveAttribute("data-role", "user");
    expect(user).toHaveAttribute("data-message-id", "user-host-id");
    expect(within(user).getByText("hello")).toHaveClass("chat-message-plain");
    expect(assistant).toHaveAttribute("data-role", "assistant");
    expect(assistant).toHaveAttribute("data-message-id", "assistant-host-id");
    expect(composer).toHaveAttribute("data-prompt-input");
    const transcript = screen.getByRole("log", { name: "Chat transcript" });
    expect(transcript).toHaveAttribute("tabindex", "0");
    Object.defineProperties(transcript, {
      clientHeight: { configurable: true, value: 100 },
      scrollHeight: { configurable: true, value: 1000 },
      scrollTop: { configurable: true, value: 0, writable: true },
    });
    fireEvent.scroll(transcript);
    expect(
      screen.getByRole("button", { name: "Scroll to bottom" }),
    ).toBeInTheDocument();
  });

  test("does not expose a destructive transcript reset", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);

    await screen.findByRole("heading", { name: "Tether chat" });

    expect(
      screen.queryByRole("button", { name: "New chat" }),
    ).not.toBeInTheDocument();
  });

  test("shows only the confirmed loaded skill count in the header", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);
    await screen.findByRole("heading", { name: "Tether chat" });

    bus.emit({
      conversation_id: conversation.id,
      event: "skill_status",
      loaded_count: 2,
      type: "chat",
    });

    expect(await screen.findByText("Skills loaded · 2")).toBeInTheDocument();
    expect(screen.queryByText("grilling")).not.toBeInTheDocument();
    expect(screen.queryByText("writing-great-skills")).not.toBeInTheDocument();
  });

  test("hides skill status before a runtime confirms loading", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);

    await screen.findByRole("heading", { name: "Tether chat" });

    expect(screen.queryByText(/Skills loaded/)).not.toBeInTheDocument();
  });

  test("requests context usage for the current pi session", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);

    await screen.findByRole("combobox", { name: "Model profile" });

    expect(bus.statusRequests).toEqual([conversation.id]);
  });

  test("shows pi context usage only after fifty thousand tokens", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);
    await screen.findByRole("heading", { name: "Tether chat" });
    await screen.findByRole("combobox", { name: "Model profile" });

    bus.emit({
      context_percent: 24,
      context_tokens: 48_000,
      context_window: 200_000,
      conversation_id: conversation.id,
      event: "session_status",
      type: "chat",
    });
    expect(screen.queryByText("48k context")).not.toBeInTheDocument();

    bus.emit({
      context_percent: 31.55,
      context_tokens: 63_100,
      context_window: 200_000,
      conversation_id: conversation.id,
      event: "session_status",
      type: "chat",
    });

    const contextTrigger = await screen.findByRole("button", {
      name: "31.6% Model context usage",
    });
    contextTrigger.focus();
    expect(
      await screen.findByRole("progressbar", { name: "Context usage" }),
    ).toHaveAttribute("aria-valuenow", "63100");
    expect(screen.getByText("63K / 200K")).toBeInTheDocument();
  });

  test("warns when pi working context nears capacity", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);
    await screen.findByRole("combobox", { name: "Model profile" });

    bus.emit({
      context_percent: 91,
      context_tokens: 182_000,
      context_window: 200_000,
      conversation_id: conversation.id,
      event: "session_status",
      type: "chat",
    });

    expect(
      await screen.findByRole("button", {
        name: "91% Model context usage",
      }),
    ).toHaveClass("text-destructive");
  });

  test("hides a confirmed zero skill count", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);
    await screen.findByRole("heading", { name: "Tether chat" });

    bus.emit({
      conversation_id: conversation.id,
      event: "skill_status",
      loaded_count: 0,
      type: "chat",
    });

    expect(screen.queryByText(/Skills loaded/)).not.toBeInTheDocument();
  });

  test("rehydrates settled chat history", async () => {
    const host = new FakeHost({
      authenticated: true,
      messages: [
        message({ content: "remember aisle seats", role: "user", seq: 1 }),
        message({
          content: "capture",
          role: "tool",
          seq: 2,
          tool_args: { content: "aisle seats" },
          tool_name: "capture",
          tool_result: { ok: true },
        }),
        message({
          content: "Captured that preference.",
          role: "assistant",
          seq: 3,
        }),
      ],
    });
    renderApp(host);

    expect(await screen.findByText("remember aisle seats")).toBeInTheDocument();
    expect(screen.getByText("Used capture")).toBeInTheDocument();
    expect(screen.getByText("Captured that preference.")).toBeInTheDocument();

    // Settled tool rows keep Kitn's disclosure with persisted input and output.
    fireEvent.click(screen.getByRole("button", { name: /Used capture/ }));
    expect(screen.getByText("aisle seats")).toBeInTheDocument();
    expect(screen.getByText(/"ok": true/)).toBeInTheDocument();
  });

  test("marks an unfinished persisted turn as recoverable instead of fresh-session-ready", async () => {
    const host = new FakeHost({
      authenticated: true,
      messages: [
        message({ content: "please investigate", role: "user", seq: 1 }),
        message({ content: "checking tools", role: "reasoning", seq: 2 }),
        message({
          content: "search",
          role: "tool",
          seq: 3,
          tool_name: "search",
        }),
      ],
    });
    host.chat.storedConversation = {
      ...conversation,
      latest_activity: new Date(Date.now() - 10 * 60 * 1000).toISOString(),
      session_gap_seconds: 300,
    };
    renderApp(host);

    expect(await screen.findByText("please investigate")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Previous turn did not finish. Send a new message to recover.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Next message starts a fresh working session"),
    ).not.toBeInTheDocument();
  });

  test("hydrates durable running and pending state and Stop targets the running turn", async () => {
    const host = new FakeHost({ authenticated: true });
    host.chat.storedConversation = {
      ...conversation,
      pending_turn_count: 2,
      running_turn_id: "018f0000-0000-7000-8000-000000000099",
    };
    const bus = renderApp(host, undefined, { path: "/chat" });

    expect(await screen.findByLabelText("Tether working")).toBeVisible();
    expect(screen.getByText("2 messages queued")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));

    expect(bus.sent.at(-1)).toEqual({
      conversationId: conversation.id,
      turnId: "018f0000-0000-7000-8000-000000000099",
      type: "abort",
    });
  });

  test("hides Stop when no generation is active", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);

    await screen.findByLabelText("Message");

    expect(
      screen.queryByRole("button", { name: "Stop" }),
    ).not.toBeInTheDocument();
  });

  test("sends prompts and renders streamed assistant deltas", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);

    const messageBox = textarea(await screen.findByLabelText("Message"));
    fireEvent.input(messageBox, { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(bus.sent).toEqual([
      {
        content: "Hello",
        conversationId: conversation.id,
        replyMode: "text",
        type: "prompt",
      },
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

  test("preserves the live Kitn message identity while text streams", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);

    fireEvent.input(textarea(await screen.findByLabelText("Message")), {
      target: { value: "stream this" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    bus.emit({
      conversation_id: conversation.id,
      event: "message_start",
      type: "chat",
    });
    bus.emit({
      conversation_id: conversation.id,
      delta: "first ",
      event: "text_delta",
      type: "chat",
    });

    const liveMessage = await screen.findByLabelText("Tether message");
    bus.emit({
      conversation_id: conversation.id,
      delta: "second",
      event: "text_delta",
      type: "chat",
    });

    expect(screen.getByLabelText("Tether message")).toBe(liveMessage);
    expect(liveMessage).toHaveTextContent("first second");
  });

  test("copies a transcript message", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const host = new FakeHost({
      authenticated: true,
      messages: [message({ content: "Keep this text", role: "assistant" })],
    });
    renderApp(host);

    fireEvent.click(
      await screen.findByRole("button", { name: "Copy message" }),
    );

    expect(writeText).toHaveBeenCalledWith("Keep this text");
  });

  test("omits the unused quote action", async () => {
    const host = new FakeHost({
      authenticated: true,
      messages: [message({ content: "Keep this concise", role: "assistant" })],
    });
    renderApp(host);

    await screen.findByText("Keep this concise");
    expect(
      screen.queryByRole("button", { name: "Quote message" }),
    ).not.toBeInTheDocument();
  });

  test("records explicit product feedback from a settled user message", async () => {
    const source = message({
      content: "The model selector is confusing.",
      role: "user",
      seq: 1,
    });
    const host = new FakeHost({ authenticated: true, messages: [source] });
    renderApp(host);

    const feedbackButton = await screen.findByRole("button", {
      name: "Record product feedback",
    });
    const copyButton = screen.getByRole("button", { name: "Copy message" });
    expect(feedbackButton).toHaveTextContent(/^$/);
    expect(feedbackButton.querySelector("svg")).toBeInTheDocument();
    expect(copyButton).toHaveTextContent(/^$/);
    expect(copyButton.querySelector("svg")).toBeInTheDocument();
    expect(feedbackButton.parentElement).toHaveClass("absolute", "right-2");
    fireEvent.click(feedbackButton);
    fireEvent.input(
      screen.getByRole("textbox", { name: "Expected behavior" }),
      {
        target: { value: "Model selection should name the active profile." },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Save feedback" }));

    await waitFor(() => {
      expect(host.productObservations.recordCalls).toEqual([
        {
          conversationId: conversation.id,
          interpretation: "Model selection should name the active profile.",
          messageId: source.id,
        },
      ]);
    });
    expect(await screen.findByText("Feedback recorded.")).toBeInTheDocument();
  });

  test("renders streamed answers as markdown", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);

    fireEvent.input(textarea(await screen.findByLabelText("Message")), {
      target: { value: "format please" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    bus.emit({
      conversation_id: conversation.id,
      event: "message_start",
      type: "chat",
    });
    bus.emit({
      conversation_id: conversation.id,
      delta: "**bold** word",
      event: "text_delta",
      type: "chat",
    });

    const strong = await screen.findByText("bold");
    expect(strong.tagName).toBe("STRONG");
  });

  test("opens cited Evidence from a settled health answer", async () => {
    const uri = "tether://health-connect/sleep/sleep-record@v7";
    const host = new FakeHost({
      authenticated: true,
      evidence: [
        {
          duration_minutes: 480,
          end_time: "2026-08-22T06:00:00Z",
          kind: "health_connect_sleep",
          record_uid: "sleep-record",
          stage_minutes: { deep: 120, rem: 90 },
          start_time: "2026-08-21T22:00:00Z",
          title: "Night sleep",
          uri,
          version_id: 7,
        },
      ],
      messages: [
        message({
          content: `Eight hours recorded. [source](${uri})`,
          role: "assistant",
          seq: 1,
        }),
      ],
    });
    renderApp(host);

    fireEvent.click(await screen.findByRole("button", { name: "source" }));

    const inspector = await screen.findByRole("dialog", {
      name: "Evidence inspector",
    });
    expect(inspector).toHaveTextContent("Night sleep");
    expect(inspector).toHaveTextContent("8 hr");
    expect(inspector).toHaveTextContent("Deep2 hr");
  });

  test("shows inline tool activity transitioning to done", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);

    fireEvent.input(textarea(await screen.findByLabelText("Message")), {
      target: { value: "use a tool" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    bus.emit({
      conversation_id: conversation.id,
      event: "tool_start",
      tool_id: "t1",
      tool_name: "search",
      type: "chat",
    });
    expect(await screen.findByText("Using search…")).toBeInTheDocument();

    bus.emit({
      conversation_id: conversation.id,
      event: "tool_end",
      tool_id: "t1",
      tool_name: "search",
      type: "chat",
    });
    expect(await screen.findByText("Used search")).toBeInTheDocument();
  });

  test("groups consecutive tools with human-readable summaries", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);

    fireEvent.input(textarea(await screen.findByLabelText("Message")), {
      target: { value: "check my email" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    bus.emit({
      conversation_id: conversation.id,
      event: "tool_start",
      tool_id: "search-1",
      tool_name: "search_gmail",
      type: "chat",
    });
    bus.emit({
      conversation_id: conversation.id,
      event: "tool_end",
      tool_id: "search-1",
      tool_name: "search_gmail",
      tool_result: {
        details: { result: { messages: [{}, {}] } },
      },
      type: "chat",
    });
    bus.emit({
      conversation_id: conversation.id,
      event: "tool_start",
      tool_id: "read-1",
      tool_name: "read_gmail_message",
      type: "chat",
    });
    bus.emit({
      conversation_id: conversation.id,
      event: "tool_end",
      tool_id: "read-1",
      tool_name: "read_gmail_message",
      type: "chat",
    });

    expect(await screen.findByText("Searched Gmail · 2 results")).toBeVisible();
    expect(
      screen.getAllByText("Completed")[0]?.closest(".chat-tool-trace-complete"),
    ).not.toBeNull();
    expect(screen.getByText("Read email")).toBeVisible();
    const activity = screen.getByLabelText("Tool activity");
    expect(screen.getAllByLabelText("Tool activity")).toHaveLength(1);
    expect(activity).toHaveClass("chat-tool-group");
    expect(activity.querySelectorAll(".chat-tool-trace")).toHaveLength(2);
  });

  test("undoes a completed archive from its action receipt", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);

    fireEvent.input(textarea(await screen.findByLabelText("Message")), {
      target: { value: "archive it" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    bus.emit({
      conversation_id: conversation.id,
      event: "tool_start",
      tool_id: "archive-1",
      tool_name: "archive_gmail_message",
      type: "chat",
    });
    bus.emit({
      conversation_id: conversation.id,
      event: "tool_end",
      tool_id: "archive-1",
      tool_name: "archive_gmail_message",
      tool_result: {
        details: {
          result: { message_id: "message-1", outcome: "done" },
        },
      },
      type: "chat",
    });

    const undo = await screen.findByRole("button", { name: "Undo archive" });
    expect(undo.parentElement).toHaveClass("items-center");
    expect(undo.previousElementSibling).toHaveClass("chat-tool-trace");
    fireEvent.click(undo);

    await waitFor(() => {
      expect(host.chat.undoGmailArchiveCalls).toEqual(["message-1"]);
    });
    expect(await screen.findByText("Restored to Inbox")).toBeInTheDocument();
  });

  test("surfaces tool call args and result inline", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);

    fireEvent.input(textarea(await screen.findByLabelText("Message")), {
      target: { value: "use a tool" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    bus.emit({
      conversation_id: conversation.id,
      event: "tool_start",
      tool_args: { q: "needle", limit: 5 },
      tool_id: "t1",
      tool_name: "search",
      type: "chat",
    });
    expect(await screen.findByText("needle")).toBeInTheDocument();

    bus.emit({
      conversation_id: conversation.id,
      event: "tool_end",
      tool_id: "t1",
      tool_name: "search",
      tool_result: { kind: "collection" },
      type: "chat",
    });
    expect(await screen.findByText(/"kind": "collection"/)).toBeInTheDocument();
  });

  test("shows a working indicator until the first token arrives", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);

    fireEvent.input(textarea(await screen.findByLabelText("Message")), {
      target: { value: "think" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    bus.emit({
      conversation_id: conversation.id,
      event: "message_start",
      type: "chat",
    });

    expect(await screen.findByLabelText("Tether working")).toBeInTheDocument();

    bus.emit({
      conversation_id: conversation.id,
      delta: "done",
      event: "text_delta",
      type: "chat",
    });
    await waitFor(() => {
      expect(screen.queryByLabelText("Tether working")).not.toBeInTheDocument();
    });
  });

  test("keeps reasoning in a separate row from the answer", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);

    fireEvent.input(textarea(await screen.findByLabelText("Message")), {
      target: { value: "reason" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    bus.emit({
      conversation_id: conversation.id,
      event: "message_start",
      type: "chat",
    });
    bus.emit({
      conversation_id: conversation.id,
      delta: "pondering",
      event: "thinking_delta",
      type: "chat",
    });
    bus.emit({
      conversation_id: conversation.id,
      delta: "the answer",
      event: "text_delta",
      type: "chat",
    });

    const reasoning = await screen.findByLabelText(/Tether reasoning/);
    expect(within(reasoning).getByText("pondering")).toBeInTheDocument();
    expect(screen.getByText("the answer")).toBeInTheDocument();
  });

  test("gives each thinking disclosure a distinct accessible name", async () => {
    const host = new FakeHost({
      authenticated: true,
      messages: [
        message({ content: "first prompt", role: "user", seq: 1 }),
        message({ content: "first chain", role: "reasoning", seq: 2 }),
        message({ content: "first answer", role: "assistant", seq: 3 }),
        message({ content: "second chain", role: "reasoning", seq: 4 }),
      ],
    });
    renderApp(host);

    const first = await screen.findByRole("button", {
      name: "Thinking details for transcript item 2",
    });
    const second = screen.getByRole("button", {
      name: "Thinking details for transcript item 4",
    });

    expect(first).toHaveAccessibleName(
      "Thinking details for transcript item 2",
    );
    expect(second).toHaveAccessibleName(
      "Thinking details for transcript item 4",
    );
  });

  test("error frames show a dismissible banner", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);

    await screen.findByRole("heading", { name: "Tether chat" });
    bus.emit({
      conversation_id: conversation.id,
      detail: "No API key for provider",
      event: "error",
      type: "chat",
    });

    const alert = await screen.findByRole("alert");
    expect(
      within(alert).getByText("No API key for provider"),
    ).toBeInTheDocument();

    fireEvent.click(
      within(alert).getByRole("button", { name: "Dismiss error" }),
    );
    await waitFor(() => {
      expect(
        screen.queryByText("No API key for provider"),
      ).not.toBeInTheDocument();
    });
  });

  test("Enter sends the prompt", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);

    const messageBox = textarea(await screen.findByLabelText("Message"));
    fireEvent.input(messageBox, { target: { value: "Hello" } });
    fireEvent.keyDown(messageBox, { key: "Enter" });

    expect(bus.sent).toEqual([
      {
        content: "Hello",
        conversationId: conversation.id,
        replyMode: "text",
        type: "prompt",
      },
    ]);
    expect(screen.getByText("Hello")).toBeInTheDocument();
  });

  test("sending during generation visibly queues the message", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);

    const messageBox = textarea(await screen.findByLabelText("Message"));
    fireEvent.input(messageBox, { target: { value: "First" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    fireEvent.input(messageBox, { target: { value: "Follow up" } });
    fireEvent.click(screen.getByRole("button", { name: "Queue message" }));

    expect(bus.sent).toEqual([
      {
        content: "First",
        conversationId: conversation.id,
        replyMode: "text",
        type: "prompt",
      },
      {
        content: "Follow up",
        conversationId: conversation.id,
        replyMode: "text",
        type: "prompt",
      },
    ]);
    const queue = screen.getByRole("region", { name: "Queued messages" });
    expect(within(queue).getByText("Follow up")).toBeInTheDocument();
    expect(messageBox.value).toBe("");
  });

  test("queued messages are sent in order as turns finish", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);
    const messageBox = textarea(await screen.findByLabelText("Message"));

    for (const content of ["First", "Second", "Third"]) {
      fireEvent.input(messageBox, { target: { value: content } });
      fireEvent.keyDown(messageBox, { key: "Enter" });
    }
    expect(
      bus.sent
        .filter((entry) => entry.type === "prompt")
        .map((entry) => entry.content),
    ).toEqual(["First", "Second", "Third"]);

    for (const [turnId, status] of [
      ["turn-1", "running"],
      ["turn-2", "pending"],
      ["turn-3", "pending"],
    ] as const) {
      bus.emit({
        conversation_id: conversation.id,
        event: "turn_queued",
        status,
        turn_id: turnId,
        type: "chat",
      });
    }
    bus.emit({
      conversation_id: conversation.id,
      event: "user_message",
      turn_id: "turn-1",
      type: "chat",
    });
    expect(bus.sent.filter((entry) => entry.type === "prompt")).toHaveLength(3);
    expect(screen.queryByText("Second")).toBeInTheDocument();

    bus.emit({
      conversation_id: conversation.id,
      event: "user_message",
      turn_id: "turn-2",
      type: "chat",
    });
    bus.emit({
      conversation_id: conversation.id,
      event: "user_message",
      turn_id: "turn-3",
      type: "chat",
    });
    expect(bus.sent.filter((entry) => entry.type === "prompt")).toHaveLength(3);
    expect(
      screen.queryByRole("region", { name: "Queued messages" }),
    ).not.toBeInTheDocument();
  });

  test("queued messages can be edited and cancelled", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);
    const messageBox = textarea(await screen.findByLabelText("Message"));

    fireEvent.input(messageBox, { target: { value: "First" } });
    fireEvent.keyDown(messageBox, { key: "Enter" });
    fireEvent.input(messageBox, { target: { value: "Needs editing" } });
    fireEvent.keyDown(messageBox, { key: "Enter" });
    bus.emit({
      conversation_id: conversation.id,
      event: "turn_queued",
      status: "running",
      turn_id: "turn-1",
      type: "chat",
    });
    bus.emit({
      conversation_id: conversation.id,
      event: "turn_queued",
      status: "pending",
      turn_id: "turn-2",
      type: "chat",
    });
    bus.emit({
      conversation_id: conversation.id,
      event: "user_message",
      turn_id: "turn-1",
      type: "chat",
    });

    const queuedMessage = screen.getByRole("article", {
      name: "Queued message 1",
    });
    fireEvent.click(
      within(queuedMessage).getByRole("button", { name: /^Edit/ }),
    );
    const editor = textarea(
      within(queuedMessage).getByRole("textbox", {
        name: "Edit queued message 1",
      }),
    );
    fireEvent.input(editor, { target: { value: "Edited follow up" } });
    fireEvent.click(
      within(queuedMessage).getByRole("button", { name: "Save changes" }),
    );
    expect(screen.getByText("Edited follow up")).toBeInTheDocument();
    bus.emit({
      conversation_id: conversation.id,
      event: "turn_queued",
      status: "pending",
      turn_id: "turn-3",
      type: "chat",
    });

    fireEvent.click(
      within(
        screen.getByRole("article", { name: "Queued message 1" }),
      ).getByRole("button", { name: "Cancel message" }),
    );
    expect(
      screen.queryByRole("region", { name: "Queued messages" }),
    ).not.toBeInTheDocument();
    expect(bus.sent.filter((entry) => entry.type === "prompt")).toHaveLength(3);
    expect(bus.sent.filter((entry) => entry.type === "abort")).toHaveLength(2);
  });

  test("Send now stops the active turn before sending the chosen message", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);
    const messageBox = textarea(await screen.findByLabelText("Message"));

    for (const content of ["First", "Second", "Urgent correction"]) {
      fireEvent.input(messageBox, { target: { value: content } });
      fireEvent.keyDown(messageBox, { key: "Enter" });
    }
    for (const [turnId, status] of [
      ["turn-1", "running"],
      ["turn-2", "pending"],
      ["turn-3", "pending"],
    ] as const) {
      bus.emit({
        conversation_id: conversation.id,
        event: "turn_queued",
        status,
        turn_id: turnId,
        type: "chat",
      });
    }
    bus.emit({
      conversation_id: conversation.id,
      event: "user_message",
      turn_id: "turn-1",
      type: "chat",
    });
    const urgent = screen.getByRole("article", { name: "Queued message 2" });
    fireEvent.click(within(urgent).getByRole("button", { name: "Send now" }));

    expect(bus.sent.at(-1)).toEqual({
      conversationId: conversation.id,
      turnId: "turn-1",
      type: "abort",
    });
    expect(
      within(
        screen.getByRole("article", { name: "Queued message 1" }),
      ).getByText("Urgent correction"),
    ).toBeInTheDocument();

    bus.emit({
      conversation_id: conversation.id,
      event: "abort_ack",
      type: "chat",
    });
    expect(bus.sent.at(-1)?.type).toBe("abort");

    bus.emit({
      conversation_id: conversation.id,
      event: "agent_end",
      type: "chat",
    });
    bus.emit({
      conversation_id: conversation.id,
      event: "turn_ended",
      status: "cancelled",
      type: "chat",
    });
    expect(
      bus.sent
        .filter((entry) => entry.type === "prompt")
        .map((entry) => entry.content),
    ).toEqual([
      "First",
      "Second",
      "Urgent correction",
      "Urgent correction",
      "Second",
    ]);
    expect(screen.getByText("Second")).toBeInTheDocument();
  });

  test("a prompt rejected before acceptance remains queued for retry", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);
    const messageBox = textarea(await screen.findByLabelText("Message"));
    fireEvent.input(messageBox, { target: { value: "Do not lose this" } });
    fireEvent.keyDown(messageBox, { key: "Enter" });

    bus.emit({
      conversation_id: conversation.id,
      detail: "generation already running",
      event: "error",
      type: "chat",
    });

    const queue = screen.getByRole("region", { name: "Queued messages" });
    expect(within(queue).getByText("Do not lose this")).toBeInTheDocument();
    fireEvent.click(within(queue).getByRole("button", { name: "Send now" }));
    expect(bus.sent).toEqual([
      {
        content: "Do not lose this",
        conversationId: conversation.id,
        replyMode: "text",
        type: "prompt",
      },
      {
        content: "Do not lose this",
        conversationId: conversation.id,
        replyMode: "text",
        type: "prompt",
      },
    ]);
  });

  test("Shift+Enter inserts a newline instead of sending", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);

    const messageBox = textarea(await screen.findByLabelText("Message"));
    fireEvent.input(messageBox, { target: { value: "line one" } });
    fireEvent.keyDown(messageBox, { key: "Enter", shiftKey: true });

    expect(bus.sent).toEqual([]);
  });

  test("stop aborts an in-flight generation", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);

    fireEvent.input(textarea(await screen.findByLabelText("Message")), {
      target: { value: "Keep going" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    bus.emit({
      conversation_id: conversation.id,
      event: "user_message",
      turn_id: "turn-1",
      type: "chat",
    });
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));

    expect(bus.sent).toEqual([
      {
        content: "Keep going",
        conversationId: conversation.id,
        replyMode: "text",
        type: "prompt",
      },
      { conversationId: conversation.id, turnId: "turn-1", type: "abort" },
    ]);
  });

  test("Send is disabled until the input has non-whitespace text", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);

    const send = await screen.findByRole("button", { name: "Send" });
    expect(send).toBeDisabled();

    const messageBox = textarea(screen.getByLabelText("Message"));
    fireEvent.input(messageBox, { target: { value: "   " } });
    expect(send).toBeDisabled();

    fireEvent.input(messageBox, { target: { value: "hello" } });
    expect(send).toBeEnabled();
  });

  test("a stopped generation keeps an interrupted marker on the transcript", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);

    fireEvent.input(textarea(await screen.findByLabelText("Message")), {
      target: { value: "Keep going" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    bus.emit({
      conversation_id: conversation.id,
      delta: "partial ans",
      event: "text_delta",
      type: "chat",
    });
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    bus.emit({
      conversation_id: conversation.id,
      event: "abort_ack",
      type: "chat",
    });

    expect(await screen.findByText("Generation stopped.")).toBeInTheDocument();

    // Settled history arriving must not wipe the marker off the partial reply.
    bus.emit({ keys: ["messages"], type: "invalidate" });
    await waitFor(() => {
      expect(host.chat.messageCalls).toBeGreaterThan(1);
    });
    expect(screen.getByText("Generation stopped.")).toBeInTheDocument();
  });

  test("invalidate frames refetch named query keys", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);

    await waitFor(() => {
      expect(host.chat.messageCalls).toBe(1);
    });

    bus.emit({ keys: ["messages"], type: "invalidate" });

    await waitFor(() => {
      expect(host.chat.messageCalls).toBeGreaterThan(1);
    });
  });

  test("puts the model selector beside working-session status", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);

    const context = await screen.findByRole("group", {
      name: "Composer context",
    });
    const composer = screen.getByRole("group", { name: "Message composer" });

    expect(
      within(context).getByText("Next message starts a fresh working session"),
    ).toBeInTheDocument();
    expect(within(context).getByRole("separator")).toBeInTheDocument();
    expect(
      await within(context).findByRole("combobox", { name: "Model profile" }),
    ).toBeInTheDocument();
    expect(
      within(composer).queryByRole("combobox", { name: "Model profile" }),
    ).not.toBeInTheDocument();
    expect(
      within(composer).getByRole("textbox", { name: "Message" }),
    ).toBeInTheDocument();
    expect(
      within(composer).getByRole("button", {
        name: "Start voice conversation",
      }),
    ).toBeInTheDocument();
    expect(
      within(composer).getByRole("button", { name: "Send" }),
    ).toBeInTheDocument();
  });

  test("shows the selected model name in a compact selector", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);

    const selector = await screen.findByRole("combobox", {
      name: "Model profile",
    });

    expect(selector).toHaveValue("gpt-5.6-luna");
    expect(selector).toHaveDisplayValue("GPT-5.6 Luna · no thinking");
    expect(selector).toHaveClass("truncate");
  });

  test("selecting a profile persists it without moving the transcript", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    const transcript = await screen.findByLabelText("Chat transcript");
    const selector = await screen.findByRole("combobox", {
      name: "Model profile",
    });
    transcript.scrollTop = 123;

    fireEvent.change(selector, { target: { value: "gpt-5.6-sol" } });

    await waitFor(() => {
      expect(host.chat.selectedModel).toBe("gpt-5.6-sol");
    });
    expect(selector).toHaveDisplayValue("GPT-5.6 Sol · medium thinking");
    expect(transcript.scrollTop).toBe(123);
  });

  test("serializes rapid profile commits and keeps the latest selection", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    const selector = await screen.findByRole("combobox", {
      name: "Model profile",
    });

    fireEvent.change(selector, { target: { value: "gpt-5.6-sol" } });
    fireEvent.change(selector, { target: { value: "gpt-5.6-luna" } });

    await waitFor(() => {
      expect(host.chat.selectedModel).toBe("gpt-5.6-luna");
    });
  });

  test("uses the catalog default without an explicit conversation profile", async () => {
    const host = new FakeHost({ authenticated: true });
    host.chat.storedConversation = {
      ...host.chat.storedConversation,
      selected_model: null,
    };
    renderApp(host);

    const selector = await screen.findByRole("combobox", {
      name: "Model profile",
    });

    expect(selector).toHaveValue("gpt-5.6-luna");
    expect(selector).toHaveDisplayValue("GPT-5.6 Luna · no thinking");
  });

  test("shows a fixed profile when the catalog has one entry", async () => {
    const host = new FakeHost({ authenticated: true });
    host.chat.storedConversation = {
      ...host.chat.storedConversation,
      selected_model: "local",
    };
    host.chat.listModels = () =>
      Promise.resolve({
        default_model: "local",
        models: [
          {
            display_name: "Local deterministic model",
            id: "local",
            model_id: "tether-local-faux",
            provider: "faux",
            thinking_level: null,
          },
        ],
      });
    renderApp(host);

    const selector = await screen.findByRole("combobox", {
      name: "Model profile",
    });

    expect(selector).toBeDisabled();
    expect(selector).toHaveValue("local");
    expect(selector).toHaveDisplayValue("Local deterministic model");
  });

  test("reports an empty model catalog without rendering a selector", async () => {
    const host = new FakeHost({ authenticated: true });
    host.chat.listModels = () =>
      Promise.resolve({ default_model: null, models: [] });
    renderApp(host);

    expect(
      await screen.findByText("No model profiles available."),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", { name: "Model profile" }),
    ).not.toBeInTheDocument();
  });

  test("does not mount transcript search", async () => {
    const host = new FakeHost({
      authenticated: true,
      messages: [
        message({ content: "ordinary text", role: "assistant", seq: 1 }),
      ],
    });
    renderApp(host);

    expect(await screen.findByText("ordinary text")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Search transcript" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("searchbox", { name: "Search transcript" }),
    ).not.toBeInTheDocument();
  });

  test("only fetches the latest page of history by default", async () => {
    const host = new FakeHost({
      authenticated: true,
      messages: [message({ content: "hi", role: "user", seq: 1 })],
    });
    renderApp(host);

    await waitFor(() => {
      expect(host.chat.listMessagesCalls.length).toBeGreaterThan(0);
    });
    expect(host.chat.listMessagesCalls[0]).toEqual({
      limit: 30,
      beforeSeq: undefined,
    });
  });

  test("scrolling near the top loads and prepends the older page", async () => {
    const messages = Array.from({ length: 32 }, (_, index) =>
      message({
        content: `msg-${(index + 1).toString()}`,
        role: "user",
        seq: index + 1,
      }),
    );
    const host = new FakeHost({ authenticated: true, messages });
    renderApp(host);

    // The default page is the newest 30 rows (seq 3..32); the oldest two are
    // not yet loaded.
    expect(await screen.findByText("msg-32")).toBeInTheDocument();
    expect(screen.queryByText("msg-1")).not.toBeInTheDocument();

    fireEvent.scroll(screen.getByLabelText("Chat transcript"));

    expect(await screen.findByText("msg-1")).toBeInTheDocument();
    expect(screen.getByText("msg-2")).toBeInTheDocument();
    await waitFor(() => {
      expect(host.chat.listMessagesCalls).toEqual([
        { limit: 30, beforeSeq: undefined },
        { limit: 30, beforeSeq: 3 },
      ]);
    });
  });

  test("stops fetching once the oldest page is smaller than the limit", async () => {
    const messages = [message({ content: "only one", role: "user", seq: 1 })];
    const host = new FakeHost({ authenticated: true, messages });
    renderApp(host);

    expect(await screen.findByText("only one")).toBeInTheDocument();
    const callsAfterInitialLoad = host.chat.listMessagesCalls.length;

    fireEvent.scroll(screen.getByLabelText("Chat transcript"));
    fireEvent.scroll(screen.getByLabelText("Chat transcript"));

    // hasMore is false (the first page came back under the limit), so the
    // near-top scroll must not trigger another fetch.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(host.chat.listMessagesCalls.length).toBe(callsAfterInitialLoad);
  });

  test("shows recovered Pi session boundaries inside settled history", async () => {
    const host = new FakeHost({
      authenticated: true,
      messages: [
        message({
          content: "Earlier answer",
          created_at: "2026-01-01T00:00:00Z",
          role: "assistant",
          seq: 1,
        }),
        message({
          content: "Later question",
          created_at: "2026-01-01T00:10:00Z",
          role: "user",
          seq: 2,
        }),
      ],
    });
    renderApp(host);

    expect(await screen.findByText("Later question")).toBeInTheDocument();
    expect(
      screen.getByLabelText("Historical Pi session boundary"),
    ).toBeInTheDocument();
  });

  test("shows a fresh-session hint when there is no prior activity", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);

    expect(
      await screen.findByText("Next message starts a fresh working session"),
    ).toBeInTheDocument();
  });

  test("hides the fresh-session hint once activity is inside the gap", async () => {
    const host = new FakeHost({ authenticated: true });
    host.chat.storedConversation = {
      ...conversation,
      latest_activity: new Date().toISOString(),
      session_gap_seconds: 300,
    };
    renderApp(host);

    await screen.findByRole("heading", { name: "Tether chat" });
    expect(
      screen.queryByText("Next message starts a fresh working session"),
    ).not.toBeInTheDocument();
  });

  test("shows the fresh-session hint once activity is past the gap", async () => {
    const host = new FakeHost({ authenticated: true });
    host.chat.storedConversation = {
      ...conversation,
      latest_activity: new Date(Date.now() - 10 * 60 * 1000).toISOString(),
      session_gap_seconds: 300,
    };
    renderApp(host);

    expect(
      await screen.findByText("Next message starts a fresh working session"),
    ).toBeInTheDocument();
  });

  test("hides the fresh-session hint while a turn is generating", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);

    expect(
      await screen.findByText("Next message starts a fresh working session"),
    ).toBeInTheDocument();

    fireEvent.input(textarea(await screen.findByLabelText("Message")), {
      target: { value: "Hello" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(
      screen.queryByText("Next message starts a fresh working session"),
    ).not.toBeInTheDocument();

    bus.emit({
      conversation_id: conversation.id,
      event: "agent_end",
      type: "chat",
    });

    expect(
      await screen.findByText("Next message starts a fresh working session"),
    ).toBeInTheDocument();
  });

  describe("voice conversation (#576)", () => {
    afterEach(() => {
      vi.unstubAllGlobals();
    });

    test("one button starts a hands-free voice conversation immediately", async () => {
      stubVoiceRecording();
      const host = new FakeHost({ authenticated: true });
      renderApp(host);

      await screen.findByLabelText("Message");
      const start = screen.getByRole("button", {
        name: "Start voice conversation",
      });
      expect(
        screen.queryByRole("button", { name: "Conversation mode" }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Hands-free" }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: /Record and review/ }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: /Record and send/ }),
      ).not.toBeInTheDocument();

      fireEvent.click(start);

      await screen.findByText("Listening…");
      expect(
        screen.getByRole("button", { name: "End voice conversation" }),
      ).toHaveAttribute("aria-pressed", "true");
    });

    test("ending voice conversation cancels its recording without transcription", async () => {
      stubVoiceRecording();
      const host = new FakeHost({ authenticated: true });
      renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Start voice conversation" }),
      );
      await screen.findByText("Listening…");

      fireEvent.click(
        screen.getByRole("button", { name: "End voice conversation" }),
      );

      expect(
        await screen.findByRole("button", {
          name: "Start voice conversation",
        }),
      ).toHaveAttribute("aria-pressed", "false");
      expect(screen.queryByText("Listening…")).not.toBeInTheDocument();
      expect(host.chat.transcribeAudioCalls).toEqual([]);
    });

    test("Escape ends a listening voice conversation", async () => {
      stubVoiceRecording();
      const host = new FakeHost({ authenticated: true });
      renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Start voice conversation" }),
      );
      await screen.findByText("Listening…");

      fireEvent.keyDown(window, { key: "Escape" });

      expect(
        screen.getByRole("button", { name: "Start voice conversation" }),
      ).toHaveAttribute("aria-pressed", "false");
      expect(screen.queryByText("Listening…")).not.toBeInTheDocument();
      expect(host.chat.transcribeAudioCalls).toEqual([]);
    });

    test("ending while microphone access is pending never starts recording", async () => {
      FakeMediaRecorder.instances = [];
      vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
      const fakeStream = {
        getTracks: () => [],
      } as unknown as MediaStream;
      let resolveMicrophone: ((stream: MediaStream) => void) | undefined;
      const microphone = new Promise<MediaStream>((resolve) => {
        resolveMicrophone = resolve;
      });
      Object.defineProperty(navigator, "mediaDevices", {
        configurable: true,
        value: { getUserMedia: () => microphone },
      });
      const host = new FakeHost({ authenticated: true });
      renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Start voice conversation" }),
      );
      fireEvent.click(
        screen.getByRole("button", { name: "End voice conversation" }),
      );
      resolveMicrophone?.(fakeStream);
      await Promise.resolve();
      await Promise.resolve();

      expect(FakeMediaRecorder.instances).toHaveLength(0);
      expect(host.chat.transcribeAudioCalls).toEqual([]);
    });

    test("ending during transcription never submits its eventual result", async () => {
      stubVoiceRecording();
      let resolveTranscript: ((transcript: string) => void) | undefined;
      const pendingTranscript = new Promise<string>((resolve) => {
        resolveTranscript = resolve;
      });
      const host = new FakeHost({ authenticated: true });
      vi.spyOn(host.chat, "transcribeAudio").mockReturnValue(pendingTranscript);
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Start voice conversation" }),
      );
      await screen.findByText("Listening…");
      latestFakeRecorder().stop();
      await screen.findByText("Transcribing…");

      fireEvent.click(
        screen.getByRole("button", { name: "End voice conversation" }),
      );
      resolveTranscript?.("do not submit me");
      await Promise.resolve();
      await Promise.resolve();

      expect(bus.sent).toEqual([]);
    });

    test("discarding a failed clip ends voice conversation", async () => {
      stubVoiceRecording();
      const host = new FakeHost({ authenticated: true });
      host.chat.transcribeAudioRejections = [new ApiError(502)];
      renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Start voice conversation" }),
      );
      await screen.findByText("Listening…");
      latestFakeRecorder().stop();
      await screen.findByText(
        "The service is temporarily unavailable. Please try again.",
      );

      fireEvent.click(screen.getByRole("button", { name: "Discard" }));

      expect(
        screen.getByRole("button", { name: "Start voice conversation" }),
      ).toHaveAttribute("aria-pressed", "false");
    });

    test("typed messages retain spoken replies while voice conversation is active", async () => {
      stubVoiceRecording();
      const speech = stubSpeech();
      const host = new FakeHost({ authenticated: true });
      const bus = renderApp(host);

      const messageBox = textarea(await screen.findByLabelText("Message"));
      fireEvent.click(
        screen.getByRole("button", { name: "Start voice conversation" }),
      );
      await screen.findByText("Listening…");
      fireEvent.input(messageBox, { target: { value: "Hello" } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));

      expect(screen.queryByText("Listening…")).not.toBeInTheDocument();
      expect(host.chat.transcribeAudioCalls).toEqual([]);
      expect(bus.sent[0]).toMatchObject({
        content: "Hello",
        replyMode: "spoken",
      });

      bus.emit({
        conversation_id: conversation.id,
        event: "agent_end",
        final_text: "Typed prompts still get spoken replies.",
        type: "chat",
      });
      await screen.findByText("Speaking reply…");
      await waitFor(() => expect(speech.spoken).toHaveLength(1));
      speech.finishSpeaking();
      await screen.findByText("Listening…");

      fireEvent.click(
        screen.getByRole("button", { name: "End voice conversation" }),
      );
      fireEvent.input(messageBox, { target: { value: "Again" } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));

      expect(bus.sent.at(-1)).toMatchObject({
        content: "Again",
        replyMode: "text",
      });
    });

    test("speech followed by silence submits once and re-arms after the reply", async () => {
      stubVoiceRecording();
      const speechEnd = stubSpeechEndDetection();
      const speech = stubSpeech();
      const host = new FakeHost({ authenticated: true });
      host.chat.nextTranscript = "hands free follow up";
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Start voice conversation" }),
      );
      await screen.findByText("Listening…");

      speechEnd.sample(0, 5_000);
      expect(bus.sent).toEqual([]);
      speechEnd.sample(0.05, 5_100);
      speechEnd.sample(0, 5_200);
      speechEnd.sample(0, 6_400);

      await waitFor(() => {
        expect(bus.sent).toEqual([
          {
            content: "hands free follow up",
            conversationId: conversation.id,
            replyMode: "spoken",
            type: "prompt",
          },
        ]);
      });
      speechEnd.sample(0, 8_000);
      expect(bus.sent).toHaveLength(1);
      expect(await screen.findByText("Thinking…")).toBeVisible();

      bus.emit({
        conversation_id: conversation.id,
        event: "agent_end",
        final_text: "spoken answer",
        type: "chat",
      });
      await screen.findByText("Speaking reply…");
      await Promise.resolve();
      await Promise.resolve();
      expect(speech.spoken).toHaveLength(1);
      speech.finishSpeaking();
      await screen.findByText("Listening…");
    });

    test("ending while thinking keeps the eventual reply silent", async () => {
      stubVoiceRecording();
      const speech = stubSpeech();
      const host = new FakeHost({ authenticated: true });
      host.chat.nextTranscript = "Answer later";
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Start voice conversation" }),
      );
      await screen.findByText("Listening…");
      latestFakeRecorder().stop();
      await screen.findByText("Thinking…");

      fireEvent.click(
        screen.getByRole("button", { name: "End voice conversation" }),
      );
      bus.emit({
        conversation_id: conversation.id,
        event: "agent_end",
        final_text: "Do not speak this reply.",
        type: "chat",
      });
      await Promise.resolve();
      await Promise.resolve();

      expect(speech.spoken).toEqual([]);
      expect(screen.queryByText("Speaking reply…")).not.toBeInTheDocument();
    });

    test("a spoken reply is normalized and the conversation button stops playback", async () => {
      stubVoiceRecording();
      const speech = stubSpeech();
      const host = new FakeHost({ authenticated: true });
      host.chat.nextTranscript = "Explain it";
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Start voice conversation" }),
      );
      await screen.findByText("Listening…");
      latestFakeRecorder().stop();
      await waitFor(() => expect(bus.sent).toHaveLength(1));

      bus.emit({
        conversation_id: conversation.id,
        event: "agent_end",
        final_text: "# Hi there\n\n```js\nsecret();\n```\n\nDone.",
        type: "chat",
      });

      await screen.findByText("Speaking reply…");
      await waitFor(() => expect(speech.spoken).toHaveLength(1));
      expect(speech.spoken[0].text).toBe("Hi there\n\nDone.");
      expect(
        screen.queryByRole("button", { name: "Stop playback" }),
      ).not.toBeInTheDocument();

      fireEvent.click(
        screen.getByRole("button", { name: "End voice conversation" }),
      );
      expect(screen.queryByText("Speaking reply…")).not.toBeInTheDocument();
      expect(speech.cancellations).toBeGreaterThanOrEqual(1);
    });

    test("speech provider failure preserves the visible answer", async () => {
      stubVoiceRecording();
      const host = new FakeHost({ authenticated: true });
      host.chat.nextTranscript = "Speak to me";
      host.chat.synthesizeSpeechRejections.push(new ApiError(502));
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Start voice conversation" }),
      );
      await screen.findByText("Listening…");
      latestFakeRecorder().stop();
      await waitFor(() => expect(bus.sent).toHaveLength(1));
      bus.emit({
        conversation_id: conversation.id,
        event: "message_start",
        type: "chat",
      });
      bus.emit({
        conversation_id: conversation.id,
        delta: "The visible answer survives.",
        event: "text_delta",
        type: "chat",
      });
      host.chat.storedMessages = [
        message({ content: "The visible answer survives.", seq: 2 }),
      ];
      bus.emit({
        conversation_id: conversation.id,
        event: "agent_end",
        final_text: "The visible answer survives.",
        type: "chat",
      });

      expect(await screen.findByText("Speech playback failed.")).toBeVisible();
      expect(screen.getByText("The visible answer survives.")).toBeVisible();
    });

    test("Ctrl+Shift+V starts and ends voice conversation", async () => {
      stubVoiceRecording();
      const host = new FakeHost({ authenticated: true });
      renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.keyDown(window, {
        ctrlKey: true,
        key: "V",
        shiftKey: true,
      });
      await screen.findByText("Listening…");
      expect(
        screen.getByRole("button", { name: "End voice conversation" }),
      ).toHaveAttribute("aria-pressed", "true");

      fireEvent.keyDown(window, {
        ctrlKey: true,
        key: "V",
        shiftKey: true,
      });
      expect(
        screen.getByRole("button", { name: "Start voice conversation" }),
      ).toHaveAttribute("aria-pressed", "false");
    });

    test("the composer placeholder reflects voice conversation state", async () => {
      stubVoiceRecording();
      const host = new FakeHost({ authenticated: true });
      renderApp(host);

      const messageBox = textarea(await screen.findByLabelText("Message"));
      expect(messageBox.getAttribute("placeholder")).toContain("Tether");

      fireEvent.click(
        screen.getByRole("button", { name: "Start voice conversation" }),
      );
      expect(messageBox.getAttribute("placeholder")).toBe("Reply spoken…");
    });
  });
});
