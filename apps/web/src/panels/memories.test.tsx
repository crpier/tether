import { QueryClient, QueryClientProvider } from "@tanstack/solid-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test, vi } from "vitest";

import { MemoriesPanel } from "./memories";
import { FakeMemoriesHost } from "../testing/fakes/memories";

afterEach(cleanup);

function topic(
  title: string,
  body: string,
  path: string,
  evidence: string[] = [],
) {
  return { body, evidence, path, title };
}

function renderPanel(
  host: FakeMemoriesHost,
  onOpenEvidence: (uri: string) => void = () => undefined,
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(() => (
    <QueryClientProvider client={queryClient}>
      <MemoriesPanel api={host} onOpenEvidence={onOpenEvidence} />
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

  test("keeps bulk provenance collapsed while Claim citations stay inspectable", async () => {
    const onOpenEvidence = vi.fn();
    const cited =
      "tether://health-connect/sleep/6867bb61-b4cd-3590-a8b1-c4678bd3bf27@v214";
    const supporting = "tether://message/019f0000-0000-7000-8000-000000000001";
    const host = new FakeMemoriesHost([
      topic(
        "Sleep",
        `## Pattern\n\n- Sleep varied. [source](${cited})`,
        "sleep.md",
        [cited, supporting],
      ),
    ]);

    renderPanel(host, onOpenEvidence);

    const disclosure = await screen.findByText("2 Evidence sources");
    expect(disclosure.closest("details")).not.toHaveAttribute("open");
    expect(screen.getByText(supporting)).not.toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "source" }));
    expect(onOpenEvidence).toHaveBeenCalledWith(cited);
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
