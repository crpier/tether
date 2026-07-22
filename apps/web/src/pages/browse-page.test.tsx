import { cleanup, fireEvent, screen } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { FakeApi, navigateTo, renderApp } from "../testing/harness";

afterEach(cleanup);

describe("Browse page", () => {
  test("opens on the memory corpus, not the review queue", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);
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
    const api = new FakeApi({ authenticated: true });
    renderApp(api);
    await navigateTo("Browse");
    await screen.findByRole("heading", { name: "Browse" });

    fireEvent.click(await screen.findByRole("button", { name: "Todos" }));
    expect(
      await screen.findByRole("region", { name: "Todos" }),
    ).toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));
    expect(
      await screen.findByRole("region", { name: "Reminders" }),
    ).toBeInTheDocument();
  });
});
