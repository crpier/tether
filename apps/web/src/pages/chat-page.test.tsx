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

function latestFakeRecorder(): FakeMediaRecorder {
  const recorder = FakeMediaRecorder.instances.at(-1);
  if (recorder === undefined) {
    throw new Error("expected a recorder to have been created");
  }
  return recorder;
}

// A scripted stand-in for the browser Web Speech API so conversation-mode
// playback (#542) is deterministic: `speak` holds the utterance until the
// test resolves it, and `cancel` drops everything queued.
class FakeSpeechSynthesis {
  cancellations = 0;
  spoken: {
    text: string;
    onend?: () => void;
    onerror?: () => void;
  }[] = [];

  speak(utterance: {
    text: string;
    onend?: () => void;
    onerror?: () => void;
  }): void {
    this.spoken.push(utterance);
  }

  cancel(): void {
    this.cancellations += 1;
    this.spoken = [];
  }

  /** Simulates playback finishing naturally (fires the live utterance's end). */
  finishSpeaking(): void {
    this.spoken.at(-1)?.onend?.();
  }
}

let activeFakeSpeech: FakeSpeechSynthesis | null = null;

function stubSpeech(): FakeSpeechSynthesis {
  const fake = new FakeSpeechSynthesis();
  activeFakeSpeech = fake;
  vi.stubGlobal("speechSynthesis", fake);
  vi.stubGlobal(
    "SpeechSynthesisUtterance",
    class {
      constructor(public text: string) {}
    },
  );
  return fake;
}

