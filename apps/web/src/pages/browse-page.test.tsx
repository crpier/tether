import { cleanup, fireEvent, screen, within } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import type { DreamRun } from "../host";
import {
  FakeHost,
  bucketItem,
  memory,
  navigateTo,
  renderApp,
  textarea,
} from "../testing/harness";

afterEach(cleanup);

describe("Browse page", () => {
  test("opens on the memory corpus, not the review queue", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    await navigateTo("Browse");
    await screen.findByRole("heading", { name: "Browse" });

    expect(
      await screen.findByRole("region", { name: "Memories" }),
    ).toBeInTheDocument();
    // The review-only affordance (capture form) never appears here; Inbox
    // owns memory review.
    expect(screen.queryByLabelText("Capture")).not.toBeInTheDocument();
  });

  test("shows Dream history and explains a selected run's Memory changes", async () => {
    const run: DreamRun = {
      attempts: 1,
      completed_at: "2026-08-21T08:01:05Z",
      conversation_id: "019f0000-0000-7000-8000-000000000001",
      conversation_title: "Seat preferences",
      created_at: "2026-08-21T08:01:00Z",
      error: null,
      evidence_end_seq: 18,
      evidence_start_seq: 12,
      id: "019f0000-0000-7000-8000-000000000002",
      kind: "manual",
      mutation_count: 1,
      status: "success",
      updated_at: "2026-08-21T08:01:05Z",
    };
    const host = new FakeHost({
      authenticated: true,
      dreamRunDetails: {
        [run.id]: {
          mutations: [
            {
              actor: "dream",
              attempts: 1,
              created_at: "2026-08-21T08:01:04Z",
              error: null,
              id: "019f0000-0000-7000-8000-000000000003",
              operation: "write",
              status: "acknowledged",
              tool_call_id: "write-seat-preferences",
              updated_at: "2026-08-21T08:01:05Z",
              workspace_path: "preferences/seating.md",
            },
          ],
          run,
        },
      },
      dreamRuns: [run],
    });
    renderApp(host);
    await navigateTo("Browse");

    const tabs = await screen.findByRole("tablist", { name: "Browse view" });
    fireEvent.click(within(tabs).getByRole("tab", { name: "Dreaming" }));

    const panel = await screen.findByRole("region", { name: "Dreaming" });
    const historyItem = await within(panel).findByRole("button", {
      name: /Changed.*Seat preferences/,
    });
    expect(historyItem).toHaveTextContent("Messages 12–18");
    expect(historyItem).toHaveTextContent("1 Memory change");
    fireEvent.click(historyItem);

    const detail = await within(panel).findByRole("region", {
      name: "Dream run details",
    });
    await within(detail).findByText("preferences/seating.md");
    expect(detail).toHaveTextContent("Wrote");
    expect(detail).toHaveTextContent("Acknowledged");
  });

  test("preserves an unsaved memory edit across Browse tabs", async () => {
    const host = new FakeHost({
      authenticated: true,
      memories: [
        memory({ content: "Aisle seats", id: "mem-1", state: "tethered" }),
      ],
    });
    renderApp(host);
    await navigateTo("Browse");

    const tabs = await screen.findByRole("tablist", { name: "Browse view" });
    const row = await screen.findByLabelText("Memory: Aisle seats");
    fireEvent.click(within(row).getByRole("button", { name: /^Edit/ }));
    fireEvent.input(textarea(screen.getByLabelText("Memory content")), {
      target: { value: "Window seats" },
    });

    fireEvent.click(within(tabs).getByRole("tab", { name: "Bucket" }));
    expect(
      await screen.findByRole("region", { name: "Bucket" }),
    ).toBeInTheDocument();
    fireEvent.click(within(tabs).getByRole("tab", { name: "Memories" }));

    expect(textarea(screen.getByLabelText("Memory content")).value).toBe(
      "Window seats",
    );
    expect(host.memories.editMemoryCalls).toHaveLength(0);
  });

  test("removes preserved Memories controls from inactive Browse tabs", async () => {
    const host = new FakeHost({
      authenticated: true,
      memories: [
        memory({ content: "Aisle seats", id: "mem-1", state: "tethered" }),
      ],
    });
    renderApp(host);
    await navigateTo("Browse");

    const tabs = await screen.findByRole("tablist", { name: "Browse view" });
    await screen.findByRole("tabpanel", { name: "Memories" });

    fireEvent.click(within(tabs).getByRole("tab", { name: "Reminders" }));
    expect(
      await screen.findByRole("region", { name: "Reminders" }),
    ).toBeInTheDocument();

    expect(
      screen.queryByRole("tabpanel", { hidden: true, name: "Memories" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("searchbox", {
        hidden: true,
        name: "Search memories",
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { hidden: true, name: /^Edit Memory/ }),
    ).not.toBeInTheDocument();
  });

  test("unmounts inactive Memories controls while Bucket is active", async () => {
    const host = new FakeHost({
      authenticated: true,
      memories: [
        memory({ content: "Aisle seats", id: "mem-1", state: "tethered" }),
      ],
    });
    renderApp(host);
    await navigateTo("Browse");

    const tabs = await screen.findByRole("tablist", { name: "Browse view" });
    await screen.findByRole("searchbox", { name: "Search memories" });

    fireEvent.click(within(tabs).getByRole("tab", { name: "Bucket" }));
    expect(
      await screen.findByRole("region", { name: "Bucket" }),
    ).toBeInTheDocument();

    expect(
      screen.queryByRole("searchbox", {
        hidden: true,
        name: "Search memories",
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        hidden: true,
        name: /^Edit Memory/,
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        hidden: true,
        name: /^Reject Memory/,
      }),
    ).not.toBeInTheDocument();
  });

  test("unmounts inactive Memories controls while Panels empty state is active", async () => {
    const host = new FakeHost({
      authenticated: true,
      memories: [
        memory({ content: "Aisle seats", id: "mem-1", state: "tethered" }),
      ],
      panels: [],
    });
    renderApp(host);
    await navigateTo("Browse");

    const tabs = await screen.findByRole("tablist", { name: "Browse view" });
    await screen.findByRole("searchbox", { name: "Search memories" });

    fireEvent.click(within(tabs).getByRole("tab", { name: "Panels" }));
    expect(
      await screen.findByRole("region", { name: "Panels" }),
    ).toBeInTheDocument();

    expect(
      screen.queryByRole("searchbox", {
        hidden: true,
        name: "Search memories",
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        hidden: true,
        name: /^Edit Memory/,
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        hidden: true,
        name: /^Reject Memory/,
      }),
    ).not.toBeInTheDocument();
  });

  test("opens the Todos tab from its direct Browse URL", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host, undefined, { path: "/browse/todos" });

    await screen.findByRole("heading", { name: "Browse" });
    expect(
      await screen.findByRole("region", { name: "Todos" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Todos" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  test("opens Reminders from the Browse tab query deep link", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host, undefined, { path: "/browse?tab=reminders" });

    await screen.findByRole("heading", { name: "Browse" });
    expect(
      await screen.findByRole("region", { name: "Reminders" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Reminders" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  test("updates the URL when switching Browse tabs", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host, undefined, { path: "/browse" });

    await screen.findByRole("heading", { name: "Browse" });
    const tabs = screen.getByRole("tablist", { name: "Browse view" });

    fireEvent.click(within(tabs).getByRole("tab", { name: "Reminders" }));
    expect(
      await screen.findByRole("region", { name: "Reminders" }),
    ).toBeInTheDocument();
    expect(window.location.pathname).toBe("/browse/reminders");

    fireEvent.click(within(tabs).getByRole("tab", { name: "Todos" }));
    expect(
      await screen.findByRole("region", { name: "Todos" }),
    ).toBeInTheDocument();
    expect(window.location.pathname).toBe("/browse/todos");
  });

  test("opens Bucket history from its direct Browse URL", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host, undefined, { path: "/browse/bucket/history" });

    await screen.findByRole("heading", { name: "Browse" });
    expect(
      await screen.findByRole("region", { name: "Bucket" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Bucket" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "History" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  test("opens Bucket history from its tab query deep link", async () => {
    const host = new FakeHost({
      authenticated: true,
      bucketItems: [
        bucketItem({
          completed_at: "2026-01-02T00:00:00Z",
          state: "completed",
          title: "Watched long ago",
        }),
      ],
    });
    renderApp(host, undefined, { path: "/browse/bucket?tab=history" });

    await screen.findByRole("heading", { name: "Browse" });
    expect(
      await screen.findByRole("region", { name: "Bucket" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Bucket" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "History" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(
      screen.getByRole("searchbox", { name: "Search bucket history" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByLabelText("Bucket item: Watched long ago"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("searchbox", { name: "Search bucket items" }),
    ).not.toBeInTheDocument();
  });

  test("switches between Bucket, Todos, Reminders and Panels tabs", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    await navigateTo("Browse");
    await screen.findByRole("heading", { name: "Browse" });

    const tabs = screen.getByRole("tablist", { name: "Browse view" });

    fireEvent.click(within(tabs).getByRole("tab", { name: "Todos" }));
    expect(
      await screen.findByRole("region", { name: "Todos" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tabpanel", { name: "Todos" })).toBeInTheDocument();

    fireEvent.click(within(tabs).getByRole("tab", { name: "Reminders" }));
    expect(
      await screen.findByRole("region", { name: "Reminders" }),
    ).toBeInTheDocument();
  });
});
