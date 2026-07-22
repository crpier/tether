import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test, vi } from "vitest";

import { ApiError } from "./api";
import {
  FakeApi,
  conversation,
  message,
  renderApp,
  textarea,
} from "./testing/harness";

afterEach(cleanup);

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

describe("Chat view", () => {
  test("rehydrates settled chat history", async () => {
    const api = new FakeApi({
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
    renderApp(api);

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

  test("renders streamed answers as markdown", async () => {
    const api = new FakeApi({ authenticated: true });
    const bus = renderApp(api);

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

  test("shows inline tool activity transitioning to done", async () => {
    const api = new FakeApi({ authenticated: true });
    const bus = renderApp(api);

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
    const api = new FakeApi({ authenticated: true });
    const bus = renderApp(api);

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
    const api = new FakeApi({ authenticated: true });
    const bus = renderApp(api);

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
    const api = new FakeApi({ authenticated: true });
    const bus = renderApp(api);

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

    const reasoning = await screen.findByLabelText("Tether reasoning");
    expect(within(reasoning).getByText("pondering")).toBeInTheDocument();
    expect(screen.getByText("the answer")).toBeInTheDocument();
  });

  test("error frames show a dismissible banner", async () => {
    const api = new FakeApi({ authenticated: true });
    const bus = renderApp(api);

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
    const api = new FakeApi({ authenticated: true });
    const bus = renderApp(api);

    const messageBox = textarea(await screen.findByLabelText("Message"));
    fireEvent.input(messageBox, { target: { value: "Hello" } });
    fireEvent.keyDown(messageBox, { key: "Enter" });

    expect(bus.sent).toEqual([
      { content: "Hello", conversationId: conversation.id, type: "prompt" },
    ]);
    expect(screen.getByText("Hello")).toBeInTheDocument();
  });

  test("Shift+Enter inserts a newline instead of sending", async () => {
    const api = new FakeApi({ authenticated: true });
    const bus = renderApp(api);

    const messageBox = textarea(await screen.findByLabelText("Message"));
    fireEvent.input(messageBox, { target: { value: "line one" } });
    fireEvent.keyDown(messageBox, { key: "Enter", shiftKey: true });

    expect(bus.sent).toEqual([]);
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

  test("Send is disabled until the input has non-whitespace text", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    const send = await screen.findByRole("button", { name: "Send" });
    expect(send).toBeDisabled();

    const messageBox = textarea(screen.getByLabelText("Message"));
    fireEvent.input(messageBox, { target: { value: "   " } });
    expect(send).toBeDisabled();

    fireEvent.input(messageBox, { target: { value: "hello" } });
    expect(send).toBeEnabled();
  });

  test("a stopped generation keeps an interrupted marker on the transcript", async () => {
    const api = new FakeApi({ authenticated: true });
    const bus = renderApp(api);

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
      expect(api.messageCalls).toBeGreaterThan(1);
    });
    expect(screen.getByText("Generation stopped.")).toBeInTheDocument();
  });

  test("New chat clears the transcript via the API", async () => {
    const api = new FakeApi({
      authenticated: true,
      messages: [message({ content: "old topic", role: "user", seq: 1 })],
    });
    renderApp(api);

    expect(await screen.findByText("old topic")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "New chat" }));

    await waitFor(() => {
      expect(api.clearConversationCalls).toBe(1);
    });
    await waitFor(() => {
      expect(screen.queryByText("old topic")).not.toBeInTheDocument();
    });
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

  test("only fetches the latest page of history by default", async () => {
    const api = new FakeApi({
      authenticated: true,
      messages: [message({ content: "hi", role: "user", seq: 1 })],
    });
    renderApp(api);

    await waitFor(() => {
      expect(api.listMessagesCalls.length).toBeGreaterThan(0);
    });
    expect(api.listMessagesCalls[0]).toEqual({
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
    const api = new FakeApi({ authenticated: true, messages });
    renderApp(api);

    // The default page is the newest 30 rows (seq 3..32); the oldest two are
    // not yet loaded.
    expect(await screen.findByText("msg-32")).toBeInTheDocument();
    expect(screen.queryByText("msg-1")).not.toBeInTheDocument();

    fireEvent.scroll(screen.getByLabelText("Chat transcript"));

    expect(await screen.findByText("msg-1")).toBeInTheDocument();
    expect(screen.getByText("msg-2")).toBeInTheDocument();
    await waitFor(() => {
      expect(api.listMessagesCalls).toEqual([
        { limit: 30, beforeSeq: undefined },
        { limit: 30, beforeSeq: 3 },
      ]);
    });
  });

  test("stops fetching once the oldest page is smaller than the limit", async () => {
    const messages = [message({ content: "only one", role: "user", seq: 1 })];
    const api = new FakeApi({ authenticated: true, messages });
    renderApp(api);

    expect(await screen.findByText("only one")).toBeInTheDocument();
    const callsAfterInitialLoad = api.listMessagesCalls.length;

    fireEvent.scroll(screen.getByLabelText("Chat transcript"));
    fireEvent.scroll(screen.getByLabelText("Chat transcript"));

    // hasMore is false (the first page came back under the limit), so the
    // near-top scroll must not trigger another fetch.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(api.listMessagesCalls.length).toBe(callsAfterInitialLoad);
  });

  test("shows a fresh-session hint when there is no prior activity", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    expect(
      await screen.findByText("Next message starts a fresh session"),
    ).toBeInTheDocument();
  });

  test("hides the fresh-session hint once activity is inside the gap", async () => {
    const api = new FakeApi({ authenticated: true });
    api.storedConversation = {
      ...conversation,
      latest_activity: new Date().toISOString(),
      session_gap_seconds: 300,
    };
    renderApp(api);

    await screen.findByRole("heading", { name: "Tether chat" });
    expect(
      screen.queryByText("Next message starts a fresh session"),
    ).not.toBeInTheDocument();
  });

  test("shows the fresh-session hint once activity is past the gap", async () => {
    const api = new FakeApi({ authenticated: true });
    api.storedConversation = {
      ...conversation,
      latest_activity: new Date(Date.now() - 10 * 60 * 1000).toISOString(),
      session_gap_seconds: 300,
    };
    renderApp(api);

    expect(
      await screen.findByText("Next message starts a fresh session"),
    ).toBeInTheDocument();
  });

  test("hides the fresh-session hint while a turn is generating", async () => {
    const api = new FakeApi({ authenticated: true });
    const bus = renderApp(api);

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

  test("New chat resets accumulated pagination state", async () => {
    const messages = Array.from({ length: 32 }, (_, index) =>
      message({
        content: `msg-${(index + 1).toString()}`,
        role: "user",
        seq: index + 1,
      }),
    );
    const api = new FakeApi({ authenticated: true, messages });
    renderApp(api);

    expect(await screen.findByText("msg-32")).toBeInTheDocument();
    fireEvent.scroll(screen.getByLabelText("Chat transcript"));
    expect(await screen.findByText("msg-1")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    await waitFor(() => {
      expect(api.clearConversationCalls).toBe(1);
    });
    await waitFor(() => {
      expect(screen.queryByText("msg-1")).not.toBeInTheDocument();
    });

    // Scrolling after the reset must not reissue a fetch for the now-stale
    // pre-clear cursor (seq 3): the accumulated map and hasMore were reset.
    const callsAfterClear = api.listMessagesCalls.length;
    fireEvent.scroll(screen.getByLabelText("Chat transcript"));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(
      api.listMessagesCalls
        .slice(callsAfterClear)
        .every((call) => call?.beforeSeq === undefined),
    ).toBe(true);
  });

  describe("voice input (issue #19)", () => {
    afterEach(() => {
      vi.unstubAllGlobals();
    });

    test("review mode fills the composer instead of sending", async () => {
      stubVoiceRecording();
      const api = new FakeApi({ authenticated: true });
      api.nextTranscript = "buy oat milk";
      const bus = renderApp(api);

      await screen.findByLabelText("Message");
      fireEvent.click(screen.getByRole("button", { name: /Say & review/ }));
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
      const api = new FakeApi({ authenticated: true });
      api.nextTranscript = "call the dentist";
      const bus = renderApp(api);

      await screen.findByLabelText("Message");
      fireEvent.click(screen.getByRole("button", { name: /Say & send/ }));
      await screen.findByText("Recording…");

      latestFakeRecorder().stop();

      await waitFor(() => {
        expect(bus.sent).toEqual([
          {
            content: "call the dentist",
            conversationId: conversation.id,
            type: "prompt",
          },
        ]);
      });
      expect(await screen.findByText("call the dentist")).toBeInTheDocument();
    });

    test("a failed transcription keeps the clip with retry/discard, entering nothing into chat", async () => {
      stubVoiceRecording();
      const api = new FakeApi({ authenticated: true });
      api.transcribeAudioRejections = [new ApiError(502)];
      const bus = renderApp(api);

      await screen.findByLabelText("Message");
      fireEvent.click(screen.getByRole("button", { name: /Say & send/ }));
      await screen.findByText("Recording…");

      latestFakeRecorder().stop();

      expect(
        await screen.findByText(
          "The service is temporarily unavailable. Please try again.",
        ),
      ).toBeInTheDocument();
      expect(bus.sent).toEqual([]);
      expect(api.transcribeAudioCalls).toHaveLength(1);

      // Discard drops the clip and returns to the idle two-button state.
      fireEvent.click(screen.getByRole("button", { name: "Discard" }));
      expect(
        await screen.findByRole("button", { name: /Say & review/ }),
      ).toBeInTheDocument();
    });

    test("retry re-uploads the retained clip and can then succeed", async () => {
      stubVoiceRecording();
      const api = new FakeApi({ authenticated: true });
      api.transcribeAudioRejections = [new ApiError(502)];
      api.nextTranscript = "buy oat milk";
      renderApp(api);

      await screen.findByLabelText("Message");
      fireEvent.click(screen.getByRole("button", { name: /Say & review/ }));
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
      expect(api.transcribeAudioCalls).toHaveLength(2);
    });

    test("cancel mid-recording never uploads anything", async () => {
      stubVoiceRecording();
      const api = new FakeApi({ authenticated: true });
      renderApp(api);

      await screen.findByLabelText("Message");
      fireEvent.click(screen.getByRole("button", { name: /Say & review/ }));
      await screen.findByText("Recording…");

      fireEvent.click(screen.getByRole("button", { name: "Cancel recording" }));

      expect(
        await screen.findByRole("button", { name: /Say & review/ }),
      ).toBeInTheDocument();
      expect(api.transcribeAudioCalls).toEqual([]);
    });
  });
});
