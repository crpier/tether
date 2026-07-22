import { QueryClient } from "@tanstack/solid-query";

import type { ProposalState } from "../api";

export const queryKeys = {
  // The "bucket-items" prefix matches the host's InvalidateEvent key, so the
  // WS invalidate frame refetches every bucket query (list, history, triage).
  bucketItems: ["bucket-items"] as const,
  bucketItemsView: (view: "active" | "completed" | "deleted" | "triage") =>
    ["bucket-items", view] as const,
  bucketSearch: (q: string) => ["bucket-items", "search", q] as const,
  conversations: ["conversations"] as const,
  // The "memories" prefix matches the host's InvalidateEvent key (it emits
  // ["memories", "review-queue"]; the prefix alone already covers every
  // memories query — queue, corpus and search).
  memories: ["memories"] as const,
  memoriesSearch: (q: string) => ["memories", "search", q] as const,
  memoriesState: (state: "loose" | "tethered") => ["memories", state] as const,
  messages: (conversationId: string) => ["messages", conversationId] as const,
  models: ["models"] as const,
  notifications: ["notifications"] as const,
  // The "panels" prefix matches the host's InvalidateEvent key, so a panel
  // CRUD from any surface (agent tool or REST) refetches the saved list and
  // every per-panel results query.
  panels: ["panels"] as const,
  panelResults: (panelId: string) => ["panels", "results", panelId] as const,
  push: ["push"] as const,
  recall: ["recall"] as const,
  // The "proposals" prefix matches the host's InvalidateEvent key (proposal
  // approve/reject/execute all emit `["proposals"]`). Grant and suggestion
  // queries nest under the same prefix — the host does not (yet) publish an
  // invalidate event for grant/revoke, but nesting means a single "proposals"
  // invalidate frame already covers both without a second host-side key.
  proposals: ["proposals"] as const,
  proposalsState: (state: ProposalState) => ["proposals", state] as const,
  // The unfiltered list, for the decided-proposals history view.
  proposalsAll: ["proposals", "all"] as const,
  proposal: (proposalId: string) =>
    ["proposals", "detail", proposalId] as const,
  grants: ["proposals", "grants"] as const,
  grantSuggestions: ["proposals", "grants", "suggestions"] as const,
  session: ["session"] as const,
  // The "todos" prefix matches the host's InvalidateEvent key, so a todo CRUD
  // from any surface (agent tool or REST) refetches the ready/waiting list.
  todos: ["todos"] as const,
  triggers: ["triggers"] as const,
  youtube: ["youtube"] as const,
};

export function invalidateNamedKey(
  queryClient: QueryClient,
  key: string,
): void {
  if (key === "messages") {
    void queryClient.invalidateQueries({ queryKey: ["messages"] });
    void queryClient.refetchQueries({ queryKey: ["messages"] });
    return;
  }
  void queryClient.invalidateQueries({ queryKey: [key] });
  void queryClient.refetchQueries({ queryKey: [key] });
}

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        staleTime: 0,
      },
    },
  });
}
