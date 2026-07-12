import { QueryClient } from "@tanstack/solid-query";

export const queryKeys = {
  conversations: ["conversations"] as const,
  messages: (conversationId: string) => ["messages", conversationId] as const,
  models: ["models"] as const,
  notifications: ["notifications"] as const,
  push: ["push"] as const,
  recall: ["recall"] as const,
  session: ["session"] as const,
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
