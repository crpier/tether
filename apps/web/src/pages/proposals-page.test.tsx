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
  proposalAction,
  renderApp,
} from "../testing/harness";
import { formatDateTime } from "../lib/format";

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

  test("direct queue proposals route opens the queue tab", async () => {
    const host = new FakeHost({
      authenticated: true,
      proposals: [proposal({ id: "prop-1", title: "Needs approval" })],
    });
    renderApp(host, createBusHarness(), { path: "/proposals/queue" });
    await screen.findByRole("heading", { name: "Proposals" });

    expect(await screen.findByRole("tab", { name: /Queue/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(await screen.findByText("Needs approval")).toBeInTheDocument();
  });

  test("direct decided proposals route opens the decided tab", async () => {
    const host = new FakeHost({
      authenticated: true,
      proposals: [
        proposal({
          decided_at: "2026-01-01T00:00:00Z",
          id: "decision-1",
          state: "approved",
          title: "Already approved",
        }),
      ],
    });
    renderApp(host, createBusHarness(), { path: "/proposals/decided" });
    await screen.findByRole("heading", { name: "Proposals" });

    expect(await screen.findByRole("tab", { name: /Decided/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(
      await screen.findByRole("heading", { name: "Decided proposals (1)" }),
    ).toBeInTheDocument();
  });

  test("direct grants proposals route opens the grants tab", async () => {
    const host = new FakeHost({
      authenticated: true,
      grants: [grant({ id: "grant-1", kind: "send_email" })],
    });
    renderApp(host, createBusHarness(), { path: "/proposals/grants" });
    await screen.findByRole("heading", { name: "Proposals" });

    expect(await screen.findByRole("tab", { name: /Grants/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
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

  test("decided proposal search reveals matching action text", async () => {
    const host = new FakeHost({
      authenticated: true,
      proposals: [
        proposal({
          actions: [
            proposalAction({
              display: "Archive two warranty emails",
              id: "warranty-display-action",
              kind: "gmail.archive",
              scope: "warranty",
            }),
          ],
          decided_at: "2026-07-23T20:30:45Z",
          id: "decision-warranty-display-match",
          rejection_reason: "Bulk rejected",
          state: "rejected",
          title: "Inbox hygiene",
        }),
        proposal({
          actions: [
            proposalAction({
              display: null,
              id: "warranty-category-action",
              kind: "gmail.delete",
              scope: "warranty",
            }),
          ],
          decided_at: "2026-07-22T20:30:45Z",
          id: "decision-warranty-category-match",
          rejection_reason: "Bulk rejected",
          state: "rejected",
          title: "Inbox hygiene",
        }),
      ],
    });
    renderApp(host);
    await navigateTo("Proposals");
    await screen.findByRole("heading", { name: "Proposals" });
    fireEvent.click(await screen.findByRole("tab", { name: /Decided/ }));

    const panel = screen.getByRole("tabpanel", { name: /Decided/ });
    await within(panel).findAllByLabelText("Proposal: Inbox hygiene");
    fireEvent.input(within(panel).getByLabelText("Search decided proposals"), {
      target: { value: "warranty" },
    });

    expect(
      await within(panel).findByText(
        "Matched action: Archive two warranty emails · gmail.archive · warranty",
      ),
    ).toBeInTheDocument();
    expect(
      await within(panel).findByText("Matched action: gmail.delete · warranty"),
    ).toBeInTheDocument();
  });

  test("decided proposal search ignores hidden summary action counts", async () => {
    const host = new FakeHost({
      authenticated: true,
      proposals: [
        proposal({
          actions: Array.from({ length: 4 }, (_, index) =>
            proposalAction({ id: `match-action-${index.toString()}` }),
          ),
          decided_at: "2026-07-23T20:30:45Z",
          id: "decision-action-count-match",
          state: "approved",
          summary: "Inbox hygiene: 2 gmail.archive, 2 gmail.delete.",
          title: "Inbox hygiene: 4 actions",
        }),
        proposal({
          actions: Array.from({ length: 8 }, (_, index) =>
            proposalAction({ id: `hidden-action-${index.toString()}` }),
          ),
          decided_at: "2026-07-22T20:30:45Z",
          id: "decision-hidden-count-match",
          state: "approved",
          summary: "Inbox hygiene: 4 gmail.archive, 4 gmail.delete.",
          title: "Inbox hygiene: 8 actions",
        }),
      ],
    });
    renderApp(host);
    await navigateTo("Proposals");
    await screen.findByRole("heading", { name: "Proposals" });
    fireEvent.click(await screen.findByRole("tab", { name: /Decided/ }));

    const panel = screen.getByRole("tabpanel", { name: /Decided/ });
    await within(panel).findByLabelText("Proposal: Inbox hygiene: 4 actions");
    fireEvent.input(within(panel).getByLabelText("Search decided proposals"), {
      target: { value: "Inbox hygiene: 4" },
    });

    expect(
      await within(panel).findByLabelText("Proposal: Inbox hygiene: 4 actions"),
    ).toBeInTheDocument();
    expect(
      within(panel).queryByLabelText("Proposal: Inbox hygiene: 8 actions"),
    ).not.toBeInTheDocument();
  });

  test("decided proposal search leaves approved-state filtering to state control", async () => {
    const host = new FakeHost({
      authenticated: true,
      proposals: [
        proposal({
          decided_at: "2026-07-23T20:30:45Z",
          id: "decision-approved-state-only",
          state: "approved",
          title: "State-only decision",
        }),
        proposal({
          decided_at: "2026-07-22T20:30:45Z",
          id: "decision-rejected-state-only",
          state: "rejected",
          title: "Other state-only decision",
        }),
      ],
    });
    renderApp(host);
    await navigateTo("Proposals");
    await screen.findByRole("heading", { name: "Proposals" });
    fireEvent.click(await screen.findByRole("tab", { name: /Decided/ }));

    const panel = screen.getByRole("tabpanel", { name: /Decided/ });
    await within(panel).findByLabelText("Proposal: State-only decision");
    fireEvent.input(within(panel).getByLabelText("Search decided proposals"), {
      target: { value: "approved" },
    });

    expect(
      within(panel).queryByLabelText("Proposal: State-only decision"),
    ).not.toBeInTheDocument();
    expect(
      within(panel).getByText("No decided proposals match."),
    ).toBeInTheDocument();

    fireEvent.input(within(panel).getByLabelText("Search decided proposals"), {
      target: { value: "" },
    });
    fireEvent.change(
      within(panel).getByLabelText("Filter decided proposals by state"),
      { target: { value: "approved" } },
    );

    expect(
      await within(panel).findByLabelText("Proposal: State-only decision"),
    ).toBeInTheDocument();
    expect(
      within(panel).queryByLabelText("Proposal: Other state-only decision"),
    ).not.toBeInTheDocument();
  });

  test("decided proposal search matches the visible decided timestamp", async () => {
    const decidedAt = "2026-07-22T20:30:45Z";
    const host = new FakeHost({
      authenticated: true,
      proposals: [
        proposal({
          decided_at: decidedAt,
          id: "decision-time-match",
          state: "approved",
          title: "Timestamp-only match",
        }),
        proposal({
          decided_at: "2026-07-22T19:00:00Z",
          id: "decision-no-match",
          state: "rejected",
          title: "Other decision",
        }),
      ],
    });
    renderApp(host);
    await navigateTo("Proposals");
    await screen.findByRole("heading", { name: "Proposals" });
    fireEvent.click(await screen.findByRole("tab", { name: /Decided/ }));

    const panel = screen.getByRole("tabpanel", { name: /Decided/ });
    await within(panel).findByText(formatDateTime(new Date(decidedAt)));
    fireEvent.input(within(panel).getByLabelText("Search decided proposals"), {
      target: { value: formatDateTime(new Date(decidedAt)) },
    });

    expect(
      await within(panel).findByLabelText("Proposal: Timestamp-only match"),
    ).toBeInTheDocument();
    expect(
      within(panel).queryByLabelText("Proposal: Other decision"),
    ).not.toBeInTheDocument();
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
