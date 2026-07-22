import { cleanup, fireEvent, screen, waitFor } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { FakeApi, navigateTo, renderApp } from "../testing/harness";

afterEach(cleanup);

describe("Settings page", () => {
  test("shows YouTube sync status, push toggle and logout", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);
    await navigateTo("Settings");
    await screen.findByRole("heading", { name: "Settings" });

    expect(
      await screen.findByRole("region", { name: "YouTube sync" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("region", { name: "Notification delivery" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Log out" })).toBeInTheDocument();
  });

  test("logging out clears the session", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);
    await navigateTo("Settings");
    await screen.findByRole("heading", { name: "Settings" });

    fireEvent.click(await screen.findByRole("button", { name: "Log out" }));

    await waitFor(() => {
      expect(api.authenticated).toBe(false);
    });
    expect(
      await screen.findByRole("heading", { name: "Sign in to Tether" }),
    ).toBeInTheDocument();
  });
});
