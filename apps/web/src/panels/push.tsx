import { createQuery, useQueryClient } from "@tanstack/solid-query";
import { Show, createSignal } from "solid-js";

import type { TetherApi } from "../api";
import { panelClass } from "../lib/panel";
import { queryKeys } from "../lib/query-keys";
import { Button } from "@/components/ui/button";

const PUSH_ENDPOINT_KEY = "tether-push-endpoint";

let cachedPushEndpoint: string | undefined;

function browserPushEndpoint(): string {
  if (cachedPushEndpoint !== undefined) {
    return cachedPushEndpoint;
  }
  let endpoint: string | null;
  try {
    endpoint = window.localStorage.getItem(PUSH_ENDPOINT_KEY);
  } catch {
    endpoint = null;
  }
  if (endpoint === null) {
    endpoint = `urn:tether:browser:${crypto.randomUUID()}`;
    try {
      window.localStorage.setItem(PUSH_ENDPOINT_KEY, endpoint);
    } catch {
      // localStorage unavailable (e.g. opaque origin); keep the in-memory value.
    }
  }
  cachedPushEndpoint = endpoint;
  return endpoint;
}

export function PushControl(props: { api: TetherApi }) {
  const queryClient = useQueryClient();
  const endpoint = browserPushEndpoint();
  const statusQuery = createQuery(() => ({
    queryFn: () => props.api.getPushStatus(endpoint),
    queryKey: queryKeys.push,
  }));
  const [busy, setBusy] = createSignal(false);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.push });
    void queryClient.refetchQueries({ queryKey: queryKeys.push });
  };

  const enable = () => {
    void (async () => {
      setBusy(true);
      try {
        await props.api.subscribePush(endpoint, "browser-key", "browser-auth");
        refresh();
      } finally {
        setBusy(false);
      }
    })();
  };

  const disable = () => {
    void (async () => {
      setBusy(true);
      try {
        await props.api.unsubscribePush(endpoint);
        refresh();
      } finally {
        setBusy(false);
      }
    })();
  };

  return (
    <section aria-label="Notification delivery" class={panelClass}>
      <h2 class="mb-3 text-sm font-semibold">Push notifications</h2>
      <Show
        fallback={<p class="text-muted-foreground text-sm">Checking…</p>}
        when={statusQuery.data}
      >
        {(status) => (
          <Show
            fallback={
              <div class="space-y-2">
                <p class="text-muted-foreground text-sm">Not subscribed</p>
                <Button disabled={busy()} onClick={enable} type="button">
                  Enable notifications
                </Button>
              </div>
            }
            when={status().subscribed}
          >
            <div class="space-y-2">
              <p class="text-sm">Subscribed</p>
              <Button
                disabled={busy()}
                onClick={disable}
                type="button"
                variant="outline"
              >
                Disable notifications
              </Button>
            </div>
          </Show>
        )}
      </Show>
    </section>
  );
}
