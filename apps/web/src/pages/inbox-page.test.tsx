import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import {
  FakeHost,
  bucketItem,
  duePrompt,
  memory,
  navigateTo,
  notification,
  renderApp,
  transcriptDecision,
} from "../testing/harness";

afterEach(cleanup);

describe("Inbox page", () => {
  test("inbox zero reads as clear", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    await navigateTo("Inbox");
    await screen.findByRole("heading", { name: "Inbox" });

    expect(
      await screen.findByText("Nothing awaiting you — inbox zero."),
    ).toBeInTheDocument();
  });

  test("groups items by kind with a per-group count", async () => {
    const host = new FakeHost({
      authenticated: true,
      duePrompts: [duePrompt({ question: "What is TCP?" })],
      memories: [memory({ content: "Prefers aisle seats" })],
    });
    host.notifications.storedNotifications = [
      notification({ body: "Call the dentist" }),
    ];
    renderApp(host);
    await navigateTo("Inbox");
    await screen.findByRole("heading", { name: "Inbox" });

    await waitFor(() => {
      expect(screen.getByText("Memory review (1)")).toBeInTheDocument();
    });
    expect(screen.getByText("Recall due (1)")).toBeInTheDocument();
    expect(screen.getByText("Fired reminder (1)")).toBeInTheDocument();
  });

  test("selecting a memory review item tethers it from the detail pane", async () => {
    const host = new FakeHost({
      authenticated: true,
      memories: [
        memory({ content: "Prefers aisle seats", id: "mem-1", version: 4 }),
      ],
    });
    renderApp(host);
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
    fireEvent.click(within(detail!).getByRole("button", { name: /^Tether/ }));

    await waitFor(() => {
      expect(host.memories.tetherMemoryCalls).toEqual([
        { memoryId: "mem-1", version: 4 },
      ]);
    });
  });

  test("bucket triage advisories surface their reason in the detail pane", async () => {
    const host = new FakeHost({
      authenticated: true,
      bucketItems: [],
    });
    host.bucket.triageReport = {
      active: [],
      duplicates: [],
      purchase: { buy_now: [], missing_price_context: [], stale_watches: [] },
      stale: [],
      under_specified: [
        {
          bucket_item_id: "bucket-1",
          reason: "movie is missing its release year",
        },
      ],
    };
    renderApp(host);
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

  test("purchase triage surfaces missing price context", async () => {
    const purchase = bucketItem({
      id: "purchase-1",
      item_type: "purchase",
      title: "Aeropress",
    });
    const host = new FakeHost({ authenticated: true, bucketItems: [purchase] });
    host.bucket.triageReport = {
      active: [purchase],
      duplicates: [],
      purchase: {
        buy_now: [],
        missing_price_context: [purchase.id],
        stale_watches: [],
      },
      stale: [],
      under_specified: [],
    };
    renderApp(host);
    await navigateTo("Inbox");

    fireEvent.click(await screen.findByRole("button", { name: "Aeropress" }));

    await waitFor(() => {
      expect(
        screen.getAllByText("Purchase is missing a price or store").length,
      ).toBeGreaterThan(0);
    });
  });

  test("transcript failures ask for a decision with source context", async () => {
    const host = new FakeHost({
      authenticated: true,
      transcriptDecisions: [
        transcriptDecision({
          last_error: "No caption track found",
          title: "Captionless talk",
          video_id: "video-1",
        }),
      ],
    });
    renderApp(host);
    await navigateTo("Inbox");

    expect(
      await screen.findByText("Transcript decision (1)"),
    ).toBeInTheDocument();
    fireEvent.click(
      await screen.findByRole("button", { name: "Captionless talk" }),
    );

    const detail = (
      await screen.findAllByLabelText("Inbox item: Captionless talk")
    )[0];
    expect(
      within(detail).getByText("No caption track found"),
    ).toBeInTheDocument();
    expect(
      within(detail).getByRole("link", { name: "Watch on YouTube" }),
    ).toHaveAttribute("href", "https://www.youtube.com/watch?v=video-1");
  });

  test("keeping a transcript attempt reopens acquisition and clears the item", async () => {
    const host = new FakeHost({
      authenticated: true,
      transcriptDecisions: [transcriptDecision({ video_id: "video-1" })],
    });
    renderApp(host);
    await navigateTo("Inbox");
    fireEvent.click(
      await screen.findByRole("button", { name: "Captionless talk" }),
    );

    fireEvent.click(
      (await screen.findAllByRole("button", { name: "Keep trying" }))[0],
    );

    await waitFor(() => {
      expect(host.youtube.keepTryingTranscriptCalls).toEqual(["video-1"]);
      expect(
        screen.queryByText("Transcript decision (1)"),
      ).not.toBeInTheDocument();
    });
  });

  test("giving up settles transcript absence and clears the item", async () => {
    const host = new FakeHost({
      authenticated: true,
      transcriptDecisions: [transcriptDecision({ video_id: "video-1" })],
    });
    renderApp(host);
    await navigateTo("Inbox");
    fireEvent.click(
      await screen.findByRole("button", { name: "Captionless talk" }),
    );

    fireEvent.click(
      (await screen.findAllByRole("button", { name: "Give up" }))[0],
    );

    await waitFor(() => {
      expect(host.youtube.giveUpTranscriptCalls).toEqual(["video-1"]);
      expect(
        screen.queryByText("Transcript decision (1)"),
      ).not.toBeInTheDocument();
    });
  });

  test("answering a multiple-choice recall prompt submits the selected index", async () => {
    const host = new FakeHost({
      authenticated: true,
      duePrompts: [
        duePrompt({
          choices: ["One thread", "Many threads"],
          promptId: "prompt-1",
          question: "What does async IO multiplex?",
        }),
      ],
    });
    renderApp(host);
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
      expect(host.recall.answerCalls).toHaveLength(1);
    });
    expect(host.recall.answerCalls[0]).toMatchObject({
      promptId: "prompt-1",
      selected_index: 0,
    });
  });

  test("dismissing a fired reminder removes it from the inbox", async () => {
    const host = new FakeHost({ authenticated: true });
    host.notifications.storedNotifications = [
      notification({ body: "Call the dentist", id: "notif-1" }),
    ];
    renderApp(host);
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
      expect(host.notifications.dismissNotificationCalls).toEqual(["notif-1"]);
    });
  });
});
