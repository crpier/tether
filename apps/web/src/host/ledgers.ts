import type { components } from "../generated";
import { requireData, type RestContext } from "./transport";

export type Ledger = components["schemas"]["LedgerRead"];
export type LedgerDetail = components["schemas"]["LedgerDetailRead"];
export type LedgerEntry = components["schemas"]["LedgerEntryRead"];
export type LedgerExport = components["schemas"]["LedgerExportRead"];
export type LedgerProposal = components["schemas"]["LedgerProposalRead"];

export interface LedgersHost {
  exportLedger(ledgerId: string): Promise<LedgerExport>;
  fetchLedger(ledgerId: string): Promise<LedgerDetail>;
  listLedgerEntries(
    ledgerId: string,
    includeSuperseded?: boolean,
  ): Promise<LedgerEntry[]>;
  listLedgerProposals(): Promise<LedgerProposal[]>;
  listLedgers(): Promise<Ledger[]>;
}

export function createLedgersHost(context: RestContext): LedgersHost {
  return {
    async exportLedger(ledgerId) {
      const { data, response } = await context.client.GET(
        "/api/ledgers/{ledger_id}/export",
        { params: { path: { ledger_id: ledgerId } } },
      );
      return requireData(data, response);
    },
    async fetchLedger(ledgerId) {
      const { data, response } = await context.client.GET(
        "/api/ledgers/{ledger_id}",
        { params: { path: { ledger_id: ledgerId } } },
      );
      return requireData(data, response);
    },
    async listLedgerEntries(ledgerId, includeSuperseded = false) {
      const { data, response } = await context.client.GET(
        "/api/ledgers/{ledger_id}/entries",
        {
          params: {
            path: { ledger_id: ledgerId },
            query: { include_superseded: includeSuperseded },
          },
        },
      );
      return requireData(data, response);
    },
    async listLedgerProposals() {
      const { data, response } = await context.client.GET(
        "/api/ledger-proposals",
      );
      return requireData(data, response);
    },
    async listLedgers() {
      const { data, response } = await context.client.GET("/api/ledgers");
      return requireData(data, response);
    },
  };
}
