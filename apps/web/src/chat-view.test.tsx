import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import {
  FakeApi,
  conversation,
  message,
  renderApp,
  textarea,
} from "./testing/harness";

afterEach(cleanup);

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
});
