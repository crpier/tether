import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import {
  createBusHarness,
  FakeHost,
  grant,
  grantSuggestion,
  navigateTo,
  proposal,
  renderApp,
} from "../testing/harness";

afterEach(cleanup);

describe("Proposals page", () => {
  test("lists pending proposals master-detail and shows the selected detail", async () => {
    const host = new FakeHost({
      authenticated: true,
      proposals: [
        proposal({ id: "prop-1", title: "Purge 42 promotional emails" }),
      ],
    });
    renderApp(host);
    await navigateTo("Proposals");
    await screen.findByRole("heading", { name: "Proposals" });

    await waitFor(() => {
      expect(
        screen.getAllByRole("button", { name: /Purge 42 promotional emails/ })
          .length,
      ).toBeGreaterThan(0);
    });
    fireEvent.click(
      screen.getAllByRole("button", {
        name: /Purge 42 promotional emails/,
      })[0],
    );

    await waitFor(() => {
      const detail = screen.getAllByLabelText(
        "Proposal: Purge 42 promotional emails",
      )[0];
      expect(detail).toHaveTextContent("Purge old promotional emails");
    });
  });

  test("approving a proposal from the detail pane calls the API", async () => {
    const host = new FakeHost({
      authenticated: true,
      proposals: [proposal({ id: "prop-1", title: "Purge emails" })],
    });
    renderApp(host);
    await navigateTo("Proposals");
    await screen.findByRole("heading", { name: "Proposals" });

    await waitFor(() => {
      expect(
        screen.getAllByRole("button", { name: /Purge emails/ }).length,
      ).toBeGreaterThan(0);
    });
    fireEvent.click(screen.getAllByRole("button", { name: /Purge emails/ })[0]);
    let detail: HTMLElement | undefined;
    await waitFor(() => {
      detail = screen.getAllByLabelText("Proposal: Purge emails")[0];
      expect(detail).toBeInTheDocument();
    });
    fireEvent.click(within(detail!).getByRole("button", { name: /^Approve/ }));

    await waitFor(() => {
      expect(host.proposals.approveProposalCalls).toEqual([
        { deselectedActionIds: [], proposalId: "prop-1", version: 1 },
      ]);
    });
  });

  test("preloads grants tab count before Grants is opened", async () => {
    const host = new FakeHost({
      authenticated: true,
      grants: [grant({ id: "grant-1", kind: "send_email" })],
      grantSuggestions: [grantSuggestion({ kind: "archive_email" })],
    });
    renderApp(host);
    await navigateTo("Proposals");
    await screen.findByRole("heading", { name: "Proposals" });

    expect(
      await screen.findByRole("tab", { name: "Grants (2)" }),
    ).toBeInTheDocument();
  });

  test("switching to Grants shows active grants", async () => {
    const host = new FakeHost({
      authenticated: true,
      grants: [grant({ id: "grant-1", kind: "send_email" })],
    });
    renderApp(host);
    await navigateTo("Proposals");
    await screen.findByRole("heading", { name: "Proposals" });

    fireEvent.click(await screen.findByRole("tab", { name: /Grants/ }));

    expect(
      await screen.findByLabelText("Grant: send_email"),
    ).toBeInTheDocument();
  });

  test("honors proposals tab and page query params", async () => {
    const grants = Array.from({ length: 40 }, (_, index) =>
      grant({
        id: `grant-${index.toString()}`,
        kind: `grant-${index.toString()}`,
      }),
    );
    const host = new FakeHost({ authenticated: true, grants });
    renderApp(host, createBusHarness(), {
      path: "/proposals?tab=grants&page=2",
    });
    await screen.findByRole("heading", { name: "Proposals" });

    expect(await screen.findByRole("tab", { name: /Grants/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    const panel = screen.getByRole("tabpanel", { name: /Grants/ });
    expect(
      await within(panel).findByLabelText("Grant: grant-25"),
    ).toBeInTheDocument();
    expect(within(panel).getByText("Page 2 of 2")).toBeInTheDocument();
    expect(window.location.pathname + window.location.search).toBe(
      "/proposals?tab=grants&page=2",
    );
  });

  test("normalizes out-of-range proposals page query params", async () => {
    const grants = Array.from({ length: 40 }, (_, index) =>
      grant({
        id: `grant-${index.toString()}`,
        kind: `grant-${index.toString()}`,
      }),
    );
    const host = new FakeHost({ authenticated: true, grants });
    renderApp(host, createBusHarness(), {
      path: "/proposals?tab=grants&page=8",
    });
    await screen.findByRole("heading", { name: "Proposals" });

    const panel = screen.getByRole("tabpanel", { name: /Grants/ });
    expect(await within(panel).findByText("Page 2 of 2")).toBeInTheDocument();
    await waitFor(() => {
      expect(window.location.pathname + window.location.search).toBe(
        "/proposals?tab=grants&page=2",
      );
    });
  });

  test("view switcher exposes tabs and selected panel state", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    await navigateTo("Proposals");
    await screen.findByRole("heading", { name: "Proposals" });

    const tabs = screen.getByRole("tablist", { name: "Proposals view" });
    expect(within(tabs).getByRole("tab", { name: /Queue/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    fireEvent.keyDown(within(tabs).getByRole("tab", { name: /Queue/ }), {
      key: "ArrowRight",
    });

    expect(within(tabs).getByRole("tab", { name: /Decided/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(
      screen.getByRole("tabpanel", { name: /Decided/ }),
    ).toBeInTheDocument();
  });

  test("preloads decided proposals count from the all-proposals query", async () => {
    const host = new FakeHost({
      authenticated: true,
      proposals: [
        proposal({
          id: "decision-1",
          state: "approved",
          title: "Already approved",
          decided_at: "2026-01-01T00:00:00Z",
        }),
      ],
    });
    renderApp(host);
    await navigateTo("Proposals");
    await screen.findByRole("heading", { name: "Proposals" });

    fireEvent.click(await screen.findByRole("tab", { name: /Decided/ }));

    expect(
      await screen.findByRole("heading", { name: "Decided proposals (1)" }),
    ).toBeInTheDocument();
  });

  test("large decided history is counted, paged, searchable, and preserved", async () => {
    const decided = Array.from({ length: 40 }, (_, index) =>
      proposal({
        decided_at: `2026-01-${(index + 1).toString().padStart(2, "0")}T00:00:00Z`,
        id: `history-${index.toString().padStart(2, "0")}`,
        state: index % 2 === 0 ? "approved" : "rejected",
        title: `Decision ${index.toString().padStart(2, "0")}`,
      }),
    );
    const host = new FakeHost({ authenticated: true, proposals: decided });
    renderApp(host);
    await navigateTo("Proposals");
    await screen.findByRole("heading", { name: "Proposals" });

    fireEvent.click(await screen.findByRole("tab", { name: /Decided/ }));

    expect(
      await screen.findByRole("heading", { name: "Decided proposals (40)" }),
    ).toBeInTheDocument();
    const panel = screen.getByRole("tabpanel", { name: /Decided/ });
    await waitFor(() => {
      expect(within(panel).getAllByRole("listitem")).toHaveLength(25);
    });

    fireEvent.input(within(panel).getByLabelText("Search decided proposals"), {
      target: { value: "Decision 36" },
    });

    expect(await within(panel).findByText("Decision 36")).toBeInTheDocument();
    expect(within(panel).queryByText("Decision 35")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /Queue/ }));
    fireEvent.click(screen.getByRole("tab", { name: /Decided/ }));
    const restoredPanel = screen.getByRole("tabpanel", { name: /Decided/ });
    expect(
      within(restoredPanel).getByDisplayValue("Decision 36"),
    ).toBeInTheDocument();
  });

  test("large grant suggestions are counted, paged, searchable, and uniquely actionable", async () => {
    const suggestions = Array.from({ length: 35 }, (_, index) =>
      grantSuggestion({
        approved: index,
        kind: `operation-${index.toString().padStart(2, "0")}`,
        scope: `scope-${index.toString().padStart(2, "0")}`,
        seen: index + 1,
      }),
    );
    const host = new FakeHost({
      authenticated: true,
      grantSuggestions: suggestions,
    });
    renderApp(host);
    await navigateTo("Proposals");
    await screen.findByRole("heading", { name: "Proposals" });

    fireEvent.click(await screen.findByRole("tab", { name: /Grants/ }));

    expect(
      await screen.findByRole("heading", { name: "Suggestions (35)" }),
    ).toBeInTheDocument();
    const panel = screen.getByRole("tabpanel", { name: /Grants/ });
    await waitFor(() => {
      expect(
        within(panel).getAllByRole("button", { name: /^Grant operation-/ }),
      ).toHaveLength(25);
    });

    fireEvent.input(within(panel).getByLabelText("Search grant suggestions"), {
      target: { value: "scope-30" },
    });

    const grantButton = await within(panel).findByRole("button", {
      name: "Grant operation-30 for scope-30",
    });
    expect(grantButton).toHaveTextContent("Grant");
  });
});
