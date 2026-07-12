import { cleanup, fireEvent, screen } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { FakeApi, input, renderApp } from "./testing/harness";

afterEach(cleanup);

describe("Login screen", () => {
  test("unauthenticated users log in before seeing chat", async () => {
    const api = new FakeApi({ authenticated: false });
    renderApp(api);

    expect(
      await screen.findByRole("heading", { name: "Sign in to Tether" }),
    ).toBeInTheDocument();

    fireEvent.input(input(screen.getByLabelText("Password")), {
      target: { value: "correct horse battery staple" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Log in" }));

    expect(
      await screen.findByRole("heading", { name: "Tether chat" }),
    ).toBeInTheDocument();
    expect(api.loginPassword).toBe("correct horse battery staple");
  });
});
