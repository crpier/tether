import { cleanup, screen, within } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { FakeHost, navigateTo, renderApp } from "../testing/harness";

afterEach(() => {
  cleanup();
});

describe("Gmail sync panel", () => {
  test("renders the disconnected state and connect button", async () => {
    const host = new FakeHost({ authenticated: true });
    host.gmail.gmailAuthStatus = {
      authorization_url: null,
      error: null,
      state: "disconnected",
    };
    renderApp(host);
    await navigateTo("Settings");

    const section = await screen.findByLabelText("Gmail");
    await within(section).findByText("Google account");
    expect(section).toHaveTextContent("Not connected");
    expect(
      within(section).getByRole("button", { name: "Connect Gmail" }),
    ).toBeInTheDocument();
  });
});
