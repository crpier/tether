import { cleanup, screen, waitFor, within } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { FakeApi, memory, proposal, renderApp } from "./testing/harness";

afterEach(cleanup);

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

describe("Shell nav badges", () => {
  test("no badge renders when a page has nothing pending", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);
    const nav = await mainNav();

    await waitFor(() => {
      expect(api.listProposalsCalls.length).toBeGreaterThan(0);
    });
    const proposalsLink = within(nav).getByRole("link", {
      name: /^Proposals/,
    });
    expect(badgeDigit(proposalsLink)).toBeNull();
  });

  test("badges reflect pending proposals and inbox items", async () => {
    const api = new FakeApi({
      authenticated: true,
      memories: [memory({ content: "Prefers aisle seats" })],
      proposals: [proposal({ id: "prop-1" })],
    });
    renderApp(api);
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
    const api = new FakeApi({ authenticated: true });
    const bus = renderApp(api);
    const nav = await mainNav();

    await waitFor(() => {
      const proposalsLink = within(nav).getByRole("link", {
        name: /^Proposals/,
      });
      expect(badgeDigit(proposalsLink)).toBeNull();
    });

    api.storedProposals = [proposal({ id: "prop-1" })];
    bus.emit({ keys: ["proposals"], type: "invalidate" });

    await waitFor(() => {
      const proposalsLink = within(nav).getByRole("link", {
        name: /^Proposals/,
      });
      expect(badgeDigit(proposalsLink)).toBe("1");
    });
  });
});
