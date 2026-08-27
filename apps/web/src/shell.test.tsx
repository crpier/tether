import { cleanup, fireEvent, screen, within } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { FakeHost, navigateTo, renderApp } from "./testing/harness";

const noop = () => undefined;
const originalMatchMedia = window.matchMedia;

afterEach(() => {
  cleanup();
  window.matchMedia = originalMatchMedia;
});

function installMatchMedia(matches: boolean) {
  window.matchMedia = (query: string): MediaQueryList => ({
    addEventListener: noop,
    addListener: noop,
    dispatchEvent: () => false,
    matches,
    media: query,
    onchange: null,
    removeEventListener: noop,
    removeListener: noop,
  });
}

async function mainNav(): Promise<HTMLElement> {
  return screen.findByRole("navigation", { name: "Main navigation" });
}

describe("Shell accessibility", () => {
  test("/chat opens chat for authenticated users", async () => {
    renderApp(new FakeHost({ authenticated: true }), undefined, {
      path: "/chat",
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "Tether chat" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { level: 1, name: "Page not found" }),
    ).not.toBeInTheDocument();
  });

  test("the removed Proposals route and navigation item are absent", async () => {
    renderApp(new FakeHost({ authenticated: true }), undefined, {
      path: "/proposals",
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "Page not found" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /^Proposals/ }),
    ).not.toBeInTheDocument();
  });

  test("the removed Inbox route and navigation item are absent", async () => {
    renderApp(new FakeHost({ authenticated: true }), undefined, {
      path: "/inbox",
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "Page not found" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /^Inbox/ }),
    ).not.toBeInTheDocument();
  });

  test("unknown authenticated routes show a not-found state without redirecting", async () => {
    renderApp(new FakeHost({ authenticated: true }), undefined, {
      path: "/not-a-real-route-autoresearch",
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "Page not found" }),
    ).toBeInTheDocument();
    expect(window.location.pathname).toBe("/not-a-real-route-autoresearch");
    expect(
      screen.queryByRole("heading", { name: "Tether chat" }),
    ).not.toBeInTheDocument();
  });

  test("authenticated pages share exactly one shell main landmark", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);

    const headings = {
      Browse: "Browse",
      Chat: "Tether chat",
      Health: "Health",
      Settings: "Settings",
    } as const;

    for (const label of ["Chat", "Health", "Browse", "Settings"] as const) {
      await navigateTo(label);
      expect(
        await screen.findByRole("heading", {
          level: 1,
          name: headings[label],
        }),
      ).toBeInTheDocument();
      expect(screen.getAllByRole("main")).toHaveLength(1);
    }
  });

  test("collapsed desktop navigation keeps full link names and titles", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    const nav = await mainNav();

    fireEvent.click(
      await screen.findByRole("button", { name: "Collapse sidebar" }),
    );

    for (const label of ["Chat", "Health", "Browse", "Settings"]) {
      const link = within(nav).getByRole("link", {
        name: new RegExp(`^${label}`),
      });
      expect(link).toHaveAttribute("title", label);
    }
  });

  test("Browse Todos mounts only the active responsive nav", async () => {
    installMatchMedia(true);
    renderApp(new FakeHost({ authenticated: true }), undefined, {
      path: "/browse",
    });

    const tabs = await screen.findByRole("tablist", { name: "Browse view" });
    fireEvent.click(within(tabs).getByRole("tab", { name: "Todos" }));

    expect(await screen.findByRole("region", { name: "Todos" })).toBeVisible();
    expect(screen.getAllByRole("navigation", { hidden: true })).toHaveLength(1);
  });

  test("only the active responsive nav is exposed", async () => {
    const labels = ["Chat", "Health", "Browse", "Settings"];

    installMatchMedia(true);
    renderApp(new FakeHost({ authenticated: true }), undefined, {
      path: "/settings",
    });
    expect(
      await screen.findByRole("navigation", { name: "Main navigation" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("navigation", { name: "Main navigation (compact)" }),
    ).not.toBeInTheDocument();
    for (const label of labels) {
      expect(
        screen.getAllByRole("link", { name: new RegExp(`^${label}`) }),
      ).toHaveLength(1);
    }

    cleanup();
    installMatchMedia(false);
    renderApp(new FakeHost({ authenticated: true }), undefined, {
      path: "/browse",
    });
    expect(
      screen.queryByRole("navigation", { name: "Main navigation" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("navigation", { name: "Main navigation (compact)" }),
    ).not.toBeInTheDocument();
    fireEvent.click(
      await screen.findByRole("button", { name: "Open sidebar" }),
    );
    expect(
      await screen.findByRole("navigation", { name: "Main navigation" }),
    ).toBeInTheDocument();
    for (const label of labels) {
      expect(
        screen.getAllByRole("link", { name: new RegExp(`^${label}`) }),
      ).toHaveLength(1);
    }
  });
});
