import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import {
  FakeApi,
  grant,
  grantSuggestion,
  navigateTo,
  proposal,
  renderApp,
} from "../testing/harness";

afterEach(cleanup);

describe("Proposals page", () => {
  test("lists pending proposals master-detail and shows the selected detail", async () => {
    const api = new FakeApi({
      authenticated: true,
      proposals: [
        proposal({ id: "prop-1", title: "Purge 42 promotional emails" }),
      ],
    });
    renderApp(api);
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
    const api = new FakeApi({
      authenticated: true,
      proposals: [proposal({ id: "prop-1", title: "Purge emails" })],
    });
    renderApp(api);
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
      expect(api.approveProposalCalls).toEqual([
        { deselectedActionIds: [], proposalId: "prop-1", version: 1 },
      ]);
    });
  });

  test("switching to Grants shows active grants", async () => {
    const api = new FakeApi({
      authenticated: true,
      grants: [grant({ id: "grant-1", kind: "send_email" })],
    });
    renderApp(api);
    await navigateTo("Proposals");
    await screen.findByRole("heading", { name: "Proposals" });

    fireEvent.click(await screen.findByRole("tab", { name: /Grants/ }));

    expect(
      await screen.findByLabelText("Grant: send_email"),
    ).toBeInTheDocument();
  });

  test("view switcher exposes tabs and selected panel state", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);
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

  test("large decided history is counted, paged, searchable, and preserved", async () => {
    const decided = Array.from({ length: 40 }, (_, index) =>
      proposal({
        decided_at: `2026-01-${(index + 1).toString().padStart(2, "0")}T00:00:00Z`,
        id: `history-${index.toString().padStart(2, "0")}`,
        state: index % 2 === 0 ? "approved" : "rejected",
        title: `Decision ${index.toString().padStart(2, "0")}`,
      }),
    );
    const api = new FakeApi({ authenticated: true, proposals: decided });
    renderApp(api);
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
    const api = new FakeApi({
      authenticated: true,
      grantSuggestions: suggestions,
    });
    renderApp(api);
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
