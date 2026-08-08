import { cleanup, fireEvent, screen, within } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import {
  FakeHost,
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
