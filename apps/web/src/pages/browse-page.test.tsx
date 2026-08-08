import { cleanup, fireEvent, screen, within } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { FakeHost, navigateTo, renderApp } from "../testing/harness";

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
