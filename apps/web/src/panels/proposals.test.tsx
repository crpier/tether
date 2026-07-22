import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { ApiError } from "../api";
import {
  FakeApi,
  grant,
  grantSuggestion,
  input,
  proposal,
  proposalAction,
  renderApp,
} from "../testing/harness";

afterEach(cleanup);

async function proposalsPanel(): Promise<HTMLElement> {
  return screen.findByRole("region", { name: "Proposals" });
}

describe("Proposals panel", () => {
  test("the queue lists pending proposals with consumer and action count", async () => {
    const api = new FakeApi({
      authenticated: true,
      proposals: [
        proposal({
          actions: [proposalAction({}), proposalAction({})],
          consumer: "gmail-purge",
          title: "Purge 2 promotional emails",
        }),
      ],
    });
    renderApp(api);

    const row = await screen.findByLabelText(
      "Proposal: Purge 2 promotional emails",
    );
    expect(row).toHaveTextContent("gmail-purge");
    expect(row).toHaveTextContent("2 actions");
  });

  test("expanding a proposal shows each action's kind, scope, and params", async () => {
    const api = new FakeApi({
      authenticated: true,
      proposals: [
        proposal({
          actions: [
            proposalAction({
              kind: "send_email",
              params: { to: "a@example.com" },
              scope: "inbox",
            }),
          ],
          title: "Send a follow-up",
        }),
      ],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Proposal: Send a follow-up");
    fireEvent.click(within(row).getByRole("button", { expanded: false }));

    expect(row).toHaveTextContent("send_email");
    expect(row).toHaveTextContent("inbox");
    expect(row).toHaveTextContent("a@example.com");
  });

  test("an action's display line is shown and its raw params are collapsed behind details", async () => {
    const api = new FakeApi({
      authenticated: true,
      proposals: [
        proposal({
          actions: [
            proposalAction({
              display: 'Archive · "Your order has shipped" · Amazon · Jul 12',
              kind: "gmail.archive",
              params: { message_id: "19759365d5434140" },
              scope: "sender-category:receipts",
            }),
          ],
          title: "Inbox hygiene",
        }),
      ],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Proposal: Inbox hygiene");
    fireEvent.click(within(row).getByRole("button", { expanded: false }));

    expect(row).toHaveTextContent(
      'Archive · "Your order has shipped" · Amazon · Jul 12',
    );
    // The opaque id is not in the primary text; it lives behind the collapsed
    // details disclosure (closed by default).
    const details = within(row).getByText("Details").closest("details");
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute("open");
    expect(details).toHaveTextContent("19759365d5434140");
  });

  test("an action without a display falls back to kind and scope, not raw JSON", async () => {
    const api = new FakeApi({
      authenticated: true,
      proposals: [
        proposal({
          actions: [
            proposalAction({
              display: null,
              kind: "gmail.label",
              params: {
                label_name: "Receipts",
                message_id: "19759365d5434140",
              },
              scope: "sender-category:receipts",
            }),
          ],
          title: "Legacy proposal",
        }),
      ],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Proposal: Legacy proposal");
    fireEvent.click(within(row).getByRole("button", { expanded: false }));

    expect(row).toHaveTextContent("gmail.label · sender-category:receipts");
    // Raw params still reachable, but only behind the details disclosure.
    const details = within(row).getByText("Details").closest("details");
    expect(details).not.toHaveAttribute("open");
  });

  test("deselecting an action and approving sends its id in deselected_action_ids", async () => {
    const keep = proposalAction({ id: "action-keep", kind: "send_email" });
    const drop = proposalAction({ id: "action-drop", kind: "archive" });
    const api = new FakeApi({
      authenticated: true,
      proposals: [
        proposal({
          actions: [keep, drop],
          id: "prop-1",
          title: "Two actions",
          version: 1,
        }),
      ],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Proposal: Two actions");
    fireEvent.click(within(row).getByRole("button", { expanded: false }));
    const checkboxes = within(row).getAllByRole("checkbox");
    expect(checkboxes).toHaveLength(2);
    // Uncheck the "archive" action (the second one).
    fireEvent.click(checkboxes[1]);
    fireEvent.click(within(row).getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(api.approveProposalCalls).toEqual([
        {
          deselectedActionIds: ["action-drop"],
          proposalId: "prop-1",
          version: 1,
        },
      ]);
    });
  });

  test("approving without deselecting anything sends an empty list", async () => {
    const api = new FakeApi({
      authenticated: true,
      proposals: [
        proposal({ id: "prop-1", title: "Just one action", version: 4 }),
      ],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Proposal: Just one action");
    fireEvent.click(within(row).getByRole("button", { expanded: false }));
    fireEvent.click(within(row).getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(api.approveProposalCalls).toEqual([
        { deselectedActionIds: [], proposalId: "prop-1", version: 4 },
      ]);
    });
    await waitFor(() => {
      expect(
        screen.queryByLabelText("Proposal: Just one action"),
      ).not.toBeInTheDocument();
    });
  });

  test("rejecting with a reason posts it and clears the queue row", async () => {
    const api = new FakeApi({
      authenticated: true,
      proposals: [
        proposal({ id: "prop-1", title: "Send a bad email", version: 2 }),
      ],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Proposal: Send a bad email");
    fireEvent.click(within(row).getByRole("button", { expanded: false }));
    fireEvent.click(within(row).getByRole("button", { name: "Reject" }));
    fireEvent.input(input(screen.getByLabelText("Reason (optional)")), {
      target: { value: "Wrong recipient" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm reject" }));

    await waitFor(() => {
      expect(api.rejectProposalCalls).toEqual([
        { proposalId: "prop-1", reason: "Wrong recipient", version: 2 },
      ]);
    });
    await waitFor(() => {
      expect(
        screen.queryByLabelText("Proposal: Send a bad email"),
      ).not.toBeInTheDocument();
    });
  });

  test("approving recovers from a stale-version 409 that is a mere version bump", async () => {
    const api = new FakeApi({
      authenticated: true,
      proposals: [
        proposal({ id: "prop-1", title: "Safe to retry", version: 1 }),
      ],
    });
    api.serverProposalVersions = { "prop-1": 2 };
    renderApp(api);

    const row = await screen.findByLabelText("Proposal: Safe to retry");
    fireEvent.click(within(row).getByRole("button", { expanded: false }));
    fireEvent.click(within(row).getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(api.approveProposalCalls).toEqual([
        { deselectedActionIds: [], proposalId: "prop-1", version: 1 },
        { deselectedActionIds: [], proposalId: "prop-1", version: 2 },
      ]);
    });
    await waitFor(() => {
      expect(
        screen.queryByLabelText("Proposal: Safe to retry"),
      ).not.toBeInTheDocument();
    });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  test("approving a proposal whose actions genuinely changed surfaces the conflict instead of retrying blind", async () => {
    const api = new FakeApi({
      authenticated: true,
      proposals: [
        proposal({ id: "prop-1", title: "Changed underneath", version: 1 }),
      ],
    });
    api.serverProposalVersions = { "prop-1": 2 };
    api.serverProposalEdits = {
      "prop-1": { summary: "A materially different summary now" },
    };
    renderApp(api);

    const row = await screen.findByLabelText("Proposal: Changed underneath");
    fireEvent.click(within(row).getByRole("button", { expanded: false }));
    fireEvent.click(within(row).getByRole("button", { name: "Approve" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This proposal changed",
    );
    expect(api.approveProposalCalls).toEqual([
      { deselectedActionIds: [], proposalId: "prop-1", version: 1 },
    ]);
    // The row is still there so the human can re-review it.
    expect(
      await screen.findByLabelText("Proposal: Changed underneath"),
    ).toBeInTheDocument();
  });

  test("a failed approve retry reports its own error, not the original 409", async () => {
    const api = new FakeApi({
      authenticated: true,
      proposals: [proposal({ id: "prop-1", title: "Retry fails", version: 1 })],
    });
    api.approveProposalRejections = [new ApiError(409), new ApiError(500)];
    renderApp(api);

    const row = await screen.findByLabelText("Proposal: Retry fails");
    fireEvent.click(within(row).getByRole("button", { expanded: false }));
    fireEvent.click(within(row).getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(api.approveProposalCalls).toHaveLength(2);
    });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(new ApiError(500).message);
    expect(alert).not.toHaveTextContent(new ApiError(409).message);
  });

  test("after a reject with revocable grants, the panel offers (not forces) revocation", async () => {
    const api = new FakeApi({
      authenticated: true,
      proposals: [
        proposal({ id: "prop-1", title: "Overreaching request", version: 1 }),
      ],
    });
    api.proposalRevocableGrantIds = { "prop-1": ["grant-abc12345"] };
    renderApp(api);

    const row = await screen.findByLabelText("Proposal: Overreaching request");
    fireEvent.click(within(row).getByRole("button", { expanded: false }));
    fireEvent.click(within(row).getByRole("button", { name: "Reject" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm reject" }));

    expect(
      await screen.findByText("Revoke the grants used for this?"),
    ).toBeInTheDocument();
    const revokeButton = screen.getByRole("button", {
      name: "Revoke grant-ab",
    });
    fireEvent.click(revokeButton);

    await waitFor(() => {
      expect(api.revokeGrantCalls).toEqual(["grant-abc12345"]);
    });
  });

  test("the history view shows decided proposals with their action outcomes", async () => {
    const api = new FakeApi({
      authenticated: true,
      proposals: [
        proposal({
          actions: [
            proposalAction({
              disposition: "approved",
              outcome: "succeeded",
              outcome_detail: "Sent to a@example.com",
            }),
          ],
          decided_at: "2026-01-05T00:00:00Z",
          id: "prop-1",
          state: "executed",
          title: "Already handled",
        }),
      ],
    });
    renderApp(api);

    const panel = await proposalsPanel();
    fireEvent.click(within(panel).getByRole("button", { name: "Decided" }));
    const row = await screen.findByLabelText("Proposal: Already handled");
    fireEvent.click(within(row).getByRole("button", { expanded: false }));

    expect(row).toHaveTextContent("succeeded");
    expect(row).toHaveTextContent("Sent to a@example.com");
  });

  test("empty states name the view they belong to", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    const panel = await proposalsPanel();
    expect(
      await within(panel).findByText("No pending proposals"),
    ).toBeInTheDocument();
    fireEvent.click(within(panel).getByRole("button", { name: "Decided" }));
    expect(
      await within(panel).findByText("No decided proposals yet"),
    ).toBeInTheDocument();
    fireEvent.click(within(panel).getByRole("button", { name: "Grants" }));
    expect(
      await within(panel).findByText("No active grants"),
    ).toBeInTheDocument();
    expect(
      await within(panel).findByText("No suggestions yet"),
    ).toBeInTheDocument();
  });

  test("the grants tab lists active grants with a revoke control", async () => {
    const api = new FakeApi({
      authenticated: true,
      grants: [grant({ id: "grant-1", kind: "send_email", scope: "inbox" })],
    });
    renderApp(api);

    const panel = await proposalsPanel();
    fireEvent.click(within(panel).getByRole("button", { name: "Grants" }));
    const row = await screen.findByLabelText("Grant: send_email (inbox)");
    fireEvent.click(within(row).getByRole("button", { name: "Revoke" }));

    await waitFor(() => {
      expect(api.revokeGrantCalls).toEqual(["grant-1"]);
    });
    await waitFor(() => {
      expect(
        screen.queryByLabelText("Grant: send_email (inbox)"),
      ).not.toBeInTheDocument();
    });
  });

  test("granting from a calibration suggestion posts the kind and scope", async () => {
    const api = new FakeApi({
      authenticated: true,
      grantSuggestions: [
        grantSuggestion({
          approved: 5,
          kind: "archive",
          rejected: 1,
          scope: "promotions",
          seen: 6,
        }),
      ],
    });
    renderApp(api);

    const panel = await proposalsPanel();
    fireEvent.click(within(panel).getByRole("button", { name: "Grants" }));
    const row = await screen.findByLabelText(
      "Suggestion: archive (promotions)",
    );
    expect(row).toHaveTextContent("seen 6");
    expect(row).toHaveTextContent("approved 5");
    fireEvent.click(within(row).getByRole("button", { name: "Grant" }));

    await waitFor(() => {
      expect(api.createGrantCalls).toEqual([
        { kind: "archive", scope: "promotions" },
      ]);
    });
  });
});
