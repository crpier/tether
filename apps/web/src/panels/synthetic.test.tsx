import { cleanup, screen, waitFor, within } from "@solidjs/testing-library";
import { fireEvent } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { FakeHost, navigateTo, panel, renderApp } from "../testing/harness";

function topic(body: string, metadata: Record<string, unknown> = {}) {
  return {
    body,
    evidence: [],
    metadata,
    path: `${body.replaceAll(" ", "-")}.md`,
    title: `Topic: ${body}`,
  };
}

afterEach(cleanup);

describe("Synthetic panels", () => {
  test("an empty list explains panels and opens a Chat starter", async () => {
    const host = new FakeHost({ authenticated: true });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Panels" }));

    await screen.findByText(/Panels are saved views over your memories/);
    fireEvent.click(screen.getByRole("link", { name: "Create in Chat" }));

    await screen.findByRole("heading", { name: "Tether chat" });
    const composer = await screen.findByLabelText("Message");
    expect(composer).toHaveValue("Create a panel for: ");
    expect(composer).toHaveFocus();
  });

  test("renders a saved panel's results as a table with facet columns", async () => {
    const finance = panel({ columns: ["due"], name: "finance" });
    const host = new FakeHost({
      authenticated: true,
      panelResults: {
        [finance.id]: {
          topics: [topic("rent is 900", { domain: "finance", due: "monthly" })],
          total: 1,
        },
      },
      panels: [finance],
    });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Panels" }));

    const card = await screen.findByLabelText("Panel: finance");
    expect(await within(card).findByText("rent is 900")).toBeInTheDocument();
    expect(within(card).getByText("monthly")).toBeInTheDocument();
    expect(
      within(card).getByRole("columnheader", { name: "due" }),
    ).toBeInTheDocument();
  });

  test("an empty panel says so instead of showing a bare table", async () => {
    const empty = panel({ name: "travel" });
    const host = new FakeHost({ authenticated: true, panels: [empty] });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Panels" }));

    const card = await screen.findByLabelText("Panel: travel");
    expect(
      await within(card).findByText(/No Memory Topics match this panel/),
    ).toBeInTheDocument();
  });

  test("caps are reported as showing N of M", async () => {
    const broad = panel({ name: "everything-finance" });
    const host = new FakeHost({
      authenticated: true,
      panelResults: {
        [broad.id]: {
          topics: [topic("one"), topic("two")],
          total: 5,
        },
      },
      panels: [broad],
    });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Panels" }));

    const card = await screen.findByLabelText("Panel: everything-finance");
    expect(await within(card).findByText("Showing 2 of 5")).toBeInTheDocument();
  });

  test("a broken stored vega-lite spec falls back to the table with a note", async () => {
    const chart = panel({
      name: "spend",
      render_kind: "vega-lite",
      vega_lite_spec: "{not valid json",
    });
    const host = new FakeHost({
      authenticated: true,
      panelResults: {
        [chart.id]: {
          topics: [topic("rent is 900")],
          total: 1,
        },
      },
      panels: [chart],
    });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Panels" }));

    const card = await screen.findByLabelText("Panel: spend");
    await waitFor(() => {
      expect(
        within(card).getByText(/Chart spec failed to render/),
      ).toBeInTheDocument();
    });
    expect(within(card).getByText("rent is 900")).toBeInTheDocument();
  });

  test("deleting a panel calls the API with its version and removes it", async () => {
    const doomed = panel({ name: "old-panel", version: 3 });
    const host = new FakeHost({ authenticated: true, panels: [doomed] });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Panels" }));

    const card = await screen.findByLabelText("Panel: old-panel");
    fireEvent.click(
      within(card).getByRole("button", { name: "Delete panel old-panel" }),
    );

    await waitFor(() => {
      expect(host.panels.deletePanelCalls).toEqual([
        { panelId: doomed.id, version: 3 },
      ]);
    });
    await waitFor(() => {
      expect(
        screen.queryByLabelText("Panel: old-panel"),
      ).not.toBeInTheDocument();
    });
  });
});
