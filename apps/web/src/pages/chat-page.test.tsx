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

    expect(await screen.findByText("63k context")).toHaveAttribute(
      "title",
      "63k of 200k tokens · 32% of pi working context",
    );
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

    expect(await screen.findByText("182k context")).toHaveClass(
      "text-destructive",
    );
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

    // Settled tool rows must stay expandable (same disclosure as a live tool
    // call), with the persisted arguments/result available behind it — this
    // is the regression this test guards against: history used to collapse
    // to a bare "used capture" line with no way to inspect the call.
    fireEvent.click(screen.getByText("arguments"));
    expect(screen.getByText(/"content": "aisle seats"/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("result"));
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

  test("quotes a transcript message into the composer", async () => {
    const host = new FakeHost({
      authenticated: true,
      messages: [
        message({ content: "First line\nSecond line", role: "assistant" }),
      ],
    });
    renderApp(host);

    fireEvent.click(
      await screen.findByRole("button", { name: "Quote message" }),
    );

    expect(
      textarea(screen.getByRole("textbox", { name: "Message" })),
    ).toHaveValue("> First line\n> Second line\n\n");
  });

  test("records explicit product feedback from a settled user message", async () => {
    const source = message({
      content: "The model selector is confusing.",
      role: "user",
      seq: 1,
    });
    const host = new FakeHost({ authenticated: true, messages: [source] });
    renderApp(host);

    fireEvent.click(
      await screen.findByRole("button", { name: "Record product feedback" }),
    );
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
    expect(screen.getByText("Read email")).toBeVisible();
    expect(screen.getAllByLabelText("Tool activity")).toHaveLength(1);
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

    fireEvent.click(
      await screen.findByRole("button", { name: "Undo archive" }),
    );

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
    expect(await screen.findByText(/"needle"/)).toBeInTheDocument();

    bus.emit({
      conversation_id: conversation.id,
      event: "tool_end",
      tool_id: "t1",
      tool_name: "search",
      tool_result: { kind: "collection" },
      type: "chat",
    });
    expect(await screen.findByText(/"collection"/)).toBeInTheDocument();
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
    expect(bus.sent).toEqual([
      {
        content: "First",
        conversationId: conversation.id,
        replyMode: "text",
        type: "prompt",
      },
    ]);

    bus.emit({
      conversation_id: conversation.id,
      event: "agent_end",
      type: "chat",
    });
    expect(bus.sent.at(-1)).toEqual({
      content: "Second",
      conversationId: conversation.id,
      replyMode: "text",
      type: "prompt",
    });
    expect(screen.queryByText("Second")).toBeInTheDocument();

    bus.emit({
      conversation_id: conversation.id,
      event: "agent_end",
      type: "chat",
    });
    expect(bus.sent.at(-1)).toEqual({
      content: "Third",
      conversationId: conversation.id,
      replyMode: "text",
      type: "prompt",
    });
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

    fireEvent.click(
      within(
        screen.getByRole("article", { name: "Queued message 1" }),
      ).getByRole("button", { name: "Cancel message" }),
    );
    expect(
      screen.queryByRole("region", { name: "Queued messages" }),
    ).not.toBeInTheDocument();
    expect(bus.sent).toHaveLength(1);
  });

  test("Send now stops the active turn before sending the chosen message", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);
    const messageBox = textarea(await screen.findByLabelText("Message"));

    for (const content of ["First", "Second", "Urgent correction"]) {
      fireEvent.input(messageBox, { target: { value: content } });
      fireEvent.keyDown(messageBox, { key: "Enter" });
    }
    const urgent = screen.getByRole("article", { name: "Queued message 2" });
    fireEvent.click(within(urgent).getByRole("button", { name: "Send now" }));

    expect(bus.sent.at(-1)).toEqual({
      conversationId: conversation.id,
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
    expect(bus.sent.at(-1)).toEqual({
      content: "Urgent correction",
      conversationId: conversation.id,
      replyMode: "text",
      type: "prompt",
    });
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
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));

    expect(bus.sent).toEqual([
      {
        content: "Keep going",
        conversationId: conversation.id,
        replyMode: "text",
        type: "prompt",
      },
      { conversationId: conversation.id, type: "abort" },
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

  test("puts a labeled model menu beside working-session status", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);

    const context = await screen.findByRole("group", {
      name: "Composer context",
    });
    const composer = screen.getByRole("group", { name: "Message composer" });

    expect(
      within(context).getByText("Next message starts a fresh working session"),
    ).toBeInTheDocument();
    expect(within(context).getByText("Model")).toBeInTheDocument();
    expect(
      await within(context).findByRole("combobox", { name: "Model profile" }),
    ).toHaveDisplayValue("Profile 1");
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

  test("presents model profiles without exposing model or effort details", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);

    const menu = await screen.findByRole("combobox", {
      name: "Model profile",
    });

    expect(menu).toHaveValue("gpt-5.6-luna");
    expect(menu).toHaveDisplayValue("Profile 1");
    expect(within(menu).getAllByRole("option")).toHaveLength(5);
    expect(screen.queryAllByText(/GPT-5\.6|thinking/)).toHaveLength(0);
  });

  test("selecting a profile persists it without moving the transcript", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    const transcript = await screen.findByLabelText("Chat transcript");
    const menu = await screen.findByRole("combobox", {
      name: "Model profile",
    });
    transcript.scrollTop = 123;

    fireEvent.change(menu, { target: { value: "gpt-5.6-sol" } });

    await waitFor(() => {
      expect(host.chat.selectedModel).toBe("gpt-5.6-sol");
    });
    expect(menu).toHaveDisplayValue("Profile 5");
    expect(transcript.scrollTop).toBe(123);
  });

  test("serializes rapid profile commits and keeps the latest selection", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    const menu = await screen.findByRole("combobox", {
      name: "Model profile",
    });

    fireEvent.change(menu, { target: { value: "gpt-5.6-sol" } });
    fireEvent.change(menu, { target: { value: "gpt-5.6-luna" } });

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

    const menu = await screen.findByRole("combobox", {
      name: "Model profile",
    });

    expect(menu).toHaveValue("gpt-5.6-luna");
    expect(menu).toHaveDisplayValue("Profile 1");
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

    const menu = await screen.findByRole("combobox", {
      name: "Model profile",
    });

    expect(menu).toBeDisabled();
    expect(menu).toHaveValue("local");
    expect(menu).toHaveDisplayValue("Profile 1");
  });

  test("reports an empty model catalog without rendering a menu", async () => {
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

  test("searches the loaded transcript without hiding unmatched messages", async () => {
    const host = new FakeHost({
      authenticated: true,
      messages: [
        message({ content: "ordinary text", role: "assistant", seq: 1 }),
        message({ content: "the hidden needle", role: "assistant", seq: 2 }),
      ],
    });
    renderApp(host);

    fireEvent.click(
      await screen.findByRole("button", { name: "Search transcript" }),
    );
    fireEvent.input(
      screen.getByRole("searchbox", { name: "Search transcript" }),
      {
        target: { value: "needle" },
      },
    );

    expect(await screen.findByText("1 match")).toBeInTheDocument();
    expect(screen.getByText("ordinary text")).toBeInTheDocument();
    expect(
      screen.getByText("the hidden needle").closest("[data-search-match]"),
    ).toHaveAttribute("data-search-match", "active");
  });

  test("loads older transcript pages when search opens", async () => {
    const host = new FakeHost({
      authenticated: true,
      messages: Array.from({ length: 31 }, (_, index) =>
        message({
          content: index === 0 ? "old needle" : `message ${index.toString()}`,
          role: "assistant",
          seq: index + 1,
        }),
      ),
    });
    renderApp(host);
    await screen.findByText("message 30");
    expect(screen.queryByText("old needle")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Search transcript" }));
    fireEvent.input(
      screen.getByRole("searchbox", { name: "Search transcript" }),
      {
        target: { value: "needle" },
      },
    );

    expect(await screen.findByText("old needle")).toBeInTheDocument();
    expect(screen.getByText("1 match")).toBeInTheDocument();
    expect(host.chat.listMessagesCalls).toHaveLength(2);
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
