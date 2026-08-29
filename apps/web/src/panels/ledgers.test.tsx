import { QueryClient, QueryClientProvider } from "@tanstack/solid-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { LedgersPanel } from "./ledgers";
import { FakeLedgersHost } from "../testing/fakes/ledgers";
import type { Ledger, LedgerEntry, LedgerProposal } from "../host/ledgers";

afterEach(cleanup);

const field = {
  deprecated: false,
  description: "The observed text.",
  enum_values: null,
  field_id: "observation",
  label: "Observation",
  required: true,
  type: "text" as const,
  unit: null,
};

const proposal: LedgerProposal = {
  approved_at: null,
  approved_by_message_id: null,
  base_revision: null,
  created_at: "2026-08-29T09:00:00Z",
  fields: [field],
  id: "01900000-0000-7000-8000-000000000001",
  kind: "create",
  ledger_id: "01900000-0000-7000-8000-000000000002",
  ledger_status: "active",
  name: "Observation log",
  proposed_by_conversation_id: "01900000-0000-7000-8000-000000000003",
  proposed_by_message_id: "01900000-0000-7000-8000-000000000004",
  proposed_revision: 1,
  purpose: "Record repeated observations.",
  status: "pending",
};

const ledger: Ledger = {
  approved_by_conversation_id: "01900000-0000-7000-8000-000000000003",
  approved_by_message_id: "01900000-0000-7000-8000-000000000005",
  created_at: "2026-08-29T09:05:00Z",
  fields: [field],
  id: proposal.ledger_id,
  name: proposal.name,
  proposal_id: proposal.id,
  purpose: proposal.purpose,
  revision: 1,
  status: "active",
};

const currentEntry: LedgerEntry = {
  evidence: ["tether://message/01900000-0000-7000-8000-000000000006"],
  id: "01900000-0000-7000-8000-000000000007",
  is_current: true,
  ledger_id: ledger.id,
  occurred_at: "2026-08-29T09:30:00Z",
  recorded_at: "2026-08-29T09:31:00Z",
  revision: 1,
  superseded_by_entry_id: null,
  supersedes_entry_id: "01900000-0000-7000-8000-000000000008",
  values: { observation: "overcast" },
};

const priorEntry: LedgerEntry = {
  evidence: ["tether://message/01900000-0000-7000-8000-000000000009"],
  id: "01900000-0000-7000-8000-000000000008",
  is_current: false,
  ledger_id: ledger.id,
  occurred_at: "2026-08-29T09:20:00Z",
  recorded_at: "2026-08-29T09:21:00Z",
  revision: 1,
  superseded_by_entry_id: currentEntry.id,
  supersedes_entry_id: null,
  values: { observation: "clear sky" },
};

function renderPanel(
  host: FakeLedgersHost,
  onOpenEvidence: (uri: string) => void = () => undefined,
) {
  return render(() => (
    <QueryClientProvider client={new QueryClient()}>
      <LedgersPanel api={host} onOpenEvidence={onOpenEvidence} />
    </QueryClientProvider>
  ));
}

describe("LedgersPanel", () => {
  test("shows the exact pending schema and requires approval through Chat", async () => {
    renderPanel(new FakeLedgersHost([proposal], [ledger]));

    const card = await screen.findByLabelText(
      "Ledger proposal: Observation log",
    );
    expect(
      within(card).getByText("Record repeated observations."),
    ).toBeVisible();
    expect(within(card).getByText("Observation")).toBeVisible();
    expect(within(card).getByText("observation")).toBeVisible();
    expect(within(card).getByText("The observed text.")).toBeVisible();
    expect(within(card).getByText("text · required")).toBeVisible();
    expect(
      within(card).getByRole("link", { name: "Approve in Chat" }),
    ).toHaveAttribute(
      "href",
      `/chat?prompt=${encodeURIComponent(`Approve Ledger proposal ${proposal.id}.`)}`,
    );
  });

  test("inspects current entries and reveals superseded history on demand", async () => {
    const opened: string[] = [];
    renderPanel(
      new FakeLedgersHost([], [ledger], {
        [ledger.id]: [currentEntry, priorEntry],
      }),
      (uri) => opened.push(uri),
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Open Observation log" }),
    );

    expect(await screen.findByText("overcast")).toBeVisible();
    expect(screen.queryByText("clear sky")).not.toBeInTheDocument();
    const current = screen.getByLabelText(`Ledger entry: ${currentEntry.id}`);
    fireEvent.click(within(current).getByRole("button", { name: "(source)" }));
    expect(opened).toEqual([currentEntry.evidence[0]]);

    fireEvent.click(screen.getByRole("button", { name: "Show history" }));
    expect(await screen.findByText("clear sky")).toBeVisible();
    expect(screen.getByText("Superseded")).toBeVisible();
  });
});
