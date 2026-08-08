import {
  createMutation,
  createQuery,
  useQueryClient,
} from "@tanstack/solid-query";
import { Match, Show, Switch } from "solid-js";

import type {
  ProviderAuthHost,
  ProviderAuthStatus,
} from "../host/provider-auth";
import { panelClass } from "../lib/panel";
import { queryKeys } from "../lib/query-keys";
import { Button } from "@/components/ui/button";

function statusLabel(state: ProviderAuthStatus["state"]): string {
  if (state === "connected") {
    return "Connected";
  }
  if (state === "authorizing") {
    return "Waiting for authorization";
  }
  if (state === "error") {
    return "Status unavailable";
  }
  return "Not connected";
}

export function ProviderAuthPanel(props: { api: ProviderAuthHost }) {
  const queryClient = useQueryClient();
  const statusQuery = createQuery(() => ({
    queryFn: () => props.api.getProviderAuthStatus(),
    queryKey: queryKeys.providerAuth,
    refetchInterval: (query) =>
      query.state.data?.state === "authorizing" ? 500 : false,
  }));
  const startMutation = createMutation(() => ({
    mutationFn: () => props.api.startProviderAuth(),
    onSuccess: (status) => {
      queryClient.setQueryData(queryKeys.providerAuth, status);
    },
  }));
  const cancelMutation = createMutation(() => ({
    mutationFn: () => props.api.cancelProviderAuth(),
    onSuccess: (status) => {
      queryClient.setQueryData(queryKeys.providerAuth, status);
    },
  }));

  return (
    <section aria-label="Model provider" class={panelClass}>
      <h2 class="mb-3 text-sm font-semibold">Model provider</h2>
      <Switch>
        <Match when={statusQuery.isLoading}>
          <p class="text-muted-foreground text-sm">Checking…</p>
        </Match>
        <Match when={statusQuery.isError}>
          <p class="text-destructive text-sm" role="alert">
            Could not check provider authorization
          </p>
        </Match>
        <Match when={statusQuery.data}>
          {(status) => (
            <div class="space-y-3 text-sm">
              <div>
                <p class="font-medium">OpenAI Codex</p>
                <p class="text-muted-foreground">
                  {statusLabel(status().state)}
                </p>
              </div>
              <Show when={status().error}>
                {(error) => (
                  <p class="text-destructive text-xs" role="alert">
                    {error()}
                  </p>
                )}
              </Show>
              <Show
                fallback={
                  <Button
                    disabled={startMutation.isPending}
                    onClick={() => startMutation.mutate()}
                    type="button"
                    variant={
                      status().state === "connected" ? "outline" : "default"
                    }
                  >
                    {status().state === "connected"
                      ? "Reconnect ChatGPT"
                      : "Connect ChatGPT"}
                  </Button>
                }
                when={status().state === "authorizing"}
              >
                <div class="space-y-3">
                  <Show
                    fallback={
                      <p class="text-muted-foreground text-xs">
                        Requesting a device code…
                      </p>
                    }
                    when={status().user_code && status().verification_uri}
                  >
                    <p class="text-muted-foreground text-xs">
                      Open OpenAI sign-in, then enter this one-time code:
                    </p>
                    <code class="bg-muted block w-fit rounded px-3 py-2 text-base font-semibold tracking-wider">
                      {status().user_code}
                    </code>
                    <a
                      class="text-primary block w-fit text-sm font-medium underline underline-offset-4"
                      href={status().verification_uri ?? undefined}
                      rel="noopener noreferrer"
                      target="_blank"
                    >
                      Open OpenAI sign-in
                    </a>
                  </Show>
                  <Button
                    disabled={cancelMutation.isPending}
                    onClick={() => cancelMutation.mutate()}
                    type="button"
                    variant="outline"
                  >
                    Cancel
                  </Button>
                </div>
              </Show>
              <Show when={startMutation.isError || cancelMutation.isError}>
                <p class="text-destructive text-xs" role="alert">
                  Provider authorization request failed. Try again.
                </p>
              </Show>
            </div>
          )}
        </Match>
      </Switch>
    </section>
  );
}
