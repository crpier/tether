import type {
  Ledger,
  LedgerDetail,
  LedgerEntry,
  LedgerExport,
  LedgerProposal,
  LedgersHost,
} from "../../host/ledgers";

export class FakeLedgersHost implements LedgersHost {
  readonly entriesByLedger: Record<string, LedgerEntry[]>;
  readonly ledgers: Ledger[];
  readonly proposals: LedgerProposal[];

  constructor(
    proposals: LedgerProposal[] = [],
    ledgers: Ledger[] = [],
    entriesByLedger: Record<string, LedgerEntry[]> = {},
  ) {
    this.entriesByLedger = entriesByLedger;
    this.ledgers = ledgers;
    this.proposals = proposals;
  }

  async exportLedger(ledgerId: string): Promise<LedgerExport> {
    const detail = await this.fetchLedger(ledgerId);
    return {
      entries: this.entriesByLedger[ledgerId] ?? [],
      ledger_id: ledgerId,
      proposals: this.proposals.filter(
        (proposal) => proposal.ledger_id === ledgerId,
      ),
      revisions: detail.revisions.toReversed(),
    };
  }

  fetchLedger(ledgerId: string): Promise<LedgerDetail> {
    const ledger = this.ledgers.find((candidate) => candidate.id === ledgerId);
    if (!ledger) {
      throw new Error("Ledger not found");
    }
    return Promise.resolve({ current: ledger, revisions: [ledger] });
  }

  listLedgerEntries(
    ledgerId: string,
    includeSuperseded = false,
  ): Promise<LedgerEntry[]> {
    const entries = this.entriesByLedger[ledgerId] ?? [];
    return Promise.resolve(
      includeSuperseded ? entries : entries.filter((entry) => entry.is_current),
    );
  }

  listLedgerProposals(): Promise<LedgerProposal[]> {
    return Promise.resolve(this.proposals);
  }

  listLedgers(): Promise<Ledger[]> {
    return Promise.resolve(this.ledgers);
  }
}