function speechFinishLast(): void {
  if (activeFakeSpeech === null) {
    throw new Error("expected speech to have been stubbed");
  }
  activeFakeSpeech.finishSpeaking();
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
    expect(screen.getByText("used capture")).toBeInTheDocument();
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
      screen.queryByText("Next message starts a fresh session"),
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
    expect(await screen.findByText("using search…")).toBeInTheDocument();

    bus.emit({
      conversation_id: conversation.id,
      event: "tool_end",
      tool_id: "t1",
      tool_name: "search",
      type: "chat",
    });
    expect(await screen.findByText("used search")).toBeInTheDocument();
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

  test("puts the unlabeled model beside session status above the composer", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);

    const context = await screen.findByRole("group", {
      name: "Composer context",
    });
    const composer = screen.getByRole("group", { name: "Message composer" });

    expect(
      within(context).getByText("Next message starts a fresh session"),
    ).toBeInTheDocument();
    expect(
      await within(context).findByRole("slider", { name: "Model profile" }),
    ).toBeInTheDocument();
    expect(
      within(composer).queryByRole("slider", { name: "Model profile" }),
    ).not.toBeInTheDocument();
    expect(
      within(composer).getByRole("textbox", { name: "Message" }),
    ).toBeInTheDocument();
    expect(
      within(composer).getByRole("button", { name: "Record and review" }),
    ).toBeInTheDocument();
    expect(
      within(composer).getByRole("button", { name: "Send" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Model")).not.toBeInTheDocument();
    expect(screen.queryByText("Message")).not.toBeInTheDocument();
  });

  test("presents model profiles without exposing model or effort details", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);

    const slider = await screen.findByRole("slider", {
      name: "Model profile",
    });

    expect(slider).toHaveAttribute("min", "0");
    expect(slider).toHaveAttribute("max", "4");
    expect(slider).toHaveAttribute("step", "1");
    expect(slider).toHaveValue("0");
    expect(slider).toHaveAttribute("aria-valuetext", "Profile 1 of 5");
    expect(screen.queryAllByText(/GPT-5\.6|thinking/)).toHaveLength(0);
  });

  test("selecting a profile persists it without moving the transcript", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    const transcript = await screen.findByLabelText("Chat transcript");
    const slider = await screen.findByRole("slider", {
      name: "Model profile",
    });
    transcript.scrollTop = 123;

    fireEvent.input(slider, { target: { value: "2" } });
    fireEvent.input(slider, { target: { value: "4" } });
    expect(host.chat.selectedModel).toBeUndefined();

    fireEvent.change(slider, { target: { value: "4" } });

    await waitFor(() => {
      expect(host.chat.selectedModel).toBe("gpt-5.6-sol");
    });
    expect(slider).toHaveAttribute("aria-valuetext", "Profile 5 of 5");
    expect(transcript.scrollTop).toBe(123);
  });

  test("serializes rapid profile commits and keeps the latest selection", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    const slider = await screen.findByRole("slider", {
      name: "Model profile",
    });

    fireEvent.input(slider, { target: { value: "4" } });
    fireEvent.change(slider, { target: { value: "4" } });
    fireEvent.input(slider, { target: { value: "0" } });
    fireEvent.change(slider, { target: { value: "0" } });

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

    const slider = await screen.findByRole("slider", {
      name: "Model profile",
    });

    expect(slider).toHaveValue("0");
    expect(slider).toHaveAttribute("aria-valuetext", "Profile 1 of 5");
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

    const slider = await screen.findByRole("slider", {
      name: "Model profile",
    });

    expect(slider).toBeDisabled();
    expect(slider).toHaveAttribute("max", "0");
    expect(slider).toHaveAttribute("aria-valuetext", "Profile 1 of 1");
  });

  test("reports an empty model catalog without rendering a slider", async () => {
    const host = new FakeHost({ authenticated: true });
    host.chat.listModels = () =>
      Promise.resolve({ default_model: null, models: [] });
    renderApp(host);

    expect(
      await screen.findByText("No model profiles available."),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("slider", { name: "Model profile" }),
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

  test("shows a fresh-session hint when there is no prior activity", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);

    expect(
      await screen.findByText("Next message starts a fresh session"),
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
      screen.queryByText("Next message starts a fresh session"),
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
      await screen.findByText("Next message starts a fresh session"),
    ).toBeInTheDocument();
  });

  test("hides the fresh-session hint while a turn is generating", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);

    expect(
      await screen.findByText("Next message starts a fresh session"),
    ).toBeInTheDocument();

    fireEvent.input(textarea(await screen.findByLabelText("Message")), {
      target: { value: "Hello" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(
      screen.queryByText("Next message starts a fresh session"),
    ).not.toBeInTheDocument();

    bus.emit({
      conversation_id: conversation.id,
      event: "agent_end",
      type: "chat",
    });

    expect(
      await screen.findByText("Next message starts a fresh session"),
    ).toBeInTheDocument();
  });

  describe("voice input (issue #19)", () => {
    afterEach(() => {
      vi.unstubAllGlobals();
    });

    test("review mode fills the composer instead of sending", async () => {
      stubVoiceRecording();
      const host = new FakeHost({ authenticated: true });
      host.chat.nextTranscript = "buy oat milk";
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: /Record and review/ }),
      );
      await screen.findByText("Recording…");

      latestFakeRecorder().stop();

      const messageBox = textarea(
        await screen.findByLabelText("Message", undefined, { timeout: 2000 }),
      );
      await waitFor(() => {
        expect(messageBox.value).toBe("buy oat milk");
      });
      // The transcript only fills the draft — it is not sent on its own.
      expect(bus.sent).toEqual([]);
    });

    test("auto-send mode sends the transcript through the normal send path", async () => {
      stubVoiceRecording();
      const host = new FakeHost({ authenticated: true });
      host.chat.nextTranscript = "call the dentist";
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(screen.getByRole("button", { name: /Record and send/ }));
      await screen.findByText("Recording…");

      latestFakeRecorder().stop();

      await waitFor(() => {
        expect(bus.sent).toEqual([
          {
            content: "call the dentist",
            conversationId: conversation.id,
            replyMode: "text",
            type: "prompt",
          },
        ]);
      });
      expect(await screen.findByText("call the dentist")).toBeInTheDocument();
    });

    test("auto-send queues a transcript while a turn is generating", async () => {
      stubVoiceRecording();
      const host = new FakeHost({ authenticated: true });
      host.chat.nextTranscript = "voice follow up";
      const bus = renderApp(host);
      const messageBox = textarea(await screen.findByLabelText("Message"));
      fireEvent.input(messageBox, { target: { value: "First" } });
      fireEvent.keyDown(messageBox, { key: "Enter" });

      fireEvent.click(screen.getByRole("button", { name: /Record and send/ }));
      await screen.findByText("Recording…");
      latestFakeRecorder().stop();

      const queue = await screen.findByRole("region", {
        name: "Queued messages",
      });
      expect(within(queue).getByText("voice follow up")).toBeInTheDocument();
      expect(bus.sent).toEqual([
        {
          content: "First",
          conversationId: conversation.id,
          replyMode: "text",
          type: "prompt",
        },
      ]);
    });

    test("a failed transcription keeps the clip with retry/discard, entering nothing into chat", async () => {
      stubVoiceRecording();
      const host = new FakeHost({ authenticated: true });
      host.chat.transcribeAudioRejections = [new ApiError(502)];
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(screen.getByRole("button", { name: /Record and send/ }));
      await screen.findByText("Recording…");

      latestFakeRecorder().stop();

      expect(
        await screen.findByText(
          "The service is temporarily unavailable. Please try again.",
        ),
      ).toBeInTheDocument();
      expect(bus.sent).toEqual([]);
      expect(host.chat.transcribeAudioCalls).toHaveLength(1);

      // Discard drops the clip and returns to the idle two-button state.
      fireEvent.click(screen.getByRole("button", { name: "Discard" }));
      expect(
        await screen.findByRole("button", { name: /Record and review/ }),
      ).toBeInTheDocument();
    });

    test("retry re-uploads the retained clip and can then succeed", async () => {
      stubVoiceRecording();
      const host = new FakeHost({ authenticated: true });
      host.chat.transcribeAudioRejections = [new ApiError(502)];
      host.chat.nextTranscript = "buy oat milk";
      renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: /Record and review/ }),
      );
      await screen.findByText("Recording…");

      latestFakeRecorder().stop();
      await screen.findByText(
        "The service is temporarily unavailable. Please try again.",
      );

      fireEvent.click(screen.getByRole("button", { name: "Retry" }));

      const messageBox = textarea(await screen.findByLabelText("Message"));
      await waitFor(() => {
        expect(messageBox.value).toBe("buy oat milk");
      });
      expect(host.chat.transcribeAudioCalls).toHaveLength(2);
    });

    test("cancel mid-recording never uploads anything", async () => {
      stubVoiceRecording();
      const host = new FakeHost({ authenticated: true });
      renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: /Record and review/ }),
      );
      await screen.findByText("Recording…");

      fireEvent.click(screen.getByRole("button", { name: "Cancel recording" }));

      expect(
        await screen.findByRole("button", { name: /Record and review/ }),
      ).toBeInTheDocument();
      expect(host.chat.transcribeAudioCalls).toEqual([]);
    });
  });

  describe("conversation mode (#542)", () => {
    test("the toggle defaults to text and captures spoken mode per prompt", async () => {
      const host = new FakeHost({ authenticated: true });
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      const toggle = screen.getByRole("button", { name: "Conversation mode" });
      expect(toggle.getAttribute("aria-pressed")).toBe("false");

      fireEvent.click(toggle);
      expect(toggle.getAttribute("aria-pressed")).toBe("true");

      const messageBox = textarea(screen.getByLabelText("Message"));
      fireEvent.input(messageBox, { target: { value: "Hello" } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));

      expect(bus.sent[0]).toMatchObject({ replyMode: "spoken" });

      const toggleAfterSend = screen.getByRole("button", {
        name: "Conversation mode",
      });
      fireEvent.click(toggleAfterSend);
      expect(toggleAfterSend.getAttribute("aria-pressed")).toBe("false");
      fireEvent.input(messageBox, { target: { value: "Again" } });
      fireEvent.click(screen.getByRole("button", { name: "Queue message" }));

      // The queued prompt dispatches only once the running turn settles; its
      // captured mode must be text despite the earlier spoken dispatch.
      bus.emit({
        conversation_id: conversation.id,
        event: "agent_end",
        type: "chat",
      });
      await screen.findByText("Again");

      expect(bus.sent.at(-1)).toMatchObject({ replyMode: "text" });
    });

    test("a settled spoken reply plays normalized text once and can be stopped", async () => {
      const speech = stubSpeech();
      const host = new FakeHost({ authenticated: true });
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Conversation mode" }),
      );
      const messageBox = textarea(screen.getByLabelText("Message"));
      fireEvent.input(messageBox, { target: { value: "Explain it" } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));

      bus.emit({
        conversation_id: conversation.id,
        event: "agent_end",
        final_text: "# Hi there\n\n```js\nsecret();\n```\n\nDone.",
        type: "chat",
      });

      await screen.findByText("Speaking reply…");
      expect(speech.spoken).toHaveLength(1);
      expect(speech.spoken[0].text).toBe("Hi there\n\nDone.");

      fireEvent.click(screen.getByRole("button", { name: "Stop playback" }));
      expect(screen.queryByText("Speaking reply…")).not.toBeInTheDocument();
      expect(speech.cancellations).toBeGreaterThanOrEqual(1);
    });

    test("text replies never play even with the toggle enabled", async () => {
      const speech = stubSpeech();
      const host = new FakeHost({ authenticated: true });
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Conversation mode" }),
      );
      // The running turn was dispatched as text before the toggle flipped.
      bus.emit({
        conversation_id: conversation.id,
        event: "agent_end",
        final_text: "text answer",
        type: "chat",
      });
      await screen.findByLabelText("Message");

      expect(speech.spoken).toHaveLength(0);
      expect(screen.queryByText("Speaking reply…")).not.toBeInTheDocument();
    });

    test("starting a voice recording cancels active playback", async () => {
      stubVoiceRecording();
      const speech = stubSpeech();
      const host = new FakeHost({ authenticated: true });
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Conversation mode" }),
      );
      const messageBox = textarea(screen.getByLabelText("Message"));
      fireEvent.input(messageBox, { target: { value: "Speak to me" } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
      bus.emit({
        conversation_id: conversation.id,
        event: "agent_end",
        final_text: "spoken answer",
        type: "chat",
      });
      await screen.findByText("Speaking reply…");

      fireEvent.click(screen.getByRole("button", { name: /Record and send/ }));

      expect(screen.queryByText("Speaking reply…")).not.toBeInTheDocument();
      expect(speech.cancellations).toBeGreaterThanOrEqual(1);
    });

    test("disabling the toggle does not cancel current playback", async () => {
      stubSpeech();
      const host = new FakeHost({ authenticated: true });
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Conversation mode" }),
      );
      const messageBox = textarea(screen.getByLabelText("Message"));
      fireEvent.input(messageBox, { target: { value: "Speak to me" } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
      bus.emit({
        conversation_id: conversation.id,
        event: "agent_end",
        final_text: "spoken answer",
        type: "chat",
      });
      await screen.findByText("Speaking reply…");

      fireEvent.click(
        screen.getByRole("button", { name: "Conversation mode" }),
      );
      expect(screen.getByText("Speaking reply…")).toBeInTheDocument();
    });
  });

  describe("hands-free loop (#544)", () => {
    test("an opt-in toggle appears with conversation mode and defaults off", async () => {
      const host = new FakeHost({ authenticated: true });
      renderApp(host);

      await screen.findByLabelText("Message");
      expect(
        screen.queryByRole("button", { name: "Hands-free" }),
      ).not.toBeInTheDocument();

      fireEvent.click(
        screen.getByRole("button", { name: "Conversation mode" }),
      );
      const handsFree = screen.getByRole("button", { name: "Hands-free" });
      expect(handsFree.getAttribute("aria-pressed")).toBe("false");
    });

    test("a naturally finished spoken reply re-arms recording", async () => {
      stubVoiceRecording();
      stubSpeech();
      const host = new FakeHost({ authenticated: true });
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Conversation mode" }),
      );
      fireEvent.click(screen.getByRole("button", { name: "Hands-free" }));
      const messageBox = textarea(screen.getByLabelText("Message"));
      fireEvent.input(messageBox, { target: { value: "Speak to me" } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
      bus.emit({
        conversation_id: conversation.id,
        event: "agent_end",
        final_text: "spoken answer",
        type: "chat",
      });
      await screen.findByText("Speaking reply…");

      speechFinishLast();
      await screen.findByText("Recording…");
    });

    test("hands-free off never re-arms recording", async () => {
      stubVoiceRecording();
      stubSpeech();
      const host = new FakeHost({ authenticated: true });
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Conversation mode" }),
      );
      const messageBox = textarea(screen.getByLabelText("Message"));
      fireEvent.input(messageBox, { target: { value: "Speak to me" } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
      bus.emit({
        conversation_id: conversation.id,
        event: "agent_end",
        final_text: "spoken answer",
        type: "chat",
      });
      await screen.findByText("Speaking reply…");

      speechFinishLast();
      await waitFor(() => {
        expect(screen.queryByText("Speaking reply…")).not.toBeInTheDocument();
      });
      expect(
        screen.getByRole("button", { name: /Record and send/ }),
      ).toBeInTheDocument();
    });

    test("stopping playback before it finishes does not re-arm recording", async () => {
      stubVoiceRecording();
      stubSpeech();
      const host = new FakeHost({ authenticated: true });
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Conversation mode" }),
      );
      fireEvent.click(screen.getByRole("button", { name: "Hands-free" }));
      const messageBox = textarea(screen.getByLabelText("Message"));
      fireEvent.input(messageBox, { target: { value: "Speak to me" } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
      bus.emit({
        conversation_id: conversation.id,
        event: "agent_end",
        final_text: "spoken answer",
        type: "chat",
      });
      await screen.findByText("Speaking reply…");

      fireEvent.click(screen.getByRole("button", { name: "Stop playback" }));

      await waitFor(() => {
        expect(screen.queryByText("Speaking reply…")).not.toBeInTheDocument();
      });
      expect(
        screen.getByRole("button", { name: /Record and send/ }),
      ).toBeInTheDocument();
    });

    test("interacting during playback breaks the loop for that cycle", async () => {
      stubVoiceRecording();
      stubSpeech();
      const host = new FakeHost({ authenticated: true });
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Conversation mode" }),
      );
      fireEvent.click(screen.getByRole("button", { name: "Hands-free" }));
      const messageBox = textarea(screen.getByLabelText("Message"));
      fireEvent.input(messageBox, { target: { value: "Speak to me" } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
      bus.emit({
        conversation_id: conversation.id,
        event: "agent_end",
        final_text: "spoken answer",
        type: "chat",
      });
      await screen.findByText("Speaking reply…");

      // Any user activity while the reply plays (typing here) means the user
      // took over; the loop must not grab the microphone out from under them.
      fireEvent.keyDown(window, { key: "a" });
      speechFinishLast();

      await waitFor(() => {
        expect(screen.queryByText("Speaking reply…")).not.toBeInTheDocument();
      });
      expect(
        screen.getByRole("button", { name: /Record and send/ }),
      ).toBeInTheDocument();
    });
  });

  describe("conversation quick wins (#546)", () => {
    test("sending a prompt barge-ins over active playback", async () => {
      stubSpeech();
      const host = new FakeHost({ authenticated: true });
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Conversation mode" }),
      );
      const messageBox = textarea(screen.getByLabelText("Message"));
      fireEvent.input(messageBox, { target: { value: "Speak to me" } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
      bus.emit({
        conversation_id: conversation.id,
        event: "agent_end",
        final_text: "a fairly long spoken answer",
        type: "chat",
      });
      await screen.findByText("Speaking reply…");

      // User takes over mid-playback: the follow-up send stops speech.
      fireEvent.input(messageBox, { target: { value: "Stop it" } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));

      expect(screen.queryByText("Speaking reply…")).not.toBeInTheDocument();
    });

    test("Escape stops playback", async () => {
      stubSpeech();
      const host = new FakeHost({ authenticated: true });
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Conversation mode" }),
      );
      const messageBox = textarea(screen.getByLabelText("Message"));
      fireEvent.input(messageBox, { target: { value: "Speak to me" } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
      bus.emit({
        conversation_id: conversation.id,
        event: "agent_end",
        final_text: "spoken answer",
        type: "chat",
      });
      await screen.findByText("Speaking reply…");

      fireEvent.keyDown(window, { key: "Escape" });

      expect(screen.queryByText("Speaking reply…")).not.toBeInTheDocument();
    });

    test("Ctrl+Shift+V toggles conversation mode from the keyboard", async () => {
      const host = new FakeHost({ authenticated: true });
      renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.keyDown(window, {
        ctrlKey: true,
        key: "V",
        shiftKey: true,
      });
      expect(
        screen
          .getByRole("button", { name: "Conversation mode" })
          .getAttribute("aria-pressed"),
      ).toBe("true");

      fireEvent.keyDown(window, {
        ctrlKey: true,
        key: "V",
        shiftKey: true,
      });
      expect(
        screen
          .getByRole("button", { name: "Conversation mode" })
          .getAttribute("aria-pressed"),
      ).toBe("false");
    });

    test("the composer placeholder reflects the current mode", async () => {
      const host = new FakeHost({ authenticated: true });
      renderApp(host);

      const messageBox = textarea(await screen.findByLabelText("Message"));
      expect(messageBox.getAttribute("placeholder")).toContain("Tether");

      fireEvent.click(
        screen.getByRole("button", { name: "Conversation mode" }),
      );
      expect(messageBox.getAttribute("placeholder")).toBe("Reply spoken…");
    });

    test("enabling conversation mode without speech support shows a hint", async () => {
      vi.stubGlobal("speechSynthesis", undefined);
      const host = new FakeHost({ authenticated: true });
      renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Conversation mode" }),
      );

      expect(
        screen.getByText(/Speech output isn't available/u),
      ).toBeInTheDocument();
    });

    test("settled spoken replies get a spoken chip on their transcript row", async () => {
      stubSpeech();
      const host = new FakeHost({ authenticated: true });
      const bus = renderApp(host);

      await screen.findByLabelText("Message");
      fireEvent.click(
        screen.getByRole("button", { name: "Conversation mode" }),
      );
      const messageBox = textarea(screen.getByLabelText("Message"));
      fireEvent.input(messageBox, { target: { value: "Speak to me" } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
      host.chat.storedMessages = [
        {
          content: "Hello there. Done.",
          conversation_id: conversation.id,
          created_at: new Date().toISOString(),
          id: "01930000-0000-7000-8000-000000000010",
          pi_message_id: null,
          role: "assistant",
          seq: 2,
          tool_args: null,
          tool_name: null,
          tool_result: null,
        },
      ];
      bus.emit({
        conversation_id: conversation.id,
        event: "text_delta",
        delta: "Hello there. Done.",
        type: "chat",
      });
      bus.emit({
        conversation_id: conversation.id,
        event: "agent_end",
        final_text: "Hello there. Done.",
        type: "chat",
      });
      await screen.findByText("Speaking reply…");

      expect(screen.getAllByLabelText("Spoken reply")).toHaveLength(1);
    });
  });
});
