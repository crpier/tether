import { cleanup, screen } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { FakeHost, navigateTo, renderApp } from "../testing/harness";

afterEach(() => {
  cleanup();
});

describe("YouTube sync panel", () => {
  test("renders the daily quota but no per-source usage line when none is configured", async () => {
    const host = new FakeHost({ authenticated: true });
    host.youtube.youTubeSyncStatus = {
      ...host.youtube.youTubeSyncStatus,
      quota: { limit: 10000, remaining: 9994, used: 6 },
      usage: {},
    };
    renderApp(host);
    await navigateTo("Settings");

    const section = await screen.findByLabelText("YouTube sync");
    await screen.findByText("Daily quota");
    expect(section).toHaveTextContent("6 / 10000");
    expect(section).not.toHaveTextContent("Supadata");
  });

  test("renders a separate Supadata monthly usage line when configured", async () => {
    const host = new FakeHost({ authenticated: true });
    host.youtube.youTubeSyncStatus = {
      ...host.youtube.youTubeSyncStatus,
      quota: { limit: 10000, remaining: 10000, used: 0 },
      usage: {
        supadata: { limit: 3000, period: "2026-07", remaining: 2979, used: 21 },
      },
    };
    renderApp(host);
    await navigateTo("Settings");

    const section = await screen.findByLabelText("YouTube sync");
    // The daily quota and the Supadata monthly usage are distinct numbers —
    // mixing them together is exactly the bug this line fixes.
    await screen.findByText("Daily quota");
    expect(section).toHaveTextContent("0 / 10000");
    expect(section).toHaveTextContent("Supadata (monthly)");
    expect(section).toHaveTextContent("21 / 3000");
  });

  test("renders a generic usage line for a non-Supadata metered source", async () => {
    const host = new FakeHost({ authenticated: true });
    host.youtube.youTubeSyncStatus = {
      ...host.youtube.youTubeSyncStatus,
      quota: { limit: 10000, remaining: 10000, used: 0 },
      usage: { widget: { limit: 5, period: "", remaining: 3, used: 2 } },
    };
    renderApp(host);
    await navigateTo("Settings");

    const section = await screen.findByLabelText("YouTube sync");
    await screen.findByText("Daily quota");
    expect(section).toHaveTextContent("widget usage");
    expect(section).toHaveTextContent("2 / 5");
  });
});
