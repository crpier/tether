import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test, vi } from "vitest";

import {
  ConversationArchiveBlockedError,
  type Conversation,
} from "../host/chat";
import { ApiError } from "../host/error";
import {
  FakeHost,
  conversation,
  input,
  message,
  renderApp,
  textarea,
} from "../testing/harness";

function scoped(
  id: string,
  displayName: string,
  overrides: Partial<Conversation> = {},
): Conversation {
  return {
    ...conversation,
    display_name: displayName,
    id,
    kind: "scoped",
    scope_brief: `${displayName} brief`,
    title: displayName,
    ...overrides,
  };
}

function useMobileLayout(): void {
  vi.stubGlobal("matchMedia", (query: string) => ({
    addEventListener: vi.fn(),
    matches: !query.includes("min-width: 1024px"),
    media: query,
    removeEventListener: vi.fn(),
  }));
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Conversation navigation", () => {
  test("redirects the root to canonical Chat while preserving its query", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host, undefined, { path: "/?prompt=keep%20this" });

    await waitFor(() => {
      expect(window.location.pathname).toBe("/chat");
      expect(window.location.search).toBe("?prompt=keep%20this");
    });
  });

  test("uses explicit Main and Scoped routes and redirects the Main UUID", async () => {
    const garden = scoped("018f0000-0000-7000-8000-000000000101", "Garden");
    const host = new FakeHost({ authenticated: true });
    host.chat.storedConversations = [conversation, garden];

    renderApp(host, undefined, { path: `/chat/${conversation.id}` });

    await waitFor(() => {
      expect(window.location.pathname).toBe("/chat");
    });
    expect(
      await screen.findByRole("heading", { name: "Main Chat" }),
    ).toBeVisible();
  });

  test("shows a retryable load error for non-404 Conversation failures", async () => {
    const host = new FakeHost({ authenticated: true });
    host.chat.fetchConversationRejections = [new Error("host unavailable")];
    renderApp(host, undefined, {
      path: "/chat/018f0000-0000-7000-8000-00000000ffff",
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Conversation could not be loaded",
    );
    expect(
      screen.queryByRole("heading", { name: "Conversation not found" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeVisible();
  });

  test("shows a Conversation Not Found view with a Main link", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host, undefined, {
      path: "/chat/018f0000-0000-7000-8000-00000000ffff",
    });

    expect(
      await screen.findByRole("heading", { name: "Conversation not found" }),
    ).toBeVisible();
    const notFound = screen.getByRole("heading", {
      name: "Conversation not found",
    }).parentElement;
    expect(notFound).not.toBeNull();
    expect(
      within(notFound!).getByRole("link", { name: "Main Chat" }),
    ).toHaveAttribute("href", "/chat");
  });

  test("a new-chat link immediately creates an untitled chat and opens it", async () => {
    const host = new FakeHost({ authenticated: true });
    host.chat.storedConversations = [conversation];
    renderApp(host, undefined, { path: "/chat?new=1" });

    await waitFor(() => {
      expect(host.chat.createConversationCalls).toEqual([{}]);
    });
    await waitFor(() => {
      expect(window.location.pathname).toMatch(/^\/chat\/[0-9a-f-]{36}$/);
    });
    expect(
      await screen.findByRole("heading", { name: "Untitled chat" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "Main Chat" }),
    ).not.toBeInTheDocument();
  });

  test("renders archived Conversations read-only and restores them", async () => {
    const archived = scoped(
      "018f0000-0000-7000-8000-000000000102",
      "Old project",
      { archived_at: "2026-01-03T00:00:00Z", status: "archived" },
    );
    const host = new FakeHost({
      authenticated: true,
      messages: [
        message({
          content: "Retained history",
          conversation_id: archived.id,
          seq: 1,
        }),
      ],
    });
    host.chat.storedConversations = [conversation, archived];
    renderApp(host, undefined, { path: `/chat/${archived.id}` });

    expect(await screen.findByText("Retained history")).toBeVisible();
    expect(screen.queryByLabelText("Message")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Search transcript" }),
    ).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Restore conversation" }),
    );

    await waitFor(() => {
      expect(host.chat.restoreConversationCalls).toEqual([archived.id]);
    });
    expect(await screen.findByLabelText("Message")).toBeVisible();
  });

  test("header edit state resets on Conversation identity and sends only changed fields", async () => {
    const garden = scoped("018f0000-0000-7000-8000-000000000103", "Garden");
    const house = scoped("018f0000-0000-7000-8000-000000000104", "House");
    const host = new FakeHost({ authenticated: true });
    host.chat.storedConversations = [conversation, garden, house];
    renderApp(host, undefined, { path: `/chat/${garden.id}` });

    fireEvent.click(
      await screen.findByRole("button", { name: "Edit conversation" }),
    );
    fireEvent.input(input(screen.getByLabelText("Conversation name")), {
      target: { value: "Back garden" },
    });
    fireEvent.click(
      within(screen.getByRole("region", { name: "Conversations" })).getByRole(
        "link",
        { name: "House" },
      ),
    );

    expect(await screen.findByRole("heading", { name: "House" })).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Save conversation" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit conversation" }));
    fireEvent.input(input(screen.getByLabelText("Conversation name")), {
      target: { value: "Home" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save conversation" }));

    await waitFor(() => {
      expect(host.chat.updateConversationCalls.at(-1)).toEqual({
        body: { display_name: "Home" },
        conversationId: house.id,
      });
    });
  });

  test("edits scope and archives an active Scoped Conversation back to Main", async () => {
    const garden = scoped("018f0000-0000-7000-8000-000000000103", "Garden");
    const host = new FakeHost({ authenticated: true });
    host.chat.storedConversations = [conversation, garden];
    renderApp(host, undefined, { path: `/chat/${garden.id}` });

    fireEvent.click(
      await screen.findByRole("button", { name: "Edit conversation" }),
    );
    fireEvent.input(input(screen.getByLabelText("Conversation name")), {
      target: { value: "Back garden" },
    });
    fireEvent.input(textarea(screen.getByLabelText("Scope brief")), {
      target: { value: "Plan vegetables and irrigation." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save conversation" }));

    await waitFor(() => {
      expect(host.chat.updateConversationCalls.at(-1)).toEqual({
        body: {
          display_name: "Back garden",
          scope_brief: "Plan vegetables and irrigation.",
        },
        conversationId: garden.id,
      });
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Archive conversation" }),
    );
    await waitFor(() => {
      expect(window.location.pathname).toBe("/chat");
    });
  });

  test("opens targeted reminders when an active prompt blocks archive", async () => {
    const garden = scoped("018f0000-0000-7000-8000-000000000109", "Garden");
    const host = new FakeHost({ authenticated: true });
    host.chat.storedConversations = [conversation, garden];
    host.chat.archiveConversationRejections = [
      new ConversationArchiveBlockedError("active_prompt_trigger"),
    ];
    renderApp(host, undefined, { path: `/chat/${garden.id}` });

    fireEvent.click(
      await screen.findByRole("button", { name: "Archive conversation" }),
    );

    await waitFor(() => {
      expect(window.location.pathname).toBe("/browse/reminders");
      expect(window.location.search).toBe(`?conversation=${garden.id}`);
    });
  });

  test("pins Main and shows active Scoped unread and working state in host order", async () => {
    const working = scoped("018f0000-0000-7000-8000-000000000104", "Working", {
      pending_turn_count: 1,
    });
    const unread = scoped("018f0000-0000-7000-8000-000000000105", "Unread", {
      has_unread: true,
    });
    const archived = scoped(
      "018f0000-0000-7000-8000-000000000106",
      "Archived",
      { archived_at: "2026-01-03T00:00:00Z", status: "archived" },
    );
    const host = new FakeHost({ authenticated: true });
    host.chat.storedConversations = [conversation, working, unread, archived];
    renderApp(host, undefined, { path: "/chat" });

    await screen.findByText("Working");
    const navigation = screen.getByRole("region", {
      name: "Conversations",
    });
    expect(
      within(navigation)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual(
      expect.arrayContaining([
        expect.stringContaining("Main Chat"),
        expect.stringContaining("Working"),
        expect.stringContaining("Unread"),
      ]),
    );
    expect(within(navigation).queryByText("Archived")).not.toBeInTheDocument();
    expect(
      within(navigation).getByLabelText("Working conversation"),
    ).toBeVisible();
    expect(
      within(navigation).getByLabelText("Unread conversation"),
    ).toBeVisible();
  });

  test("switching Conversations stops voice without aborting the background turn", async () => {
    class Recorder {
      ondataavailable: ((event: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;
      started = false;
      start(): void {
        this.started = true;
      }
      stop(): void {
        this.onstop?.();
      }
    }
    vi.stubGlobal("MediaRecorder", Recorder);
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: () =>
          Promise.resolve({ getTracks: () => [] } as unknown as MediaStream),
      },
    });
    const garden = scoped("018f0000-0000-7000-8000-000000000110", "Garden");
    const host = new FakeHost({ authenticated: true });
    host.chat.storedConversations = [conversation, garden];
    const bus = renderApp(host, undefined, { path: "/chat" });

    fireEvent.click(
      await screen.findByRole("button", { name: "Start voice conversation" }),
    );
    await screen.findByText("Listening…");
    fireEvent.input(textarea(screen.getByLabelText("Message")), {
      target: { value: "Keep working" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    const navigation = screen.getByRole("region", { name: "Conversations" });
    fireEvent.click(within(navigation).getByRole("link", { name: "Garden" }));

    await waitFor(() => {
      expect(window.location.pathname).toBe(`/chat/${garden.id}`);
    });
    expect(
      screen.getByRole("button", { name: "Start voice conversation" }),
    ).toHaveAttribute("aria-pressed", "false");
    expect(bus.sent).toEqual([
      {
        content: "Keep working",
        conversationId: conversation.id,
        replyMode: "spoken",
        type: "prompt",
      },
    ]);
  });

  test("duplicate names include their scope excerpt in navigation and target labels", async () => {
    const vegetables = scoped(
      "018f0000-0000-7000-8000-000000000120",
      "Garden",
      { scope_brief: "Vegetables and irrigation" },
    );
    const landscaping = scoped(
      "018f0000-0000-7000-8000-000000000121",
      "Garden",
      { scope_brief: "Landscaping and trees" },
    );
    const host = new FakeHost({ authenticated: true });
    host.chat.storedConversations = [conversation, vegetables, landscaping];
    renderApp(host, undefined, { path: "/chat" });

    const navigation = await screen.findByRole("region", {
      name: "Conversations",
    });
    expect(
      await within(navigation).findByRole("link", {
        name: "Garden · Vegetables and irrigation",
      }),
    ).toBeVisible();
    expect(
      await within(navigation).findByRole("link", {
        name: "Garden · Landscaping and trees",
      }),
    ).toBeVisible();
  });

  test("exact duplicate names and scopes include a stable UUID suffix", async () => {
    const first = scoped("018f0000-0000-7000-8000-00000000a120", "Garden", {
      scope_brief: "Same scope",
    });
    const second = scoped("018f0000-0000-7000-8000-00000000b121", "Garden", {
      scope_brief: "Same scope",
    });
    const host = new FakeHost({ authenticated: true });
    host.chat.storedConversations = [conversation, first, second];
    renderApp(host, undefined, { path: "/chat" });

    const navigation = await screen.findByRole("region", {
      name: "Conversations",
    });
    expect(
      await within(navigation).findByRole("link", {
        name: "Garden · Same scope · 00a120",
      }),
    ).toBeVisible();
    expect(
      await within(navigation).findByRole("link", {
        name: "Garden · Same scope · 00b121",
      }),
    ).toBeVisible();
  });

  test("mobile Conversation picker traps focus, closes on Escape, and restores focus", async () => {
    useMobileLayout();
    const garden = scoped("018f0000-0000-7000-8000-000000000107", "Garden");
    const host = new FakeHost({ authenticated: true });
    host.chat.storedConversations = [conversation, garden];
    renderApp(host, undefined, { path: "/chat" });

    const trigger = await screen.findByRole("button", {
      name: "Choose conversation",
    });
    trigger.focus();
    fireEvent.click(trigger);
    const picker = screen.getByRole("dialog", { name: "Choose conversation" });
    expect(picker).toHaveAttribute("aria-modal", "true");
    fireEvent.keyDown(document, { key: "Escape" });

    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "Choose conversation" }),
      ).not.toBeInTheDocument();
    });
    expect(trigger).toHaveFocus();
  });

  test("mobile Conversation picker closes after selecting a Scoped Conversation", async () => {
    useMobileLayout();
    const garden = scoped("018f0000-0000-7000-8000-000000000107", "Garden");
    const host = new FakeHost({ authenticated: true });
    host.chat.storedConversations = [conversation, garden];
    renderApp(host, undefined, { path: "/chat" });

    fireEvent.click(
      await screen.findByRole("button", { name: "Choose conversation" }),
    );
    const picker = screen.getByRole("dialog", { name: "Choose conversation" });
    fireEvent.click(within(picker).getByRole("link", { name: /Garden/ }));

    await waitFor(() => {
      expect(window.location.pathname).toBe(`/chat/${garden.id}`);
    });
    expect(
      screen.queryByRole("dialog", { name: "Choose conversation" }),
    ).not.toBeInTheDocument();
  });

  test("marks only the highest rendered Message sequence as read", async () => {
    const garden = scoped("018f0000-0000-7000-8000-000000000108", "Garden", {
      has_unread: true,
      latest_message_seq: 3,
    });
    const host = new FakeHost({
      authenticated: true,
      messages: [
        message({ conversation_id: garden.id, content: "one", seq: 1 }),
        message({ conversation_id: garden.id, content: "two", seq: 2 }),
      ],
    });
    host.chat.storedConversations = [conversation, garden];
    renderApp(host, undefined, { path: `/chat/${garden.id}` });

    await screen.findByText("two");
    await waitFor(() => {
      expect(host.chat.markConversationReadCalls).toEqual([
        { conversationId: garden.id, lastReadSeq: 2 },
      ]);
    });
    expect(host.chat.markConversationReadCalls).not.toContainEqual({
      conversationId: garden.id,
      lastReadSeq: 3,
    });
  });

  test("retries read position after a transient host failure", async () => {
    const garden = scoped("018f0000-0000-7000-8000-000000000108", "Garden", {
      has_unread: true,
      latest_message_seq: 2,
    });
    const host = new FakeHost({
      authenticated: true,
      messages: [message({ conversation_id: garden.id, seq: 2 })],
    });
    host.chat.storedConversations = [conversation, garden];
    host.chat.markConversationReadRejections = [new ApiError(503)];
    renderApp(host, undefined, { path: `/chat/${garden.id}` });

    await waitFor(() => {
      expect(host.chat.markConversationReadCalls).toHaveLength(2);
    });
    expect(host.chat.markConversationReadCalls).toEqual([
      { conversationId: garden.id, lastReadSeq: 2 },
      { conversationId: garden.id, lastReadSeq: 2 },
    ]);
  });

  test("shows a retryable error when restoring an archived Conversation fails", async () => {
    const archived = scoped("018f0000-0000-7000-8000-000000000109", "Archive", {
      status: "archived",
    });
    const host = new FakeHost({ authenticated: true });
    host.chat.storedConversations = [conversation, archived];
    host.chat.restoreConversationRejections = [new ApiError(503)];
    renderApp(host, undefined, { path: `/chat/${archived.id}` });

    fireEvent.click(
      await screen.findByRole("button", { name: "Restore conversation" }),
    );

    expect(
      await screen.findByRole("alert", {
        name: undefined,
      }),
    ).toHaveTextContent("Conversation could not be restored");
    expect(host.chat.restoreConversationCalls).toEqual([archived.id]);
  });

  test("renders a durable turn lifecycle when cancellation produced no Messages", async () => {
    const turnId = "018f0000-0000-7000-8000-000000000210";
    const host = new FakeHost({ authenticated: true });
    host.chat.storedTurns = [
      {
        completed_at: "2026-01-01T00:00:01Z",
        conversation_id: conversation.id,
        created_at: "2026-01-01T00:00:00Z",
        failure_code: null,
        failure_summary: null,
        id: turnId,
        origin: "scheduled",
        prompt: "Cancelled before execution",
        reply_mode: "text",
        request_id: null,
        started_at: null,
        status: "cancelled",
      },
    ];
    renderApp(host, undefined, { path: `/chat?turn=${turnId}` });

    const lifecycle = await screen.findByRole("article", {
      name: "Conversation turn lifecycle",
    });
    expect(lifecycle).toHaveTextContent("Cancelled before execution");
    expect(lifecycle).toHaveTextContent("Status: cancelled");
    expect(
      screen.queryByText("Conversation turn was not found."),
    ).not.toBeInTheDocument();
  });

  test("loads a stable turn query and renders Scheduled lifecycle without Feedback", async () => {
    const turnId = "018f0000-0000-7000-8000-000000000201";
    const scheduled = message({
      content: "Summarise my week",
      role: "scheduled",
      seq: 8,
      turn: {
        failure_code: "provider_failed",
        failure_summary: "The model failed while generating a response.",
        intended_fire_at: "2026-01-02T09:00:00Z",
        occurrence_id: "018f0000-0000-7000-8000-000000000203",
        origin: "scheduled",
        status: "failed",
        trigger_id: "018f0000-0000-7000-8000-000000000202",
      },
      turn_id: turnId,
    });
    const host = new FakeHost({ authenticated: true, messages: [scheduled] });
    renderApp(host, undefined, { path: `/chat?turn=${turnId}` });

    const row = await screen.findByLabelText("Scheduled message");
    expect(row).toHaveTextContent("Scheduled");
    expect(within(row).getByText("Summarise my week")).toHaveClass(
      "chat-message-plain",
    );
    expect(row).toHaveTextContent(
      "The model failed while generating a response.",
    );
    expect(host.chat.listMessagesCalls).toContainEqual({ turnId });
    expect(
      within(row).getByRole("button", { name: "Copy message" }),
    ).toBeVisible();
    expect(
      within(row).queryByRole("button", { name: "Quote message" }),
    ).not.toBeInTheDocument();
    expect(
      within(row).queryByRole("button", { name: "Record product feedback" }),
    ).not.toBeInTheDocument();
    expect(
      within(row).getByRole("link", { name: "View scheduled occurrence" }),
    ).toHaveAttribute(
      "href",
      "/browse/reminders?occurrence=018f0000-0000-7000-8000-000000000203",
    );
  });
});
