import { cleanup, fireEvent, screen, waitFor } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { FakeHost, navigateTo, renderApp } from "../testing/harness";

afterEach(cleanup);

describe("Settings page", () => {
  test("shows YouTube sync status, push toggle and logout", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
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

  test("shows the server-owned model provider connection", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    await navigateTo("Settings");

    const provider = await screen.findByRole("region", {
      name: "Model provider",
    });

    await waitFor(() => {
      expect(provider).toHaveTextContent("OpenAI Codex");
      expect(provider).toHaveTextContent("Connected");
    });
  });

  test("starts device recovery and shows the OpenAI code", async () => {
    const host = new FakeHost({ authenticated: true });
    host.providerAuth.providerAuthStatus = {
      error: null,
      expires_in_seconds: null,
      state: "disconnected",
      user_code: null,
      verification_uri: null,
    };
    host.providerAuth.nextProviderAuthStatus = {
      error: null,
      expires_in_seconds: 900,
      state: "authorizing",
      user_code: "ABCD-EFGH",
      verification_uri: "https://auth.openai.com/codex/device",
    };
    renderApp(host);
    await navigateTo("Settings");

    fireEvent.click(
      await screen.findByRole("button", { name: "Connect ChatGPT" }),
    );

    expect(await screen.findByText("ABCD-EFGH")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Open OpenAI sign-in" }),
    ).toHaveAttribute("href", "https://auth.openai.com/codex/device");
  });

  test("polls an active recovery until the server credential connects", async () => {
    const host = new FakeHost({ authenticated: true });
    host.providerAuth.providerAuthStatus = {
      error: null,
      expires_in_seconds: null,
      state: "disconnected",
      user_code: null,
      verification_uri: null,
    };
    host.providerAuth.nextProviderAuthStatus = {
      error: null,
      expires_in_seconds: 900,
      state: "authorizing",
      user_code: "ABCD-EFGH",
      verification_uri: "https://auth.openai.com/codex/device",
    };
    renderApp(host);
    await navigateTo("Settings");
    fireEvent.click(
      await screen.findByRole("button", { name: "Connect ChatGPT" }),
    );
    await screen.findByText("ABCD-EFGH");

    host.providerAuth.providerAuthStatus = {
      error: null,
      expires_in_seconds: null,
      state: "connected",
      user_code: null,
      verification_uri: null,
    };

    expect(await screen.findByText("Connected")).toBeInTheDocument();
  });

  test("cancels an active provider recovery", async () => {
    const host = new FakeHost({ authenticated: true });
    host.providerAuth.providerAuthStatus = {
      error: null,
      expires_in_seconds: 900,
      state: "authorizing",
      user_code: "ABCD-EFGH",
      verification_uri: "https://auth.openai.com/codex/device",
    };
    renderApp(host);
    await navigateTo("Settings");

    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(
      await screen.findByRole("button", { name: "Connect ChatGPT" }),
    ).toBeInTheDocument();
    expect(host.providerAuth.cancelProviderAuthCalls).toBe(1);
  });

  test("logging out clears the session", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    await navigateTo("Settings");
    await screen.findByRole("heading", { name: "Settings" });

    fireEvent.click(await screen.findByRole("button", { name: "Log out" }));

    await waitFor(() => {
      expect(host.auth.authenticated).toBe(false);
    });
    expect(
      await screen.findByRole("heading", { name: "Sign in to Tether" }),
    ).toBeInTheDocument();
  });
});
