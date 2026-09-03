import {
  createMutation,
  createQuery,
  useQueryClient,
} from "@tanstack/solid-query";
import { For, Match, Show, Switch } from "solid-js";

import type { YouTubeHost } from "../host/youtube";
import { formatDateTime, formatSyncTimestamp } from "../lib/format";
import { panelClass } from "../lib/panel";
import { queryKeys } from "../lib/query-keys";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

function formatUntil(iso: string): string {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) {
    return iso;
  }
  return formatDateTime(when);
}

export function YouTubeSyncPanel(props: { api: YouTubeHost }) {
  const queryClient = useQueryClient();
  const authQuery = createQuery(() => ({
    queryFn: () => props.api.getYouTubeAuthStatus(),
    queryKey: queryKeys.youtubeAuth,
  }));
  const startAuthMutation = createMutation(() => ({
    mutationFn: () => props.api.startYouTubeAuth(),
    onSuccess: (status) => {
      queryClient.setQueryData(queryKeys.youtubeAuth, status);
    },
  }));
  const statusQuery = createQuery(() => ({
    queryFn: () => props.api.getYouTubeSyncStatus(),
    queryKey: queryKeys.youtube,
    // Sync completions push a "youtube" invalidate over the chat socket, but
    // poll too so quota/pause clocks stay fresh without a sync event.
    refetchInterval: 60_000,
  }));

  return (
    <section aria-label="YouTube sync" class={panelClass}>
      <h2 class="mb-3 text-sm font-semibold">YouTube</h2>
      <Switch>
        <Match when={authQuery.isLoading}>
          <p class="text-muted-foreground text-sm">Checking authorization…</p>
        </Match>
        <Match when={authQuery.isError}>
          <p class="text-destructive text-sm" role="alert">
            Could not check YouTube authorization
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
                      ? "Reconnect YouTube"
                      : "Connect YouTube"}
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
                  YouTube authorization request failed. Try again.
                </p>
              </Show>
            </div>
          )}
        </Match>
      </Switch>
      <h3 class="mb-3 mt-4 border-t pt-4 text-sm font-semibold">Sync health</h3>
      <Switch>
        <Match when={statusQuery.isLoading}>
          <p class="text-muted-foreground text-sm">Loading…</p>
        </Match>
        <Match when={statusQuery.isError}>
          <p class="text-destructive text-sm" role="alert">
            Could not load sync status
          </p>
        </Match>
        <Match when={statusQuery.data}>
          {(status) => (
            <div class="space-y-3 text-sm">
              <div class="flex items-baseline justify-between">
                <span class="text-muted-foreground text-xs">Videos</span>
                <span class="font-medium">{status().videos_total}</span>
              </div>
              <div class="space-y-1">
                <span class="text-muted-foreground text-xs">Transcripts</span>
                <div class="flex flex-wrap gap-1">
                  <Badge variant="secondary">
                    {`${String(status().transcriptions.done)} done`}
                  </Badge>
                  <Badge variant="outline">
                    {`${String(status().transcriptions.pending)} pending`}
                  </Badge>
                  <Show when={status().transcriptions.needs_review > 0}>
                    <Badge variant="outline">
                      {`${String(status().transcriptions.needs_review)} needs review`}
                    </Badge>
                  </Show>
                  <Show when={status().transcriptions.unavailable > 0}>
                    <Badge variant="outline">
                      {`${String(status().transcriptions.unavailable)} unavailable`}
                    </Badge>
                  </Show>
                </div>
              </div>
              <div class="flex items-baseline justify-between">
                <span class="text-muted-foreground text-xs">Last synced</span>
                <span>
                  {status().last_synced_at
                    ? formatSyncTimestamp(status().last_synced_at ?? "")
                    : "never"}
                </span>
              </div>
              <div class="flex items-baseline justify-between">
                <span class="text-muted-foreground text-xs">Daily quota</span>
                <span>{`${String(status().quota.used)} / ${String(status().quota.limit)}`}</span>
              </div>
              <Show when={status().api_paused_until}>
                {(until) => (
                  <p class="text-destructive text-xs" role="status">
                    {`API backing off until ${formatUntil(until())}`}
                    <span class="mt-0.5 block opacity-80">
                      Auto-retry after a quota error from YouTube — not the
                      daily budget above. Clears on the first successful call.
                    </span>
                  </p>
                )}
              </Show>
              <For each={status().transcriptions.providers_paused}>
                {(pause) => (
                  <p class="text-destructive text-xs" role="status">
                    {`${pause.source} paused until ${formatUntil(pause.paused_until)}`}
                  </p>
                )}
              </For>
            </div>
          )}
        </Match>
      </Switch>
    </section>
  );
}
