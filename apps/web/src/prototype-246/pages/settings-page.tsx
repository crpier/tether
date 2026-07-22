// PROTOTYPE #246 — throwaway, do not ship
//
// Settings: YouTube sync, push toggle, logout. Simple single layout.

import { createSignal } from "solid-js";

import { Button } from "@/components/ui/button";
import { panelClass } from "@/lib/panel";
import { cx } from "@/lib/cva";

export function SettingsPage() {
  const [pushEnabled, setPushEnabled] = createSignal(true);

  return (
    <div class="flex flex-1 flex-col gap-4 p-4">
      <h1 class="text-lg font-semibold">Settings</h1>

      <div class={cx(panelClass, "flex items-center justify-between")}>
        <div>
          <p class="font-medium">YouTube sync</p>
          <p class="text-sm text-muted-foreground">
            Last synced 3 hours ago · 214 videos indexed.
          </p>
        </div>
        <Button size="sm" variant="outline">
          Sync now
        </Button>
      </div>

      <div class={cx(panelClass, "flex items-center justify-between")}>
        <div>
          <p class="font-medium">Push notifications</p>
          <p class="text-sm text-muted-foreground">
            Deliver fired reminders and recall prompts to this device.
          </p>
        </div>
        <Button
          onClick={() => setPushEnabled(!pushEnabled())}
          size="sm"
          variant={pushEnabled() ? "default" : "outline"}
        >
          {pushEnabled() ? "Enabled" : "Disabled"}
        </Button>
      </div>

      <div class={cx(panelClass, "flex items-center justify-between")}>
        <div>
          <p class="font-medium">Session</p>
          <p class="text-sm text-muted-foreground">crpier42@gmail.com</p>
        </div>
        <Button size="sm" variant="destructive">
          Log out
        </Button>
      </div>
    </div>
  );
}
