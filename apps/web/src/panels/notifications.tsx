import { createQuery, useQueryClient } from "@tanstack/solid-query";
import { For, Show, createMemo } from "solid-js";

import type { TetherApi } from "../api";
import { formatSyncTimestamp } from "../lib/format";
import { panelClass } from "../lib/panel";
import { queryKeys } from "../lib/query-keys";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

function notificationKindLabel(actionKind: string | null): string {
  if (actionKind === "prompt") {
    return "Agent result";
  }
  if (actionKind === "message") {
    return "Reminder";
  }
  return "Notification";
}

export function NotificationsPanel(props: {
  api: TetherApi;
  refreshToken: number;
}) {
  const queryClient = useQueryClient();
  const notificationsQuery = createQuery(() => ({
    queryFn: () => props.api.listNotifications(),
    // A fired notification arrives over the WebSocket callback, outside a
    // reactive owner; bumping the token there changes this key and forces a
    // refetch of the authoritative list (the same pattern the chat transcript
    // uses for its own socket-driven refreshes).
    queryKey: [...queryKeys.notifications, props.refreshToken] as const,
  }));
  const notifications = createMemo(() => notificationsQuery.data ?? []);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.notifications });
  };

  const dismiss = (notificationId: string) => {
    void (async () => {
      await props.api.dismissNotification(notificationId);
      refresh();
    })();
  };

  const clearAll = () => {
    void (async () => {
      await props.api.clearNotifications();
      refresh();
    })();
  };

  return (
    <section aria-label="Notifications" class={panelClass}>
      <div class="mb-3 flex items-center justify-between">
        <h2 class="text-sm font-semibold">Notifications</h2>
        <Show when={notifications().length > 0}>
          <Button onClick={clearAll} size="sm" type="button" variant="ghost">
            Clear all
          </Button>
        </Show>
      </div>
      <Show
        fallback={
          <p class="text-muted-foreground text-sm">No notifications yet</p>
        }
        when={notifications().length > 0}
      >
        <ul class="space-y-2">
          <For each={notifications()}>
            {(item) => (
              <li
                aria-label={`Notification: ${item.body}`}
                class="bg-muted rounded-md border px-3 py-2 text-sm"
              >
                <div class="flex items-center gap-2">
                  <Badge variant="secondary">
                    {notificationKindLabel(item.action_kind)}
                  </Badge>
                  <span class="text-muted-foreground text-xs">
                    {formatSyncTimestamp(item.created_at)}
                  </span>
                  <button
                    aria-label="Dismiss notification"
                    class="ml-auto shrink-0 opacity-70 hover:opacity-100"
                    onClick={() => {
                      dismiss(item.id);
                    }}
                    type="button"
                  >
                    ✕
                  </button>
                </div>
                <p class="mt-1">{item.body}</p>
                <Show when={item.action_kind === "prompt" && item.source_label}>
                  {(label) => (
                    <p class="text-muted-foreground mt-1 text-xs italic">
                      {`Prompt: ${label()}`}
                    </p>
                  )}
                </Show>
              </li>
            )}
          </For>
        </ul>
      </Show>
    </section>
  );
}
