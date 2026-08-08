import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import {
  FakeHost,
  memory,
  navigateTo,
  proposal,
  renderApp,
} from "./testing/harness";

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

// The nav link's icon glyph ("P") is also in `textContent` (only its
// accessible name hides it via aria-hidden), so badge presence is asserted
// against the trailing digit rather than the link's full text content.
function badgeDigit(link: HTMLElement): string | null {
  const match = /(\d+)$/.exec(link.textContent);
  return match ? match[1] : null;
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
      Inbox: "Inbox",
      Proposals: "Proposals",
      Settings: "Settings",
    } as const;

    for (const label of [
      "Chat",
      "Proposals",
      "Inbox",
      "Browse",
      "Settings",
    ] as const) {
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

    for (const label of ["Chat", "Proposals", "Inbox", "Browse", "Settings"]) {
      const link = within(nav).getByRole("link", {
        name: new RegExp(`^${label}`),
      });
      expect(link).toHaveAttribute("title", label);
    }
  });

  test("only the active responsive nav is exposed", async () => {
    const labels = ["Chat", "Proposals", "Inbox", "Browse", "Settings"];

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
      await screen.findByRole("navigation", {
        name: "Main navigation (compact)",
      }),
    ).toBeInTheDocument();
    for (const label of labels) {
      expect(
        screen.getAllByRole("link", { name: new RegExp(`^${label}`) }),
      ).toHaveLength(1);
    }
  });
});

describe("Shell nav badges", () => {
  test("no badge renders when a page has nothing pending", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    const nav = await mainNav();

    await waitFor(() => {
      expect(host.proposals.listProposalsCalls.length).toBeGreaterThan(0);
    });
    const proposalsLink = within(nav).getByRole("link", {
      name: /^Proposals/,
    });
    expect(badgeDigit(proposalsLink)).toBeNull();
  });

  test("badges reflect pending proposals and inbox items", async () => {
    const host = new FakeHost({
      authenticated: true,
      memories: [memory({ content: "Prefers aisle seats" })],
      proposals: [proposal({ id: "prop-1" })],
    });
    renderApp(host);
    const nav = await mainNav();

    await waitFor(() => {
      const proposalsLink = within(nav).getByRole("link", {
        name: /^Proposals/,
      });
      expect(badgeDigit(proposalsLink)).toBe("1");
    });
    await waitFor(() => {
      const inboxLink = within(nav).getByRole("link", { name: /^Inbox/ });
      expect(badgeDigit(inboxLink)).toBe("1");
    });
  });

  test("a badge updates on a bus invalidate frame", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);
    const nav = await mainNav();

    await waitFor(() => {
      const proposalsLink = within(nav).getByRole("link", {
        name: /^Proposals/,
      });
      expect(badgeDigit(proposalsLink)).toBeNull();
    });

    host.proposals.storedProposals = [proposal({ id: "prop-1" })];
    bus.emit({ keys: ["proposals"], type: "invalidate" });

    await waitFor(() => {
      const proposalsLink = within(nav).getByRole("link", {
        name: /^Proposals/,
      });
      expect(badgeDigit(proposalsLink)).toBe("1");
    });
  });
});
