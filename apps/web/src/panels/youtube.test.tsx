import { cleanup, screen } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { FakeHost, navigateTo, renderApp } from "../testing/harness";

afterEach(() => {
  cleanup();
});

describe("YouTube sync panel", () => {
  test("renders the daily quota", async () => {
    const host = new FakeHost({ authenticated: true });
    host.youtube.youTubeSyncStatus = {
      ...host.youtube.youTubeSyncStatus,
      quota: { limit: 10000, remaining: 9994, used: 6 },
    };
    renderApp(host);
    await navigateTo("Settings");

    const section = await screen.findByLabelText("YouTube sync");
    await screen.findByText("Daily quota");
    expect(section).toHaveTextContent("6 / 10000");
  });
});
