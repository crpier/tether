import {
  createMutation,
  createQuery,
  useQueryClient,
} from "@tanstack/solid-query";
import { Match, Show, Switch } from "solid-js";

import type { GmailHost } from "../host/gmail";
import { panelClass } from "../lib/panel";
import { queryKeys } from "../lib/query-keys";
import { Button } from "@/components/ui/button";

export function GmailSyncPanel(props: { api: GmailHost }) {
  const queryClient = useQueryClient();
  const authQuery = createQuery(() => ({
    queryFn: () => props.api.getGmailAuthStatus(),
    queryKey: queryKeys.gmailAuth,
    refetchInterval: (query) =>
      query.state.data?.state === "authorizing" ? 500 : false,
  }));
  const startAuthMutation = createMutation(() => ({
    mutationFn: () => props.api.startGmailAuth(),
    onSuccess: (status) => {
      queryClient.setQueryData(queryKeys.gmailAuth, status);
    },
  }));

  return (
    <section aria-label="Gmail" class={panelClass}>
      <h2 class="mb-3 text-sm font-semibold">Gmail</h2>
      <Switch>
        <Match when={authQuery.isLoading}>
          <p class="text-muted-foreground text-sm">Checking authorization…</p>
        </Match>
        <Match when={authQuery.isError}>
          <p class="text-destructive text-sm" role="alert">
            Could not check Gmail authorization
          </p>
        </Match>
        <Match when={authQuery.data}>
          {(authorization) => (
            <div class="space-y-3 text-sm">
              <div>
                <p class="font-medium">Google account</p>
                <p class="text-muted-foreground">
                  {authorization().state === "connected"
                    ? "Connected"
                    : authorization().state === "authorizing"
                      ? "Waiting for Google authorization"
                      : "Not connected"}
                </p>
              </div>
              <Show when={authorization().error}>
                {(error) => (
                  <p class="text-destructive text-xs" role="alert">
                    {error()}
                  </p>
                )}
              </Show>
              <Show
                fallback={
                  <Button
                    disabled={startAuthMutation.isPending}
                    onClick={() => startAuthMutation.mutate()}
                    type="button"
                    variant={
                      authorization().state === "connected"
                        ? "outline"
                        : "default"
                    }
                  >
                    {authorization().state === "connected"
                      ? "Reconnect Gmail"
                      : "Connect Gmail"}
                  </Button>
                }
                when={
                  authorization().state === "authorizing" &&
                  authorization().authorization_url
                }
              >
                <a
                  class="text-primary block w-fit text-sm font-medium underline underline-offset-4"
                  href={authorization().authorization_url ?? undefined}
                  rel="noopener noreferrer"
                >
                  Continue with Google
                </a>
              </Show>
              <Show when={startAuthMutation.isError}>
                <p class="text-destructive text-xs" role="alert">
                  Gmail authorization request failed. Try again.
                </p>
              </Show>
            </div>
          )}
        </Match>
      </Switch>
    </section>
  );
}
