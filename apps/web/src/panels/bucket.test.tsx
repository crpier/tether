import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test, vi } from "vitest";

import { ApiError } from "../host/error";
import { formatDate } from "../lib/format";
import {
  FakeHost,
  bucketItem,
  input,
  navigateTo,
  renderApp,
} from "../testing/harness";

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

describe("Bucket panel", () => {
  test("keeps the creation form collapsed until Add item is chosen", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));

    expect(screen.queryByLabelText("Title")).not.toBeInTheDocument();
    expect(
      screen.getByText(
        /Bucket items are things you intend to consume or visit/,
      ),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add item" }));
    expect(await screen.findByLabelText("Title")).toBeInTheDocument();
  });

  test("names the item type select from its label only", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));
    fireEvent.click(await screen.findByRole("button", { name: "Add item" }));

    const typeSelect = await screen.findByRole("combobox", { name: "Type" });
    expect(typeSelect).toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", {
        name: "TypeBookMoviePlacePurchaseTravel",
      }),
    ).not.toBeInTheDocument();
    expect((typeSelect as HTMLSelectElement).labels[0].textContent).toBe(
      "Type",
    );
  });

  test("lists active items with type, intent context and created date", async () => {
    const host = new FakeHost({
      authenticated: true,
      bucketItems: [
        bucketItem({
          created_at: "2026-01-05T00:00:00Z",
          intent_context: "a friend raved",
          item_type: "movie",
          title: "Dune",
        }),
      ],
    });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));

    const row = await screen.findByLabelText("Bucket item: Dune");
    expect(row).toHaveTextContent("Dune");
    expect(row).toHaveTextContent("movie");
    expect(row).toHaveTextContent("a friend raved");
    expect(row).toHaveTextContent(formatDate(new Date("2026-01-05T00:00:00Z")));
  });

  test("shows type-specific metadata on active item cards", async () => {
    const host = new FakeHost({
      authenticated: true,
      bucketItems: [
        bucketItem({
          data: { title: "Dune", year: 2021 },
          item_type: "movie",
          title: "Dune",
        }),
      ],
    });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));

    expect(await screen.findByLabelText("Bucket item: Dune")).toHaveTextContent(
      "Year: 2021",
    );
  });

  test("adding a movie posts the typed payload with its intent context", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));
    fireEvent.click(await screen.findByRole("button", { name: "Add item" }));

    fireEvent.input(input(await screen.findByLabelText("Title")), {
      target: { value: "Dune" },
    });
    fireEvent.input(input(screen.getByLabelText("Year")), {
      target: { value: "2021" },
    });
    fireEvent.input(input(screen.getByLabelText("Reason")), {
      target: { value: "a friend raved" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add item" }));

    await waitFor(() => {
      expect(host.bucket.addBucketItemCalls).toHaveLength(1);
    });
    const body = host.bucket.addBucketItemCalls[0];
    expect(body.item_type).toBe("movie");
    expect(body.data).toEqual({ title: "Dune", year: 2021 });
    expect(body.intent_context).toBe("a friend raved");
    expect(
      await screen.findByLabelText("Bucket item: Dune"),
    ).toBeInTheDocument();
    // The form collapses once the add lands.
    expect(screen.queryByLabelText("Title")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Add item" }),
    ).toBeInTheDocument();
  });

  test("switching the item type swaps the payload fields", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));
    fireEvent.click(await screen.findByRole("button", { name: "Add item" }));

    fireEvent.change(await screen.findByLabelText("Type"), {
      target: { value: "place" },
    });
    fireEvent.input(input(screen.getByLabelText("Name")), {
      target: { value: "Lisbon" },
    });
    fireEvent.input(input(screen.getByLabelText("Location")), {
      target: { value: "Portugal" },
    });
    fireEvent.input(input(screen.getByLabelText("Reason")), {
      target: { value: "want to visit" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add item" }));

    await waitFor(() => {
      expect(host.bucket.addBucketItemCalls).toHaveLength(1);
    });
    const body = host.bucket.addBucketItemCalls[0];
    expect(body.item_type).toBe("place");
    expect(body.data).toEqual({ location: "Portugal", name: "Lisbon" });
  });

  test("adding a travel item posts destination and season", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));
    fireEvent.click(await screen.findByRole("button", { name: "Add item" }));

    fireEvent.change(await screen.findByLabelText("Type"), {
      target: { value: "travel" },
    });
    fireEvent.input(input(screen.getByLabelText("Destination")), {
      target: { value: "Japan" },
    });
    fireEvent.input(input(screen.getByLabelText("Season")), {
      target: { value: "spring" },
    });
    fireEvent.input(input(screen.getByLabelText("Reason")), {
      target: { value: "cherry blossoms" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add item" }));

    await waitFor(() => {
      expect(host.bucket.addBucketItemCalls).toHaveLength(1);
    });
    const body = host.bucket.addBucketItemCalls[0];
    expect(body.item_type).toBe("travel");
    expect(body.data).toEqual({ destination: "Japan", season: "spring" });
  });

  test("an optional field left blank is omitted from the payload", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));
    fireEvent.click(await screen.findByRole("button", { name: "Add item" }));

    fireEvent.input(input(await screen.findByLabelText("Title")), {
      target: { value: "Arrival" },
    });
    fireEvent.input(input(screen.getByLabelText("Reason")), {
      target: { value: "sci-fi kick" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add item" }));

    await waitFor(() => {
      expect(host.bucket.addBucketItemCalls).toHaveLength(1);
    });
    expect(host.bucket.addBucketItemCalls[0].data).toEqual({
      title: "Arrival",
    });
  });

  test("adding a book posts title and author", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));
    fireEvent.click(await screen.findByRole("button", { name: "Add item" }));

    fireEvent.change(await screen.findByLabelText("Type"), {
      target: { value: "book" },
    });
    fireEvent.input(input(screen.getByLabelText("Title")), {
      target: { value: "Dune" },
    });
    fireEvent.input(input(screen.getByLabelText("Author")), {
      target: { value: "Frank Herbert" },
    });
    fireEvent.input(input(screen.getByLabelText("Reason")), {
      target: { value: "the movie was great" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add item" }));

    await waitFor(() => {
      expect(host.bucket.addBucketItemCalls).toHaveLength(1);
    });
    const body = host.bucket.addBucketItemCalls[0];
    expect(body.item_type).toBe("book");
    expect(body.data).toEqual({ author: "Frank Herbert", title: "Dune" });
  });

  test("a non-numeric year is rejected before any request", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));
    fireEvent.click(await screen.findByRole("button", { name: "Add item" }));

    fireEvent.input(input(await screen.findByLabelText("Title")), {
      target: { value: "Dune" },
    });
    fireEvent.input(input(screen.getByLabelText("Year")), {
      target: { value: "next year" },
    });
    fireEvent.input(input(screen.getByLabelText("Reason")), {
      target: { value: "a friend raved" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add item" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Year must be a whole number",
    );
    expect(host.bucket.addBucketItemCalls).toHaveLength(0);
  });

  test("a failed add surfaces the error and keeps the form filled", async () => {
    const host = new FakeHost({ authenticated: true });
    host.bucket.addBucketItemRejections = [new ApiError(500)];
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));
    fireEvent.click(await screen.findByRole("button", { name: "Add item" }));

    fireEvent.input(input(await screen.findByLabelText("Title")), {
      target: { value: "Dune" },
    });
    fireEvent.input(input(screen.getByLabelText("Reason")), {
      target: { value: "a friend raved" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add item" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      new ApiError(500).message,
    );
    expect(host.bucket.addBucketItemCalls).toHaveLength(1);
    // The form keeps its values so the user can retry.
    expect(input(screen.getByLabelText("Title")).value).toBe("Dune");
    expect(input(screen.getByLabelText("Reason")).value).toBe("a friend raved");
  });

  test("a blank reason is rejected before any request", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));
    fireEvent.click(await screen.findByRole("button", { name: "Add item" }));

    fireEvent.input(input(await screen.findByLabelText("Title")), {
      target: { value: "Dune" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add item" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Add a reason so future-you knows why",
    );
    expect(host.bucket.addBucketItemCalls).toHaveLength(0);
  });

  test("a warn dedup advisory is shown but the add still lands", async () => {
    const host = new FakeHost({ authenticated: true });
    host.bucket.nextDedup = {
      duplicates: [bucketItem({ id: "dup-1", state: "active", title: "Dune" })],
      severity: "warn",
    };
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));
    fireEvent.click(await screen.findByRole("button", { name: "Add item" }));

    fireEvent.input(input(await screen.findByLabelText("Title")), {
      target: { value: "Dune" },
    });
    fireEvent.input(input(screen.getByLabelText("Reason")), {
      target: { value: "again" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add item" }));

    const advisory = await screen.findByRole("status", {
      name: "Duplicate advisory",
    });
    expect(advisory).toHaveTextContent(
      "Added, but it duplicates an active item",
    );
    expect(advisory).toHaveTextContent("Dune");
    expect(host.bucket.addBucketItemCalls).toHaveLength(1);
  });

  test("an inform dedup advisory names the terminal duplicate's state", async () => {
    const host = new FakeHost({ authenticated: true });
    host.bucket.nextDedup = {
      duplicates: [
        bucketItem({
          completed_at: "2022-03-01T00:00:00Z",
          id: "dup-1",
          state: "completed",
          title: "Dune",
        }),
      ],
      severity: "inform",
    };
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));
    fireEvent.click(await screen.findByRole("button", { name: "Add item" }));

    fireEvent.input(input(await screen.findByLabelText("Title")), {
      target: { value: "Dune" },
    });
    fireEvent.input(input(screen.getByLabelText("Reason")), {
      target: { value: "rewatch" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add item" }));

    const advisory = await screen.findByRole("status", {
      name: "Duplicate advisory",
    });
    expect(advisory).toHaveTextContent("Added — you've had this before");
    expect(advisory).toHaveTextContent("completed");
    expect(host.bucket.addBucketItemCalls).toHaveLength(1);
  });

  test("completing an item calls the API with its version", async () => {
    const host = new FakeHost({
      authenticated: true,
      bucketItems: [bucketItem({ id: "item-1", title: "Dune", version: 3 })],
    });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));

    const row = await screen.findByLabelText("Bucket item: Dune");
    fireEvent.click(within(row).getByRole("button", { name: /^Complete/ }));

    await waitFor(() => {
      expect(host.bucket.completeBucketItemCalls).toEqual([
        { bucketItemId: "item-1", version: 3 },
      ]);
    });
    await waitFor(() => {
      expect(
        screen.queryByLabelText("Bucket item: Dune"),
      ).not.toBeInTheDocument();
    });
    // The post-mutation refresh must not refetch the disabled empty-term
    // search query — the host rejects a blank search with a 400.
    expect(host.bucket.searchBucketItemsCalls).not.toContain("");
  });

  test("completing recovers from a stale-version 409 by refetching", async () => {
    const host = new FakeHost({
      authenticated: true,
      bucketItems: [bucketItem({ id: "item-1", title: "Dune", version: 1 })],
    });
    host.bucket.serverBucketItemVersions = { "item-1": 2 };
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));

    const row = await screen.findByLabelText("Bucket item: Dune");
    fireEvent.click(within(row).getByRole("button", { name: /^Complete/ }));

    await waitFor(() => {
      expect(host.bucket.completeBucketItemCalls).toEqual([
        { bucketItemId: "item-1", version: 1 },
        { bucketItemId: "item-1", version: 2 },
      ]);
    });
    expect(
      screen.queryByLabelText("Bucket item: Dune"),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  test("deleting an item calls the API with its version", async () => {
    const host = new FakeHost({
      authenticated: true,
      bucketItems: [bucketItem({ id: "item-1", title: "Dune", version: 2 })],
    });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));

    const row = await screen.findByLabelText("Bucket item: Dune");
    fireEvent.click(within(row).getByRole("button", { name: /^Delete/ }));

    await waitFor(() => {
      expect(host.bucket.deleteBucketItemCalls).toEqual([
        { bucketItemId: "item-1", version: 2 },
      ]);
    });
    await waitFor(() => {
      expect(
        screen.queryByLabelText("Bucket item: Dune"),
      ).not.toBeInTheDocument();
    });
  });

  test("deleting recovers from a stale-version 409 by refetching", async () => {
    const host = new FakeHost({
      authenticated: true,
      bucketItems: [bucketItem({ id: "item-1", title: "Dune", version: 1 })],
    });
    host.bucket.serverBucketItemVersions = { "item-1": 2 };
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));

    const row = await screen.findByLabelText("Bucket item: Dune");
    fireEvent.click(within(row).getByRole("button", { name: /^Delete/ }));

    await waitFor(() => {
      expect(host.bucket.deleteBucketItemCalls).toEqual([
        { bucketItemId: "item-1", version: 1 },
        { bucketItemId: "item-1", version: 2 },
      ]);
    });
    expect(
      screen.queryByLabelText("Bucket item: Dune"),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  test("a failed complete retry reports its own error, not the original 409", async () => {
    const host = new FakeHost({
      authenticated: true,
      bucketItems: [bucketItem({ id: "item-1", title: "Dune", version: 1 })],
    });
    host.bucket.completeBucketItemRejections = [
      new ApiError(409),
      new ApiError(500),
    ];
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));

    const row = await screen.findByLabelText("Bucket item: Dune");
    fireEvent.click(within(row).getByRole("button", { name: /^Complete/ }));

    await waitFor(() => {
      expect(host.bucket.completeBucketItemCalls).toHaveLength(2);
    });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(new ApiError(500).message);
    expect(alert).not.toHaveTextContent(new ApiError(409).message);
  });

  test("typing a search query lists matches from the search endpoint", async () => {
    const host = new FakeHost({
      authenticated: true,
      bucketItems: [
        bucketItem({ id: "item-1", title: "Blade Runner" }),
        bucketItem({ id: "item-2", title: "Dune" }),
      ],
    });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));

    await screen.findByLabelText("Bucket item: Dune");
    fireEvent.input(input(screen.getByLabelText("Search")), {
      target: { value: "Blade" },
    });

    await waitFor(() => {
      expect(host.bucket.searchBucketItemsCalls).toContain("Blade");
    });
    expect(
      await screen.findByLabelText("Bucket item: Blade Runner"),
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Bucket item: Dune"),
    ).not.toBeInTheDocument();
  });

  test("keystrokes are debounced into one search request per pause", async () => {
    const host = new FakeHost({
      authenticated: true,
      bucketItems: [bucketItem({ id: "item-1", title: "Blade Runner" })],
    });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));
    await screen.findByLabelText("Bucket item: Blade Runner");

    vi.useFakeTimers();
    const field = input(screen.getByLabelText("Search"));
    fireEvent.input(field, { target: { value: "B" } });
    fireEvent.input(field, { target: { value: "Bl" } });
    fireEvent.input(field, { target: { value: "Blade" } });
    expect(host.bucket.searchBucketItemsCalls).toEqual([]);
    await vi.advanceTimersByTimeAsync(150);
    vi.useRealTimers();

    await waitFor(() => {
      expect(host.bucket.searchBucketItemsCalls).toEqual(["Blade"]);
    });
  });

  test("a mutation does not refetch stale cached search terms", async () => {
    const host = new FakeHost({
      authenticated: true,
      bucketItems: [
        bucketItem({ id: "item-1", title: "Blade Runner" }),
        bucketItem({ id: "item-2", title: "Dune", version: 1 }),
      ],
    });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));
    await screen.findByLabelText("Bucket item: Dune");

    // Register a search cache entry, then clear the term so it goes stale.
    fireEvent.input(input(screen.getByLabelText("Search")), {
      target: { value: "Blade" },
    });
    await waitFor(() => {
      expect(host.bucket.searchBucketItemsCalls).toEqual(["Blade"]);
    });
    fireEvent.input(input(screen.getByLabelText("Search")), {
      target: { value: "" },
    });
    const row = await screen.findByLabelText("Bucket item: Dune");
    fireEvent.click(within(row).getByRole("button", { name: /^Complete/ }));

    await waitFor(() => {
      expect(
        screen.queryByLabelText("Bucket item: Dune"),
      ).not.toBeInTheDocument();
    });
    // The post-mutation refresh only refetches what is on screen; the stale
    // "Blade" cache entry waits until it is looked at again.
    expect(host.bucket.searchBucketItemsCalls).toEqual(["Blade"]);
  });

  test("the history view shows terminal items read-only", async () => {
    const host = new FakeHost({
      authenticated: true,
      bucketItems: [
        bucketItem({ id: "item-1", title: "Still active" }),
        bucketItem({
          completed_at: "2022-03-01T00:00:00Z",
          id: "item-2",
          state: "completed",
          title: "Watched long ago",
        }),
        bucketItem({
          deleted_at: "2023-06-01T00:00:00Z",
          id: "item-3",
          state: "deleted",
          title: "Changed my mind",
        }),
      ],
    });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));

    await screen.findByLabelText("Bucket item: Still active");
    fireEvent.click(screen.getByRole("tab", { name: "History" }));

    const completedRow = await screen.findByLabelText(
      "Bucket item: Watched long ago",
    );
    expect(completedRow).toHaveTextContent("completed");
    expect(completedRow).toHaveTextContent(
      formatDate(new Date("2022-03-01T00:00:00Z")),
    );
    const deletedRow = screen.getByLabelText("Bucket item: Changed my mind");
    expect(deletedRow).toHaveTextContent("deleted");
    // History is read-only: no lifecycle actions on terminal rows.
    expect(
      within(completedRow).queryByRole("button", { name: /^Complete/ }),
    ).not.toBeInTheDocument();
    expect(
      within(completedRow).queryByRole("button", { name: /^Delete/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Bucket item: Still active"),
    ).not.toBeInTheDocument();
  });

  test("shows type-specific metadata in history", async () => {
    const host = new FakeHost({
      authenticated: true,
      bucketItems: [
        bucketItem({
          completed_at: "2026-02-01T00:00:00Z",
          data: { title: "Dune", year: 2021 },
          item_type: "movie",
          state: "completed",
          title: "Dune",
        }),
      ],
    });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));
    fireEvent.click(await screen.findByRole("tab", { name: "History" }));

    expect(await screen.findByLabelText("Bucket item: Dune")).toHaveTextContent(
      "Year: 2021",
    );
  });

  test("history interleaves completed and deleted by terminal date, newest first", async () => {
    const host = new FakeHost({
      authenticated: true,
      bucketItems: [
        bucketItem({
          completed_at: "2024-01-01T00:00:00Z",
          id: "item-1",
          state: "completed",
          title: "Old completed",
        }),
        bucketItem({
          deleted_at: "2025-01-01T00:00:00Z",
          id: "item-2",
          state: "deleted",
          title: "Recent deleted",
        }),
        bucketItem({
          completed_at: "2026-01-01T00:00:00Z",
          id: "item-3",
          state: "completed",
          title: "Newest completed",
        }),
      ],
    });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));

    await screen.findByRole("heading", { name: "Bucket" });
    fireEvent.click(screen.getByRole("tab", { name: "History" }));

    await screen.findByLabelText("Bucket item: Newest completed");
    const rows = screen.getAllByLabelText(/^Bucket item: /);
    expect(rows.map((item) => item.getAttribute("aria-label"))).toEqual([
      "Bucket item: Newest completed",
      "Bucket item: Recent deleted",
      "Bucket item: Old completed",
    ]);
  });

  test("a bucket-items invalidate frame refetches the active list", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Bucket" }));

    await screen.findByRole("heading", { name: "Bucket" });
    await waitFor(() => {
      expect(host.bucket.listBucketItemsCalls).toBeGreaterThan(0);
    });
    const before = host.bucket.listBucketItemsCalls;
    host.bucket.storedBucketItems = [
      bucketItem({ title: "Captured by the agent" }),
    ];
    bus.emit({ keys: ["bucket-items"], type: "invalidate" });

    await waitFor(() => {
      expect(host.bucket.listBucketItemsCalls).toBeGreaterThan(before);
    });
    expect(
      await screen.findByLabelText("Bucket item: Captured by the agent"),
    ).toBeInTheDocument();
  });
});
