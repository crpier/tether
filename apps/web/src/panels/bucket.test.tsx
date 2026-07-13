import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { ApiError } from "../api";
import { FakeApi, bucketItem, input, renderApp } from "../testing/harness";

afterEach(cleanup);

describe("Bucket panel", () => {
  test("lists active items with type, intent context and created date", async () => {
    const api = new FakeApi({
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
    renderApp(api);

    const row = await screen.findByLabelText("Bucket item: Dune");
    expect(row).toHaveTextContent("Dune");
    expect(row).toHaveTextContent("movie");
    expect(row).toHaveTextContent("a friend raved");
    expect(row).toHaveTextContent(
      new Date("2026-01-05T00:00:00Z").toLocaleDateString(),
    );
  });

  test("adding a movie posts the typed payload with its intent context", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

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
      expect(api.addBucketItemCalls).toHaveLength(1);
    });
    const body = api.addBucketItemCalls[0];
    expect(body.item_type).toBe("movie");
    expect(body.data).toEqual({ title: "Dune", year: 2021 });
    expect(body.intent_context).toBe("a friend raved");
    expect(
      await screen.findByLabelText("Bucket item: Dune"),
    ).toBeInTheDocument();
    // The form resets once the add lands.
    expect(input(screen.getByLabelText("Title")).value).toBe("");
    expect(input(screen.getByLabelText("Reason")).value).toBe("");
  });

  test("switching the item type swaps the payload fields", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

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
      expect(api.addBucketItemCalls).toHaveLength(1);
    });
    const body = api.addBucketItemCalls[0];
    expect(body.item_type).toBe("place");
    expect(body.data).toEqual({ location: "Portugal", name: "Lisbon" });
  });

  test("adding a travel item posts destination and season", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

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
      expect(api.addBucketItemCalls).toHaveLength(1);
    });
    const body = api.addBucketItemCalls[0];
    expect(body.item_type).toBe("travel");
    expect(body.data).toEqual({ destination: "Japan", season: "spring" });
  });

  test("an optional field left blank is omitted from the payload", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    fireEvent.input(input(await screen.findByLabelText("Title")), {
      target: { value: "Arrival" },
    });
    fireEvent.input(input(screen.getByLabelText("Reason")), {
      target: { value: "sci-fi kick" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add item" }));

    await waitFor(() => {
      expect(api.addBucketItemCalls).toHaveLength(1);
    });
    expect(api.addBucketItemCalls[0].data).toEqual({ title: "Arrival" });
  });

  test("a blank reason is rejected before any request", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    fireEvent.input(input(await screen.findByLabelText("Title")), {
      target: { value: "Dune" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add item" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Add a reason so future-you knows why",
    );
    expect(api.addBucketItemCalls).toHaveLength(0);
  });

  test("a warn dedup advisory is shown but the add still lands", async () => {
    const api = new FakeApi({ authenticated: true });
    api.nextDedup = {
      duplicates: [bucketItem({ id: "dup-1", state: "active", title: "Dune" })],
      severity: "warn",
    };
    renderApp(api);

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
    expect(api.addBucketItemCalls).toHaveLength(1);
  });

  test("an inform dedup advisory names the terminal duplicate's state", async () => {
    const api = new FakeApi({ authenticated: true });
    api.nextDedup = {
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
    renderApp(api);

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
    expect(api.addBucketItemCalls).toHaveLength(1);
  });

  test("completing an item calls the API with its version", async () => {
    const api = new FakeApi({
      authenticated: true,
      bucketItems: [bucketItem({ id: "item-1", title: "Dune", version: 3 })],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Bucket item: Dune");
    fireEvent.click(within(row).getByRole("button", { name: "Complete" }));

    await waitFor(() => {
      expect(api.completeBucketItemCalls).toEqual([
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
    expect(api.searchBucketItemsCalls).not.toContain("");
  });

  test("completing recovers from a stale-version 409 by refetching", async () => {
    const api = new FakeApi({
      authenticated: true,
      bucketItems: [bucketItem({ id: "item-1", title: "Dune", version: 1 })],
    });
    api.serverBucketItemVersions = { "item-1": 2 };
    renderApp(api);

    const row = await screen.findByLabelText("Bucket item: Dune");
    fireEvent.click(within(row).getByRole("button", { name: "Complete" }));

    await waitFor(() => {
      expect(api.completeBucketItemCalls).toEqual([
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
    const api = new FakeApi({
      authenticated: true,
      bucketItems: [bucketItem({ id: "item-1", title: "Dune", version: 2 })],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Bucket item: Dune");
    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(api.deleteBucketItemCalls).toEqual([
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
    const api = new FakeApi({
      authenticated: true,
      bucketItems: [bucketItem({ id: "item-1", title: "Dune", version: 1 })],
    });
    api.serverBucketItemVersions = { "item-1": 2 };
    renderApp(api);

    const row = await screen.findByLabelText("Bucket item: Dune");
    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(api.deleteBucketItemCalls).toEqual([
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
    const api = new FakeApi({
      authenticated: true,
      bucketItems: [bucketItem({ id: "item-1", title: "Dune", version: 1 })],
    });
    api.completeBucketItemRejections = [new ApiError(409), new ApiError(500)];
    renderApp(api);

    const row = await screen.findByLabelText("Bucket item: Dune");
    fireEvent.click(within(row).getByRole("button", { name: "Complete" }));

    await waitFor(() => {
      expect(api.completeBucketItemCalls).toHaveLength(2);
    });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(new ApiError(500).message);
    expect(alert).not.toHaveTextContent(new ApiError(409).message);
  });

  test("typing a search query lists matches from the search endpoint", async () => {
    const api = new FakeApi({
      authenticated: true,
      bucketItems: [
        bucketItem({ id: "item-1", title: "Blade Runner" }),
        bucketItem({ id: "item-2", title: "Dune" }),
      ],
    });
    renderApp(api);

    await screen.findByLabelText("Bucket item: Dune");
    fireEvent.input(input(screen.getByLabelText("Search")), {
      target: { value: "Blade" },
    });

    await waitFor(() => {
      expect(api.searchBucketItemsCalls).toContain("Blade");
    });
    expect(
      await screen.findByLabelText("Bucket item: Blade Runner"),
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Bucket item: Dune"),
    ).not.toBeInTheDocument();
  });

  test("the history view shows terminal items read-only", async () => {
    const api = new FakeApi({
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
    renderApp(api);

    await screen.findByLabelText("Bucket item: Still active");
    fireEvent.click(screen.getByRole("button", { name: "History" }));

    const completedRow = await screen.findByLabelText(
      "Bucket item: Watched long ago",
    );
    expect(completedRow).toHaveTextContent("completed");
    expect(completedRow).toHaveTextContent(
      new Date("2022-03-01T00:00:00Z").toLocaleDateString(),
    );
    const deletedRow = screen.getByLabelText("Bucket item: Changed my mind");
    expect(deletedRow).toHaveTextContent("deleted");
    // History is read-only: no lifecycle actions on terminal rows.
    expect(
      within(completedRow).queryByRole("button", { name: "Complete" }),
    ).not.toBeInTheDocument();
    expect(
      within(completedRow).queryByRole("button", { name: "Delete" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Bucket item: Still active"),
    ).not.toBeInTheDocument();
  });

  test("the triage view surfaces under-specified, duplicate and stale items", async () => {
    const stale = bucketItem({
      id: "item-3",
      intent_context: "saved on a whim",
      title: "Old intention",
    });
    const api = new FakeApi({
      authenticated: true,
      bucketItems: [
        bucketItem({ id: "item-1", title: "Dune" }),
        bucketItem({ id: "item-2", title: "Dune" }),
        stale,
      ],
    });
    api.triageReport = {
      active: api.storedBucketItems,
      duplicates: [{ bucket_item_ids: ["item-1", "item-2"] }],
      stale: [
        {
          bucket_item_id: "item-3",
          intent_context: {
            age_days: 240,
            decay: 0.6,
            intent_context: "saved on a whim",
          },
        },
      ],
      under_specified: [
        {
          bucket_item_id: "item-1",
          reason: "movie is missing its release year",
        },
      ],
    };
    renderApp(api);

    await screen.findAllByLabelText("Bucket item: Dune");
    fireEvent.click(screen.getByRole("button", { name: "Triage" }));

    expect(
      await screen.findByText(/movie is missing its release year/),
    ).toBeInTheDocument();
    expect(screen.getByText(/2 items share one identity/)).toBeInTheDocument();
    expect(screen.getByText(/saved 240 days ago/)).toBeInTheDocument();
    expect(screen.getByText(/saved on a whim/)).toBeInTheDocument();
  });

  test("an empty triage report reads as a healthy backlog", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    await screen.findByRole("heading", { name: "Bucket" });
    fireEvent.click(screen.getByRole("button", { name: "Triage" }));

    expect(
      await screen.findByText("Nothing to triage — the backlog looks healthy."),
    ).toBeInTheDocument();
  });

  test("a bucket-items invalidate frame refetches the active list", async () => {
    const api = new FakeApi({ authenticated: true });
    const bus = renderApp(api);

    await screen.findByRole("heading", { name: "Bucket" });
    await waitFor(() => {
      expect(api.listBucketItemsCalls).toBeGreaterThan(0);
    });
    const before = api.listBucketItemsCalls;
    api.storedBucketItems = [bucketItem({ title: "Captured by the agent" })];
    bus.emit({ keys: ["bucket-items"], type: "invalidate" });

    await waitFor(() => {
      expect(api.listBucketItemsCalls).toBeGreaterThan(before);
    });
    expect(
      await screen.findByLabelText("Bucket item: Captured by the agent"),
    ).toBeInTheDocument();
  });
});
