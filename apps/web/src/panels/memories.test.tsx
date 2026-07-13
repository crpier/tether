import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test, vi } from "vitest";

import { ApiError } from "../api";
import {
  FakeApi,
  input,
  memory,
  renderApp,
  textarea,
} from "../testing/harness";

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

async function memoriesPanel(): Promise<HTMLElement> {
  return screen.findByRole("region", { name: "Memories" });
}

describe("Memories panel", () => {
  test("the review queue lists loose memories with content and created date", async () => {
    const api = new FakeApi({
      authenticated: true,
      memories: [
        memory({
          content: "I prefer aisle seats",
          created_at: "2026-01-05T00:00:00Z",
        }),
      ],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Memory: I prefer aisle seats");
    expect(row).toHaveTextContent("I prefer aisle seats");
    expect(row).toHaveTextContent(
      new Date("2026-01-05T00:00:00Z").toLocaleDateString(),
    );
  });

  test("capturing a memory posts the content and clears the form", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    const panel = await memoriesPanel();
    fireEvent.input(input(within(panel).getByLabelText("Capture")), {
      target: { value: "I prefer aisle seats" },
    });
    fireEvent.click(
      within(panel).getByRole("button", { name: "Capture memory" }),
    );

    await waitFor(() => {
      expect(api.captureMemoryCalls).toEqual(["I prefer aisle seats"]);
    });
    expect(
      await screen.findByLabelText("Memory: I prefer aisle seats"),
    ).toBeInTheDocument();
    expect(input(within(panel).getByLabelText("Capture")).value).toBe("");
  });

  test("a blank capture is rejected before any request", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    const panel = await memoriesPanel();
    fireEvent.click(
      within(panel).getByRole("button", { name: "Capture memory" }),
    );

    expect(await within(panel).findByRole("alert")).toHaveTextContent(
      "Write something to capture",
    );
    expect(api.captureMemoryCalls).toHaveLength(0);
  });

  test("a failed capture surfaces the error and keeps the draft", async () => {
    const api = new FakeApi({ authenticated: true });
    api.captureMemoryRejections = [new ApiError(500)];
    renderApp(api);

    const panel = await memoriesPanel();
    fireEvent.input(input(within(panel).getByLabelText("Capture")), {
      target: { value: "I prefer aisle seats" },
    });
    fireEvent.click(
      within(panel).getByRole("button", { name: "Capture memory" }),
    );

    expect(await within(panel).findByRole("alert")).toHaveTextContent(
      new ApiError(500).message,
    );
    expect(input(within(panel).getByLabelText("Capture")).value).toBe(
      "I prefer aisle seats",
    );
  });

  test("tethering calls the API with the observed version and empties the row from the queue", async () => {
    const api = new FakeApi({
      authenticated: true,
      memories: [memory({ content: "Aisle seats", id: "mem-1", version: 3 })],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Memory: Aisle seats");
    fireEvent.click(within(row).getByRole("button", { name: "Tether" }));

    await waitFor(() => {
      expect(api.tetherMemoryCalls).toEqual([
        { memoryId: "mem-1", version: 3 },
      ]);
    });
    await waitFor(() => {
      expect(
        screen.queryByLabelText("Memory: Aisle seats"),
      ).not.toBeInTheDocument();
    });
  });

  test("tethering recovers from a stale-version 409 by refetching and retrying", async () => {
    const api = new FakeApi({
      authenticated: true,
      memories: [memory({ content: "Aisle seats", id: "mem-1", version: 1 })],
    });
    api.serverMemoryVersions = { "mem-1": 2 };
    renderApp(api);

    const row = await screen.findByLabelText("Memory: Aisle seats");
    fireEvent.click(within(row).getByRole("button", { name: "Tether" }));

    await waitFor(() => {
      expect(api.tetherMemoryCalls).toEqual([
        { memoryId: "mem-1", version: 1 },
        { memoryId: "mem-1", version: 2 },
      ]);
    });
    expect(
      screen.queryByLabelText("Memory: Aisle seats"),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  test("a failed tether retry reports its own error, not the original 409", async () => {
    const api = new FakeApi({
      authenticated: true,
      memories: [memory({ content: "Aisle seats", id: "mem-1", version: 1 })],
    });
    api.tetherMemoryRejections = [new ApiError(409), new ApiError(500)];
    renderApp(api);

    const row = await screen.findByLabelText("Memory: Aisle seats");
    fireEvent.click(within(row).getByRole("button", { name: "Tether" }));

    await waitFor(() => {
      expect(api.tetherMemoryCalls).toHaveLength(2);
    });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(new ApiError(500).message);
    expect(alert).not.toHaveTextContent(new ApiError(409).message);
  });

  test("rejecting a loose memory calls the API with its version", async () => {
    const api = new FakeApi({
      authenticated: true,
      memories: [memory({ content: "Wrong guess", id: "mem-1", version: 2 })],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Memory: Wrong guess");
    fireEvent.click(within(row).getByRole("button", { name: "Reject" }));

    await waitFor(() => {
      expect(api.rejectMemoryCalls).toEqual([
        { memoryId: "mem-1", version: 2 },
      ]);
    });
    await waitFor(() => {
      expect(
        screen.queryByLabelText("Memory: Wrong guess"),
      ).not.toBeInTheDocument();
    });
  });

  test("rejecting recovers from a stale-version 409 by refetching and retrying", async () => {
    const api = new FakeApi({
      authenticated: true,
      memories: [memory({ content: "Wrong guess", id: "mem-1", version: 1 })],
    });
    api.serverMemoryVersions = { "mem-1": 2 };
    renderApp(api);

    const row = await screen.findByLabelText("Memory: Wrong guess");
    fireEvent.click(within(row).getByRole("button", { name: "Reject" }));

    await waitFor(() => {
      expect(api.rejectMemoryCalls).toEqual([
        { memoryId: "mem-1", version: 1 },
        { memoryId: "mem-1", version: 2 },
      ]);
    });
    expect(
      screen.queryByLabelText("Memory: Wrong guess"),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  test("editing a memory saves the new content at the observed version", async () => {
    const api = new FakeApi({
      authenticated: true,
      memories: [memory({ content: "Aisle seats", id: "mem-1", version: 2 })],
    });
    renderApp(api);

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
      memories: [memory({ content: "Aisle seats", id: "mem-1" })],
    });
    renderApp(api);

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
      memories: [memory({ content: "Aisle seats", id: "mem-1", version: 1 })],
    });
    // The version moved (e.g. the memory was tethered) but the content basis
    // the edit was formulated against is intact, so the retry is safe.
    api.serverMemoryVersions = { "mem-1": 2 };
    renderApp(api);

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
      memories: [memory({ content: "Aisle seats", id: "mem-1", version: 1 })],
    });
    api.serverMemoryVersions = { "mem-1": 2 };
    api.serverMemoryEdits = { "mem-1": { content: "Emergency exit seats" } };
    renderApp(api);

    const row = await screen.findByLabelText("Memory: Aisle seats");
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    fireEvent.input(textarea(screen.getByLabelText("Memory content")), {
      target: { value: "Window seats" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    // No blind retry: one call went out, the editor stays open with the draft,
    // and the conflict is reported.
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

  test("the corpus view lists tethered memories with edit and reject but no tether", async () => {
    const api = new FakeApi({
      authenticated: true,
      memories: [
        memory({ content: "Loose one", id: "mem-1" }),
        memory({
          content: "Trusted fact",
          id: "mem-2",
          state: "tethered",
          tethered_at: "2026-01-06T00:00:00Z",
        }),
      ],
    });
    renderApp(api);

    const panel = await memoriesPanel();
    await screen.findByLabelText("Memory: Loose one");
    fireEvent.click(within(panel).getByRole("button", { name: "Corpus" }));

    const row = await screen.findByLabelText("Memory: Trusted fact");
    expect(row).toHaveTextContent(
      new Date("2026-01-06T00:00:00Z").toLocaleDateString(),
    );
    expect(
      within(row).getByRole("button", { name: "Edit" }),
    ).toBeInTheDocument();
    expect(
      within(row).getByRole("button", { name: "Reject" }),
    ).toBeInTheDocument();
    expect(
      within(row).queryByRole("button", { name: "Tether" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Memory: Loose one"),
    ).not.toBeInTheDocument();
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

    const panel = await memoriesPanel();
    fireEvent.click(within(panel).getByRole("button", { name: "Corpus" }));
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

    const panel = await memoriesPanel();
    fireEvent.click(within(panel).getByRole("button", { name: "Corpus" }));
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

  test("a memories invalidate frame refetches the review queue", async () => {
    const api = new FakeApi({ authenticated: true });
    const bus = renderApp(api);

    await memoriesPanel();
    await waitFor(() => {
      expect(api.listMemoriesCalls).toBeGreaterThan(0);
    });
    const before = api.listMemoriesCalls;
    api.storedMemories = [memory({ content: "Captured by the agent" })];
    bus.emit({ keys: ["memories", "review-queue"], type: "invalidate" });

    await waitFor(() => {
      expect(api.listMemoriesCalls).toBeGreaterThan(before);
    });
    expect(
      await screen.findByLabelText("Memory: Captured by the agent"),
    ).toBeInTheDocument();
  });

  test("empty states name the view they belong to", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    const panel = await memoriesPanel();
    expect(
      await within(panel).findByText("Review queue is clear"),
    ).toBeInTheDocument();
    fireEvent.click(within(panel).getByRole("button", { name: "Corpus" }));
    expect(
      await within(panel).findByText("No tethered memories yet"),
    ).toBeInTheDocument();
  });
});
