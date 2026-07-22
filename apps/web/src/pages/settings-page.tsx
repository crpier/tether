import { useQueryClient } from "@tanstack/solid-query";
import { createSignal } from "solid-js";

import { useAppContext } from "../app-context";
import { queryKeys } from "../lib/query-keys";
import { PushControl } from "../panels/push";
import { YouTubeSyncPanel } from "../panels/youtube";
import { Button } from "@/components/ui/button";

// Rarely-touched controls (#250): YouTube sync status, push subscription
// toggle, logout. Everything here was previously stacked permanently in the
// single-page sidebar; it earns a page of its own precisely because it is
// visited seldom.
export function SettingsPage() {
  const { api } = useAppContext();
  const queryClient = useQueryClient();
  const [loggingOut, setLoggingOut] = createSignal(false);

  const logout = () => {
    if (loggingOut()) {
      return;
    }
    void (async () => {
      setLoggingOut(true);
      try {
        await api.logout();
        await queryClient.invalidateQueries({ queryKey: queryKeys.session });
      } finally {
        setLoggingOut(false);
      }
    })();
  };

  return (
    <main
      aria-labelledby="settings-title"
      class="flex min-h-full flex-1 flex-col"
    >
      <header class="bg-card border-b px-4 py-3 sm:px-5">
        <h1 id="settings-title" class="text-lg font-semibold tracking-tight">
          Settings
        </h1>
      </header>
      <div class="mx-auto w-full max-w-xl flex-1 space-y-4 p-4 sm:p-5">
        <YouTubeSyncPanel api={api} />
        <PushControl api={api} />
        <section class="bg-card text-card-foreground rounded-xl border p-4 shadow-sm">
          <h2 class="mb-3 text-sm font-semibold">Account</h2>
          <Button
            disabled={loggingOut()}
            onClick={logout}
            type="button"
            variant="outline"
          >
            Log out
          </Button>
        </section>
      </div>
    </main>
  );
}
