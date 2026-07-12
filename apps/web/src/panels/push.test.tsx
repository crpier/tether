import { cleanup, fireEvent, screen, waitFor } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { FakeApi, renderApp } from "../testing/harness";

afterEach(cleanup);

describe("Push panel", () => {
  test("enabling push subscribes the browser", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    fireEvent.click(
      await screen.findByRole("button", { name: "Enable notifications" }),
    );

    await waitFor(() => {
      expect(api.subscribeCalls).toHaveLength(1);
    });
    expect(
      await screen.findByRole("button", { name: "Disable notifications" }),
    ).toBeInTheDocument();
  });
});
