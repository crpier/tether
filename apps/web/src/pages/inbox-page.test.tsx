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
  duePrompt,
  memory,
  navigateTo,
  notification,
  renderApp,
} from "../testing/harness";

afterEach(cleanup);

describe("Inbox page", () => {
  test("inbox zero reads as clear", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);
    await navigateTo("Inbox");
    await screen.findByRole("heading", { name: "Inbox" });

    expect(
      await screen.findByText("Nothing awaiting you — inbox zero."),
    ).toBeInTheDocument();
  });

  test("groups items by kind with a per-group count", async () => {
    const api = new FakeApi({
      authenticated: true,
      duePrompts: [duePrompt({ question: "What is TCP?" })],
      memories: [memory({ content: "Prefers aisle seats" })],
    });
    api.storedNotifications = [notification({ body: "Call the dentist" })];
    renderApp(api);
    await navigateTo("Inbox");
    await screen.findByRole("heading", { name: "Inbox" });

    await waitFor(() => {
      expect(screen.getByText("Memory review (1)")).toBeInTheDocument();
    });
    expect(screen.getByText("Recall due (1)")).toBeInTheDocument();
    expect(screen.getByText("Fired reminder (1)")).toBeInTheDocument();
  });

  test("selecting a memory review item tethers it from the detail pane", async () => {
    const api = new FakeApi({
      authenticated: true,
      memories: [
        memory({ content: "Prefers aisle seats", id: "mem-1", version: 4 }),
      ],
    });
    renderApp(api);
    await navigateTo("Inbox");
    await screen.findByRole("heading", { name: "Inbox" });

    fireEvent.click(
      await screen.findByRole("button", { name: "Prefers aisle seats" }),
    );
    let detail: HTMLElement | undefined;
    await waitFor(() => {
      detail = screen.getAllByLabelText("Inbox item: Prefers aisle seats")[0];
      expect(detail).toBeInTheDocument();
    });
    fireEvent.click(within(detail!).getByRole("button", { name: "Tether" }));

    await waitFor(() => {
      expect(api.tetherMemoryCalls).toEqual([
        { memoryId: "mem-1", version: 4 },
      ]);
    });
  });

  test("bucket triage advisories surface their reason in the detail pane", async () => {
    const api = new FakeApi({
      authenticated: true,
      bucketItems: [],
    });
    api.triageReport = {
      active: [],
      duplicates: [],
      stale: [],
      under_specified: [
        {
          bucket_item_id: "bucket-1",
          reason: "movie is missing its release year",
        },
      ],
    };
    renderApp(api);
    await navigateTo("Inbox");
    await screen.findByRole("heading", { name: "Inbox" });

    await waitFor(() => {
      expect(screen.getByText("Bucket triage (1)")).toBeInTheDocument();
    });
    fireEvent.click(await screen.findByRole("button", { name: "bucket-1" }));
    await waitFor(() => {
      expect(
        screen.getAllByText("movie is missing its release year").length,
      ).toBeGreaterThan(0);
    });
  });

  test("answering a multiple-choice recall prompt submits the selected index", async () => {
    const api = new FakeApi({
      authenticated: true,
      duePrompts: [
        duePrompt({
          choices: ["One thread", "Many threads"],
          promptId: "prompt-1",
          question: "What does async IO multiplex?",
        }),
      ],
    });
    renderApp(api);
    await navigateTo("Inbox");
    await screen.findByRole("heading", { name: "Inbox" });

    fireEvent.click(
      await screen.findByRole("button", {
        name: "What does async IO multiplex?",
      }),
    );
    await waitFor(() => {
      expect(
        screen.getAllByRole("button", { name: "One thread" }).length,
      ).toBeGreaterThan(0);
    });
    fireEvent.click(screen.getAllByRole("button", { name: "One thread" })[0]);

    await waitFor(() => {
      expect(api.answerCalls).toHaveLength(1);
    });
    expect(api.answerCalls[0]).toMatchObject({
      promptId: "prompt-1",
      selected_index: 0,
    });
  });

  test("dismissing a fired reminder removes it from the inbox", async () => {
    const api = new FakeApi({ authenticated: true });
    api.storedNotifications = [
      notification({ body: "Call the dentist", id: "notif-1" }),
    ];
    renderApp(api);
    await navigateTo("Inbox");
    await screen.findByRole("heading", { name: "Inbox" });

    fireEvent.click(
      await screen.findByRole("button", { name: "Call the dentist" }),
    );
    await waitFor(() => {
      expect(
        screen.getAllByRole("button", { name: "Dismiss" }).length,
      ).toBeGreaterThan(0);
    });
    fireEvent.click(screen.getAllByRole("button", { name: "Dismiss" })[0]);

    await waitFor(() => {
      expect(api.dismissNotificationCalls).toEqual(["notif-1"]);
    });
  });
});
