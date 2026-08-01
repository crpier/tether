import { createQuery, useQueryClient } from "@tanstack/solid-query";
import { Show, createSignal } from "solid-js";

import type { TetherApi } from "../api";
import { panelClass } from "../lib/panel";
import { queryKeys } from "../lib/query-keys";
import { Button } from "@/components/ui/button";

function base64UrlToBytes(value: string): Uint8Array<ArrayBuffer> {
  const padded = `${value}${"=".repeat((4 - (value.length % 4)) % 4)}`;
  const binary = window.atob(padded.replaceAll("-", "+").replaceAll("_", "/"));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function keyToBase64Url(key: ArrayBuffer | null): string {
  if (key === null) {
    return "";
  }
  const binary = String.fromCharCode(...new Uint8Array(key));
  return window
    .btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/u, "");
}

function pushSupported(): boolean {
  return "serviceWorker" in navigator && "PushManager" in window;
}

async function currentSubscription(): Promise<PushSubscription | null> {
  if (!pushSupported()) {
    return null;
  }
  const registration = await navigator.serviceWorker.register("/sw.js");
  return registration.pushManager.getSubscription();
}

async function subscribeBrowser(api: TetherApi): Promise<void> {
  if (!pushSupported()) {
    throw new Error("Push notifications are not supported in this browser.");
  }
  if (Notification.permission !== "granted") {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      throw new Error("Notification permission was not granted.");
    }
  }
  const config = await api.getPushConfig();
  const registration = await navigator.serviceWorker.register("/sw.js");
  const subscription = await registration.pushManager.subscribe({
    applicationServerKey: base64UrlToBytes(config.vapid_public_key),
    userVisibleOnly: true,
  });
  await api.subscribePush(
    subscription.endpoint,
    keyToBase64Url(subscription.getKey("p256dh")),
    keyToBase64Url(subscription.getKey("auth")),
  );
}

async function unsubscribeBrowser(api: TetherApi): Promise<void> {
  const subscription = await currentSubscription();
  if (subscription === null) {
    return;
  }
  await subscription.unsubscribe();
  await api.unsubscribePush(subscription.endpoint);
}

export function PushControl(props: { api: TetherApi }) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = createSignal(false);
  const statusQuery = createQuery(() => ({
    queryFn: async () => {
      const subscription = await currentSubscription();
      return props.api.getPushStatus(subscription?.endpoint ?? "");
    },
    queryKey: queryKeys.push,
  }));

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.push });
    void queryClient.refetchQueries({ queryKey: queryKeys.push });
  };

  const enable = () => {
    void (async () => {
      setBusy(true);
      try {
        await subscribeBrowser(props.api);
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
        await unsubscribeBrowser(props.api);
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
                <Button
                  disabled={busy() || !pushSupported()}
                  onClick={enable}
                  type="button"
                >
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
