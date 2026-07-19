import { cleanup, screen, waitFor, within } from "@solidjs/testing-library";
import { fireEvent } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { FakeApi, memory, panel, renderApp } from "../testing/harness";

afterEach(cleanup);

describe("Synthetic panels", () => {
  test("renders nothing when no panels are saved", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    await screen.findByLabelText("Reminder");
    expect(screen.queryByLabelText(/^Panel:/)).not.toBeInTheDocument();
  });

  test("renders a saved panel's results as a table with facet columns", async () => {
    const finance = panel({ columns: ["due"], name: "finance" });
    const api = new FakeApi({
      authenticated: true,
      panelResults: {
        [finance.id]: {
          memories: [
            memory({
              content: "rent is 900",
              facets: { domain: "finance", due: "monthly" },
              state: "tethered",
              tethered_at: "2026-06-01T00:00:00Z",
            }),
          ],
          total: 1,
        },
      },
      panels: [finance],
    });
    renderApp(api);

    const card = await screen.findByLabelText("Panel: finance");
    expect(await within(card).findByText("rent is 900")).toBeInTheDocument();
    expect(within(card).getByText("monthly")).toBeInTheDocument();
    expect(
      within(card).getByRole("columnheader", { name: "due" }),
    ).toBeInTheDocument();
  });

  test("an empty panel says so instead of showing a bare table", async () => {
    const empty = panel({ name: "travel" });
    const api = new FakeApi({ authenticated: true, panels: [empty] });
    renderApp(api);

    const card = await screen.findByLabelText("Panel: travel");
    expect(
      await within(card).findByText(/No memories match this panel/),
    ).toBeInTheDocument();
  });

  test("caps are reported as showing N of M", async () => {
    const broad = panel({ name: "everything-finance" });
    const api = new FakeApi({
      authenticated: true,
      panelResults: {
        [broad.id]: {
          memories: [memory({ content: "one" }), memory({ content: "two" })],
          total: 5,
        },
      },
      panels: [broad],
    });
    renderApp(api);

    const card = await screen.findByLabelText("Panel: everything-finance");
    expect(await within(card).findByText("Showing 2 of 5")).toBeInTheDocument();
  });

  test("a broken stored vega-lite spec falls back to the table with a note", async () => {
    const chart = panel({
      name: "spend",
      render_kind: "vega-lite",
      vega_lite_spec: "{not valid json",
    });
    const api = new FakeApi({
      authenticated: true,
      panelResults: {
        [chart.id]: {
          memories: [memory({ content: "rent is 900" })],
          total: 1,
        },
      },
      panels: [chart],
    });
    renderApp(api);

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
    const api = new FakeApi({ authenticated: true, panels: [doomed] });
    renderApp(api);

    const card = await screen.findByLabelText("Panel: old-panel");
    fireEvent.click(
      within(card).getByRole("button", { name: "Delete panel old-panel" }),
    );

    await waitFor(() => {
      expect(api.deletePanelCalls).toEqual([
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
