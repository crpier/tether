import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test, vi } from "vitest";

import { formatDate } from "../lib/format";
import {
  FakeApi,
  input,
  memory,
  navigateTo,
  renderApp,
  textarea,
} from "../testing/harness";

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

// The Memories panel now only ever mounts pinned to "corpus" (#250): review
// moved to the Inbox page (see inbox-page.test.tsx), Browse only ever opens
// on the tethered corpus + search.
async function openCorpus(): Promise<HTMLElement> {
  await navigateTo("Browse");
  return screen.findByRole("region", { name: "Memories" });
}

describe("Memories panel (Browse corpus)", () => {
  test("lists tethered memories with edit and reject but no tether", async () => {
    const api = new FakeApi({
      authenticated: true,
      memories: [
        memory({
          content: "Trusted fact",
          id: "mem-2",
          state: "tethered",
          tethered_at: "2026-01-06T00:00:00Z",
        }),
      ],
    });
    renderApp(api);

    const panel = await openCorpus();
    const row = await within(panel).findByLabelText("Memory: Trusted fact");
    expect(row).toHaveTextContent(formatDate(new Date("2026-01-06T00:00:00Z")));
    expect(
      within(row).getByRole("button", { name: "Edit" }),
    ).toBeInTheDocument();
    expect(
      within(row).getByRole("button", { name: "Reject" }),
    ).toBeInTheDocument();
    expect(
      within(row).queryByRole("button", { name: "Tether" }),
    ).not.toBeInTheDocument();
  });

  test("editing a memory saves the new content at the observed version", async () => {
    const api = new FakeApi({
      authenticated: true,
      memories: [
        memory({
          content: "Aisle seats",
          id: "mem-1",
          state: "tethered",
          version: 2,
        }),
      ],
    });
    renderApp(api);
    await openCorpus();

    const row = await screen.findByLabelText("Memory: Aisle seats");
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    const editor = textarea(screen.getByLabelText("Memory content"));
    fireEvent.input(editor, { target: { value: "Window seats" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(api.editMemoryCalls).toEqual([
        { content: "Window seats", memoryId: "mem-1", version: 2 },
      ]);
    });
    expect(
      await screen.findByLabelText("Memory: Window seats"),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Memory content")).not.toBeInTheDocument();
  });

  test("cancelling an edit restores the row without a request", async () => {
    const api = new FakeApi({
      authenticated: true,
      memories: [
        memory({ content: "Aisle seats", id: "mem-1", state: "tethered" }),
      ],
    });
    renderApp(api);
    await openCorpus();

    const row = await screen.findByLabelText("Memory: Aisle seats");
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    fireEvent.input(textarea(screen.getByLabelText("Memory content")), {
      target: { value: "Window seats" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(
      await screen.findByLabelText("Memory: Aisle seats"),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Memory content")).not.toBeInTheDocument();
    expect(api.editMemoryCalls).toHaveLength(0);
  });

  test("an edit 409 with an unchanged basis retries with the fresh version", async () => {
    const api = new FakeApi({
      authenticated: true,
      memories: [
        memory({
          content: "Aisle seats",
          id: "mem-1",
          state: "tethered",
          version: 1,
        }),
      ],
    });
    // The version moved (e.g. a concurrent edit from another tab landed
    // first) but the content basis this edit was formulated against is
    // intact, so the retry is safe.
    api.serverMemoryVersions = { "mem-1": 2 };
    renderApp(api);
    await openCorpus();

    const row = await screen.findByLabelText("Memory: Aisle seats");
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    fireEvent.input(textarea(screen.getByLabelText("Memory content")), {
      target: { value: "Window seats" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(api.editMemoryCalls).toEqual([
        { content: "Window seats", memoryId: "mem-1", version: 1 },
        { content: "Window seats", memoryId: "mem-1", version: 2 },
      ]);
    });
    expect(
      await screen.findByLabelText("Memory: Window seats"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  test("an edit 409 with concurrently changed content re-arms the editor instead of clobbering", async () => {
    const api = new FakeApi({
      authenticated: true,
      memories: [
        memory({
          content: "Aisle seats",
          id: "mem-1",
          state: "tethered",
          version: 1,
        }),
      ],
    });
    api.serverMemoryVersions = { "mem-1": 2 };
    api.serverMemoryEdits = { "mem-1": { content: "Emergency exit seats" } };
    renderApp(api);
    await openCorpus();

    const row = await screen.findByLabelText("Memory: Aisle seats");
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    fireEvent.input(textarea(screen.getByLabelText("Memory content")), {
      target: { value: "Window seats" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    // No blind retry: one call went out, the editor stays open with the
    // draft, and the conflict is reported.
    await waitFor(() => {
      expect(api.editMemoryCalls).toHaveLength(1);
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "changed while you were editing",
    );
    const editor = textarea(screen.getByLabelText("Memory content"));
    expect(editor.value).toBe("Window seats");

    // A deliberate second save wins against the fresh version.
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(api.editMemoryCalls).toEqual([
        { content: "Window seats", memoryId: "mem-1", version: 1 },
        { content: "Window seats", memoryId: "mem-1", version: 2 },
      ]);
    });
    expect(
      await screen.findByLabelText("Memory: Window seats"),
    ).toBeInTheDocument();
  });

  test("rejecting a tethered memory calls the API with its version", async () => {
    const api = new FakeApi({
      authenticated: true,
      memories: [
        memory({
          content: "Aisle seats",
          id: "mem-1",
          state: "tethered",
          version: 3,
        }),
      ],
    });
    renderApp(api);
    const panel = await openCorpus();

    const row = await within(panel).findByLabelText("Memory: Aisle seats");
    fireEvent.click(within(row).getByRole("button", { name: "Reject" }));

    await waitFor(() => {
      expect(api.rejectMemoryCalls).toEqual([
        { memoryId: "mem-1", version: 3 },
      ]);
    });
    await waitFor(() => {
      expect(
        screen.queryByLabelText("Memory: Aisle seats"),
      ).not.toBeInTheDocument();
    });
  });

  test("typing a corpus search lists matches from the search endpoint", async () => {
    const api = new FakeApi({
      authenticated: true,
      memories: [
        memory({
          content: "Prefers aisle seats",
          id: "mem-1",
          state: "tethered",
          tethered_at: "2026-01-06T00:00:00Z",
        }),
        memory({
          content: "Allergic to peanuts",
          id: "mem-2",
          state: "tethered",
          tethered_at: "2026-01-06T00:00:00Z",
        }),
      ],
    });
    renderApp(api);

    const panel = await openCorpus();
    await screen.findByLabelText("Memory: Allergic to peanuts");
    fireEvent.input(input(within(panel).getByLabelText("Search memories")), {
      target: { value: "aisle" },
    });

    await waitFor(() => {
      expect(api.searchMemoriesCalls).toContain("aisle");
    });
    expect(
      await screen.findByLabelText("Memory: Prefers aisle seats"),
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Memory: Allergic to peanuts"),
    ).not.toBeInTheDocument();
  });

  test("corpus search keystrokes are debounced into one request per pause", async () => {
    const api = new FakeApi({
      authenticated: true,
      memories: [
        memory({
          content: "Prefers aisle seats",
          id: "mem-1",
          state: "tethered",
          tethered_at: "2026-01-06T00:00:00Z",
        }),
      ],
    });
    renderApp(api);

    const panel = await openCorpus();
    await screen.findByLabelText("Memory: Prefers aisle seats");

    vi.useFakeTimers();
    const field = input(within(panel).getByLabelText("Search memories"));
    fireEvent.input(field, { target: { value: "a" } });
    fireEvent.input(field, { target: { value: "ai" } });
    fireEvent.input(field, { target: { value: "aisle" } });
    expect(api.searchMemoriesCalls).toEqual([]);
    await vi.advanceTimersByTimeAsync(150);
    vi.useRealTimers();

    await waitFor(() => {
      expect(api.searchMemoriesCalls).toEqual(["aisle"]);
    });
  });

  test("a memories invalidate frame refetches the corpus", async () => {
    const api = new FakeApi({
      authenticated: true,
      memories: [
        memory({ content: "Old fact", state: "tethered", id: "mem-1" }),
      ],
    });
    const bus = renderApp(api);

    await openCorpus();
    await screen.findByLabelText("Memory: Old fact");
    const before = api.listMemoriesCalls;
    api.storedMemories = [
      ...api.storedMemories,
      memory({ content: "Captured by the agent", state: "tethered" }),
    ];
    bus.emit({ keys: ["memories", "review-queue"], type: "invalidate" });

    await waitFor(() => {
      expect(api.listMemoriesCalls).toBeGreaterThan(before);
    });
    expect(
      await screen.findByLabelText("Memory: Captured by the agent"),
    ).toBeInTheDocument();
  });

  test("an empty corpus names the view it belongs to", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    const panel = await openCorpus();
    expect(
      await within(panel).findByText("No tethered memories yet"),
    ).toBeInTheDocument();
  });
});
