import { cleanup, fireEvent, screen } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { FakeHost, input, renderApp } from "./testing/harness";

afterEach(cleanup);

describe("Login screen", () => {
  test("unauthenticated users log in before seeing chat", async () => {
    const host = new FakeHost({ authenticated: false });
    renderApp(host);

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
    expect(host.auth.loginPassword).toBe("correct horse battery staple");
  });
});
