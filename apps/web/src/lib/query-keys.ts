import { QueryClient } from "@tanstack/solid-query";

export const queryKeys = {
  // The "bucket-items" prefix matches the host's InvalidateEvent key, so the
  // WS invalidate frame refetches every bucket query (list, history, triage).
  bucketItems: ["bucket-items"] as const,
  bucketItemsView: (view: "active" | "completed" | "deleted" | "triage") =>
    ["bucket-items", view] as const,
  bucketSearch: (q: string) => ["bucket-items", "search", q] as const,
  conversations: ["conversations"] as const,
  dreamRuns: ["dream-runs"] as const,
  dreamRun: (runId: string) => ["dream-runs", runId] as const,
  healthOverview: (days: number) => ["health", "overview", days] as const,
  ledgerEntries: (ledgerId: string, includeSuperseded: boolean) =>
    ["ledgers", ledgerId, "entries", includeSuperseded] as const,
  ledgerProposals: ["ledgers", "proposals"] as const,
  ledgers: ["ledgers"] as const,
  // The "memories" prefix matches the host's InvalidateEvent key (it emits
  // ["memories", "review-queue"]; the prefix alone already covers every
  // memories query — queue, corpus and search).
  memories: ["memories"] as const,
  memoryTopics: (q: string) => ["memories", "topics", q] as const,
  memoriesWorkspaceDiagnostics: [
    "memories",
    "workspace",
    "diagnostics",
  ] as const,
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
  providerAuth: ["provider-auth"] as const,
  productObservations: ["product-observations"] as const,
  session: ["session"] as const,
  // The "todos" prefix matches the host's InvalidateEvent key, so a todo CRUD
  // from any surface (agent tool or REST) refetches the ready/waiting list.
  todos: ["todos"] as const,
  triggers: ["triggers"] as const,
  gmailAuth: ["gmail-auth"] as const,
  youtube: ["youtube"] as const,
  youtubeAuth: ["youtube-auth"] as const,
  youtubeTranscriptDecisions: ["youtube", "transcript-decisions"] as const,
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
