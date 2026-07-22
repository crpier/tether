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
    fireEvent.click(within(detail!).getByRole("button", { name: "Approve" }));

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

    fireEvent.click(await screen.findByRole("button", { name: "Grants" }));

    expect(
      await screen.findByLabelText("Grant: send_email"),
    ).toBeInTheDocument();
  });
});
