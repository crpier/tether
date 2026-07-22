// PROTOTYPE #246 — throwaway, do not ship
//
// Plan: three variants of dense-page internals (A master-detail, B stacked
// cards, C dense table) on this prototype shell, switchable via ?variant=.
//
// Shell chrome: left sidebar (desktop, collapsible) / bottom tab bar
// (mobile), each nav item carrying a badge count. This chrome is shared
// across all three variants — only Proposals/Inbox page internals differ.

import { For, Match, Show, Switch, createSignal } from "solid-js";

import {
  mockBucketTriageItems,
  mockFiredReminders,
  mockMemoryReviewItems,
  mockProposals,
  mockRecallPrompts,
} from "./mock-data";
import { ChatPage } from "./pages/chat-page";
import { BrowsePage } from "./pages/browse-page";
import { InboxPage } from "./pages/inbox-page";
import { ProposalsPage } from "./pages/proposals-page";
import { SettingsPage } from "./pages/settings-page";
import { PrototypeSwitcher } from "./switcher";
import { currentPage, setCurrentPage } from "./store";
import type { ProtoPage } from "./types";
import { cx } from "@/lib/cva";

const proposalsBadge = mockProposals.length;
const inboxBadge =
  mockMemoryReviewItems.length +
  mockBucketTriageItems.length +
  mockRecallPrompts.length +
  mockFiredReminders.length;

const NAV_ITEMS: { page: ProtoPage; label: string; badge: number | null }[] = [
  { badge: null, label: "Chat", page: "chat" },
  { badge: proposalsBadge, label: "Proposals", page: "proposals" },
  { badge: inboxBadge, label: "Inbox", page: "inbox" },
  { badge: null, label: "Browse", page: "browse" },
  { badge: null, label: "Settings", page: "settings" },
];

function NavBadge(props: { count: number | null }) {
  return (
    <Show when={props.count !== null && props.count > 0}>
      <span class="ml-auto inline-flex min-w-5 items-center justify-center rounded-full bg-sidebar-primary px-1.5 py-0.5 text-[11px] font-semibold text-sidebar-primary-foreground">
        {props.count}
      </span>
    </Show>
  );
}

function DesktopSidebar() {
  const [collapsed, setCollapsed] = createSignal(false);

  return (
    <aside
      class={cx(
        "hidden shrink-0 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground transition-[width] duration-150 lg:flex",
        collapsed() ? "w-14" : "w-56",
      )}
    >
      <div class="flex items-center justify-between px-3 py-3">
        <Show when={!collapsed()}>
          <span class="text-sm font-bold tracking-wide">Tether</span>
        </Show>
        <button
          class="ml-auto rounded-md px-1.5 py-1 text-xs text-sidebar-foreground/70 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
          onClick={() => setCollapsed(!collapsed())}
          type="button"
        >
          {collapsed() ? "»" : "«"}
        </button>
      </div>
      <nav class="flex flex-col gap-1 px-2">
        <For each={NAV_ITEMS}>
          {(item) => (
            <button
              class={cx(
                "flex items-center gap-2 rounded-md px-2 py-2 text-left text-sm font-medium",
                currentPage() === item.page
                  ? "bg-sidebar-accent text-sidebar-accent-foreground"
                  : "text-sidebar-foreground/80 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
              )}
              onClick={() => setCurrentPage(item.page)}
              type="button"
            >
              <span
                class="inline-flex size-5 shrink-0 items-center justify-center rounded bg-sidebar-primary/20 text-[11px] font-bold"
                aria-hidden="true"
              >
                {item.label.charAt(0)}
              </span>
              <Show when={!collapsed()}>
                <span class="truncate">{item.label}</span>
              </Show>
              <Show when={!collapsed()}>
                <NavBadge count={item.badge} />
              </Show>
            </button>
          )}
        </For>
      </nav>
    </aside>
  );
}

function MobileBottomTabs() {
  return (
    <nav class="fixed inset-x-0 bottom-0 z-40 flex border-t border-sidebar-border bg-sidebar text-sidebar-foreground lg:hidden">
      <For each={NAV_ITEMS}>
        {(item) => (
          <button
            class={cx(
              "relative flex flex-1 flex-col items-center gap-0.5 py-2 text-[11px] font-medium",
              currentPage() === item.page
                ? "text-sidebar-accent-foreground"
                : "text-sidebar-foreground/70",
            )}
            onClick={() => setCurrentPage(item.page)}
            type="button"
          >
            <span>{item.label}</span>
            <Show when={item.badge !== null && item.badge > 0}>
              <span class="absolute right-3 top-1 inline-flex min-w-4 items-center justify-center rounded-full bg-sidebar-primary px-1 text-[9px] font-semibold text-sidebar-primary-foreground">
                {item.badge}
              </span>
            </Show>
          </button>
        )}
      </For>
    </nav>
  );
}

export function PrototypeShell() {
  return (
    <div class="flex h-dvh w-dvw overflow-hidden bg-background text-foreground">
      <DesktopSidebar />
      <main class="flex min-w-0 flex-1 flex-col overflow-y-auto pb-16 lg:pb-0">
        <Switch>
          <Match when={currentPage() === "chat"}>
            <ChatPage />
          </Match>
          <Match when={currentPage() === "proposals"}>
            <ProposalsPage />
          </Match>
          <Match when={currentPage() === "inbox"}>
            <InboxPage />
          </Match>
          <Match when={currentPage() === "browse"}>
            <BrowsePage />
          </Match>
          <Match when={currentPage() === "settings"}>
            <SettingsPage />
          </Match>
        </Switch>
      </main>
      <MobileBottomTabs />
      <PrototypeSwitcher />
    </div>
  );
}
