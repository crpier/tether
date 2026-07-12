import { createQuery } from "@tanstack/solid-query";
import { For, Match, Show, Switch } from "solid-js";

import type { TetherApi } from "../api";
import { formatSyncTimestamp } from "../lib/format";
import { panelClass } from "../lib/panel";
import { queryKeys } from "../lib/query-keys";
import { Badge } from "@/components/ui/badge";

function formatUntil(iso: string): string {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) {
    return iso;
  }
  return when.toLocaleString();
}

export function YouTubeSyncPanel(props: { api: TetherApi }) {
  const statusQuery = createQuery(() => ({
    queryFn: () => props.api.getYouTubeSyncStatus(),
    queryKey: queryKeys.youtube,
    // Sync completions push a "youtube" invalidate over the chat socket, but
    // poll too so quota/pause clocks stay fresh without a sync event.
    refetchInterval: 60_000,
  }));

  return (
    <section aria-label="YouTube sync" class={panelClass}>
      <h2 class="mb-3 text-sm font-semibold">YouTube sync</h2>
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
                    {`${String(status().transcripts_done)} done`}
                  </Badge>
                  <Badge variant="outline">
                    {`${String(status().transcripts_pending)} pending`}
                  </Badge>
                  <Show when={status().transcripts_unavailable > 0}>
                    <Badge variant="outline">
                      {`${String(status().transcripts_unavailable)} unavailable`}
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
              <For each={status().transcript_providers_paused}>
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
