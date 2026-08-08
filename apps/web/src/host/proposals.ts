import type { components } from "../generated";
import { requireData, requireOk, type RestContext } from "./transport";

export type Proposal = components["schemas"]["ProposalRead"];
export type ProposalAction = components["schemas"]["ProposalActionRead"];
export type ProposalState = components["schemas"]["ProposalState"];
export type ActionDisposition = components["schemas"]["ActionDisposition"];
export type ActionOutcome = components["schemas"]["ActionOutcome"];
export type ProposalRejection = components["schemas"]["RejectionRead"];
export type Grant = components["schemas"]["GrantRead"];
export type GrantSuggestion = components["schemas"]["GrantSuggestionRead"];
export type CreateGrant = components["schemas"]["CreateGrantRequest"];

export interface ApproveProposalInput {
  version: number;
  deselectedActionIds: string[];
}

export interface RejectProposalInput {
  version: number;
  reason?: string;
}

export interface ProposalsHost {
  listProposals(state?: ProposalState): Promise<Proposal[]>;
  getProposal(proposalId: string): Promise<Proposal>;
  approveProposal(
    proposalId: string,
    input: ApproveProposalInput,
  ): Promise<Proposal>;
  rejectProposal(
    proposalId: string,
    input: RejectProposalInput,
  ): Promise<ProposalRejection>;
  listGrants(): Promise<Grant[]>;
  grantSuggestions(): Promise<GrantSuggestion[]>;
  createGrant(body: CreateGrant): Promise<Grant>;
  revokeGrant(grantId: string): Promise<void>;
}

export function createProposalsHost(context: RestContext): ProposalsHost {
  return {
    async listProposals(state) {
      const query =
        state === undefined ? "" : `?state=${encodeURIComponent(state)}`;
      const response = await context.fetch(`/api/proposals${query}`, {
        credentials: "include",
      });
      const data = response.ok
        ? ((await response.json()) as Proposal[])
        : undefined;
      return requireData(data, response);
    },
    async getProposal(proposalId) {
      const { data, response } = await context.client.GET(
        "/api/proposals/{proposal_id}",
        { params: { path: { proposal_id: proposalId } } },
      );
      return requireData(data, response);
    },
    async approveProposal(proposalId, input) {
      const { data, response } = await context.client.POST(
        "/api/proposals/{proposal_id}/approve",
        {
          body: {
            deselected_action_ids: input.deselectedActionIds,
            version: input.version,
          },
          params: { path: { proposal_id: proposalId } },
        },
      );
      return requireData(data, response);
    },
    async rejectProposal(proposalId, input) {
      const { data, response } = await context.client.POST(
        "/api/proposals/{proposal_id}/reject",
        {
          body: { reason: input.reason ?? null, version: input.version },
          params: { path: { proposal_id: proposalId } },
        },
      );
      return requireData(data, response);
    },
    async listGrants() {
      const { data, response } = await context.client.GET("/api/grants");
      return requireData(data, response);
    },
    async grantSuggestions() {
      const { data, response } = await context.client.GET(
        "/api/grants/suggestions",
      );
      return requireData(data, response);
    },
    async createGrant(body) {
      const { data, response } = await context.client.POST("/api/grants", {
        body,
      });
      return requireData(data, response);
    },
    async revokeGrant(grantId) {
      const { response } = await context.client.DELETE(
        "/api/grants/{grant_id}",
        { params: { path: { grant_id: grantId } } },
      );
      requireOk(response);
    },
  };
}
