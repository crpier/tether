import type {
  ApproveProposalInput,
  CreateGrant,
  Grant,
  GrantSuggestion,
  Proposal,
  ProposalRejection,
  ProposalsHost,
  ProposalState,
  RejectProposalInput,
} from "../../host/proposals";
import { ApiError } from "../../host/error";
import { grant } from "../fixtures";

export class FakeProposalsHost implements ProposalsHost {
  storedProposals: Proposal[];
  storedGrants: Grant[];
  storedGrantSuggestions: GrantSuggestion[];
  listProposalsCalls: (ProposalState | undefined)[] = [];
  getProposalCalls: string[] = [];
  approveProposalCalls: ({ proposalId: string } & ApproveProposalInput)[] = [];
  rejectProposalCalls: ({ proposalId: string } & RejectProposalInput)[] = [];
  createGrantCalls: CreateGrant[] = [];
  revokeGrantCalls: string[] = [];
  serverProposalVersions: Record<string, number> = {};
  serverProposalEdits: Record<string, Partial<Proposal>> = {};
  proposalRevocableGrantIds: Record<string, string[]> = {};
  approveProposalRejections: ApiError[] = [];
  rejectProposalRejections: ApiError[] = [];
  createGrantRejections: ApiError[] = [];
  revokeGrantRejections: ApiError[] = [];

  constructor(options: {
    grants?: Grant[];
    grantSuggestions?: GrantSuggestion[];
    proposals?: Proposal[];
  }) {
    this.storedProposals = options.proposals ?? [];
    this.storedGrants = options.grants ?? [];
    this.storedGrantSuggestions = options.grantSuggestions ?? [];
  }

  listProposals(state?: ProposalState): Promise<Proposal[]> {
    this.listProposalsCalls.push(state);
    return Promise.resolve(
      state === undefined
        ? this.storedProposals
        : this.storedProposals.filter((item) => item.state === state),
    );
  }

  getProposal(proposalId: string): Promise<Proposal> {
    this.getProposalCalls.push(proposalId);
    const found = this.storedProposals.find(
      (candidate) => candidate.id === proposalId,
    );
    if (found === undefined) {
      return Promise.reject(new ApiError(404));
    }
    return Promise.resolve(found);
  }

  approveProposal(
    proposalId: string,
    input: ApproveProposalInput,
  ): Promise<Proposal> {
    this.approveProposalCalls.push({ proposalId, ...input });
    const forced = this.approveProposalRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    const serverVersion = this.serverProposalVersions[proposalId];
    if (
      Object.hasOwn(this.serverProposalVersions, proposalId) &&
      serverVersion !== input.version
    ) {
      this.storedProposals = this.storedProposals.map((existing) =>
        existing.id === proposalId
          ? {
              ...existing,
              ...this.serverProposalEdits[proposalId],
              version: serverVersion,
            }
          : existing,
      );
      return Promise.reject(new ApiError(409));
    }
    const current = this.storedProposals.find(
      (existing) => existing.id === proposalId,
    );
    if (current === undefined) {
      return Promise.reject(new ApiError(404));
    }
    const decidedAt = "2026-01-02T00:00:00Z";
    const updated: Proposal = {
      ...current,
      actions: current.actions.map((action) => ({
        ...action,
        disposition: input.deselectedActionIds.includes(action.id)
          ? "deselected"
          : "approved",
      })),
      decided_at: decidedAt,
      state: "executed",
      version: input.version + 1,
    };
    this.serverProposalVersions[proposalId] = updated.version;
    this.storedProposals = this.storedProposals.map((existing) =>
      existing.id === proposalId ? updated : existing,
    );
    return Promise.resolve(updated);
  }

  rejectProposal(
    proposalId: string,
    input: RejectProposalInput,
  ): Promise<ProposalRejection> {
    this.rejectProposalCalls.push({ proposalId, ...input });
    const forced = this.rejectProposalRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    const serverVersion = this.serverProposalVersions[proposalId];
    if (
      Object.hasOwn(this.serverProposalVersions, proposalId) &&
      serverVersion !== input.version
    ) {
      this.storedProposals = this.storedProposals.map((existing) =>
        existing.id === proposalId
          ? {
              ...existing,
              ...this.serverProposalEdits[proposalId],
              version: serverVersion,
            }
          : existing,
      );
      return Promise.reject(new ApiError(409));
    }
    const current = this.storedProposals.find(
      (existing) => existing.id === proposalId,
    );
    if (current === undefined) {
      return Promise.reject(new ApiError(404));
    }
    const updated: Proposal = {
      ...current,
      decided_at: "2026-01-02T00:00:00Z",
      rejection_reason: input.reason ?? null,
      state: "rejected",
      version: input.version + 1,
    };
    this.serverProposalVersions[proposalId] = updated.version;
    this.storedProposals = this.storedProposals.map((existing) =>
      existing.id === proposalId ? updated : existing,
    );
    return Promise.resolve({
      proposal: updated,
      revocable_grant_ids: this.proposalRevocableGrantIds[proposalId] ?? [],
    });
  }

  listGrants(): Promise<Grant[]> {
    return Promise.resolve(this.storedGrants);
  }

  grantSuggestions(): Promise<GrantSuggestion[]> {
    return Promise.resolve(this.storedGrantSuggestions);
  }

  createGrant(body: CreateGrant): Promise<Grant> {
    this.createGrantCalls.push(body);
    const forced = this.createGrantRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    const created = grant({
      id: `018f0000-0000-7000-8000-0000000005${this.createGrantCalls.length
        .toString()
        .padStart(2, "0")}`,
      kind: body.kind,
      scope: body.scope,
    });
    this.storedGrants = [created, ...this.storedGrants];
    this.storedGrantSuggestions = this.storedGrantSuggestions.filter(
      (suggestion) =>
        !(suggestion.kind === body.kind && suggestion.scope === body.scope),
    );
    return Promise.resolve(created);
  }

  revokeGrant(grantId: string): Promise<void> {
    this.revokeGrantCalls.push(grantId);
    const forced = this.revokeGrantRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    this.storedGrants = this.storedGrants.filter(
      (candidate) => candidate.id !== grantId,
    );
    return Promise.resolve();
  }
}
