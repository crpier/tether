import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { FakeHost, navigateTo, renderApp } from "../testing/harness";

afterEach(cleanup);

describe("Settings page", () => {
  test("shows Gmail, YouTube sync status, push toggle and logout", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    await navigateTo("Settings");
    await screen.findByRole("heading", { name: "Settings" });

    expect(
      await screen.findByRole("region", { name: "Gmail" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("region", { name: "YouTube sync" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("region", { name: "Notification delivery" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Log out" })).toBeInTheDocument();
  });

  test("offers Google reconnection for Gmail and YouTube", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    await navigateTo("Settings");

    expect(
      await screen.findByRole("button", { name: "Reconnect YouTube" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Connect Gmail" }),
    ).toBeInTheDocument();
  });

  test("starts YouTube recovery and shows the Google consent link", async () => {
    const host = new FakeHost({ authenticated: true });
    host.youtube.youTubeAuthStatus = {
      authorization_url: null,
      error: null,
      state: "disconnected",
    };
    host.youtube.nextYouTubeAuthStatus = {
      authorization_url: "https://accounts.google.test/consent",
      error: null,
      state: "authorizing",
    };
    renderApp(host);
    await navigateTo("Settings");

    fireEvent.click(
      await screen.findByRole("button", { name: "Connect YouTube" }),
    );

    expect(
      await screen.findByRole("link", { name: "Continue with Google" }),
    ).toHaveAttribute("href", "https://accounts.google.test/consent");
  });

  test("starts Gmail recovery and shows the Google consent link", async () => {
    const host = new FakeHost({ authenticated: true });
    host.gmail.gmailAuthStatus = {
      authorization_url: null,
      error: null,
      state: "disconnected",
    };
    host.gmail.nextGmailAuthStatus = {
      authorization_url: "https://accounts.google.test/gmail-consent",
      error: null,
      state: "authorizing",
    };
    renderApp(host);
    await navigateTo("Settings");

    const gmailPanel = await screen.findByRole("region", { name: "Gmail" });
    fireEvent.click(
      await within(gmailPanel).findByRole("button", { name: "Connect Gmail" }),
    );

    expect(
      await within(gmailPanel).findByRole("link", {
        name: "Continue with Google",
      }),
    ).toHaveAttribute("href", "https://accounts.google.test/gmail-consent");
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
    expect(
      await screen.findByRole("button", { name: "Reconnect OpenAI Codex" }),
    ).toBeInTheDocument();
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
      await screen.findByRole("button", { name: "Connect OpenAI Codex" }),
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
      await screen.findByRole("button", { name: "Connect OpenAI Codex" }),
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
      await screen.findByRole("button", { name: "Connect OpenAI Codex" }),
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
