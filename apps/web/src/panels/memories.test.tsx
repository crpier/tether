import { QueryClient, QueryClientProvider } from "@tanstack/solid-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { MemoriesPanel } from "./memories";
import { FakeMemoriesHost } from "../testing/fakes/memories";

afterEach(cleanup);

function topic(title: string, body: string, path: string) {
  return { body, evidence: [], path, title };
}

function renderPanel(host: FakeMemoriesHost) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(() => (
    <QueryClientProvider client={queryClient}>
      <MemoriesPanel api={host} />
    </QueryClientProvider>
  ));
}

describe("MemoriesPanel", () => {
  test("renders Dreaming Topics without edit or review controls", async () => {
    const host = new FakeMemoriesHost([
      topic("Travel preferences", "Prefers aisle seats.", "travel.md"),
    ]);

    renderPanel(host);

    const panel = screen.getByRole("region", { name: "Memory Topics" });
    expect(
      await within(panel).findByLabelText("Memory Topic: Travel preferences"),
    ).toHaveTextContent("Prefers aisle seats.");
    expect(
      screen.queryByRole("button", { name: /edit/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/Memory review/i)).not.toBeInTheDocument();
  });

  test("searches current Topics through the read-only host seam", async () => {
    const host = new FakeMemoriesHost([
      topic("Travel", "Prefers aisle seats.", "travel.md"),
      topic("Food", "Likes curry.", "food.md"),
    ]);
    renderPanel(host);

    fireEvent.input(screen.getByLabelText("Search Memory"), {
      target: { value: "aisle" },
    });

    await waitFor(() => {
      expect(host.listMemoryTopicsCalls).toContain("aisle");
    });
    expect(
      await screen.findByLabelText("Memory Topic: Travel"),
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Memory Topic: Food"),
    ).not.toBeInTheDocument();
  });

  test("surfaces workspace diagnostics", async () => {
    const host = new FakeMemoriesHost();
    host.workspaceDiagnostics = [
      { code: "frontmatter.invalid", message: "Invalid YAML", path: "bad.md" },
    ];

    renderPanel(host);

    expect(
      await screen.findByText("Memory workspace diagnostics"),
    ).toBeInTheDocument();
    expect(screen.getByText("bad.md: Invalid YAML")).toBeInTheDocument();
  });
});
