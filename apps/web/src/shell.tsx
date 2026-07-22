import { A, useLocation } from "@solidjs/router";
import { createQuery } from "@tanstack/solid-query";
import { For, Show, createMemo, createSignal } from "solid-js";
import type { JSX } from "solid-js";

import { useAppContext } from "./app-context";
import { cx } from "./lib/cva";
import { queryKeys } from "./lib/query-keys";

interface NavItem {
  badge?: () => number;
  label: string;
  path: string;
}

// Badge counts are client-derived (#250): no count endpoints. Each list query
// mounted here stays warm — and invalidate-driven — regardless of which page
// is on screen, so a badge is simply the relevant list's length. Inbox sums
// every kind awaiting adjudication: loose-memory review, bucket triage
// findings, due recall prompts, and undismissed fired-reminder notifications.
function useBadgeCounts() {
  const { api } = useAppContext();

  const proposalsQuery = createQuery(() => ({
    queryFn: () => api.listProposals("pending"),
    queryKey: queryKeys.proposalsState("pending"),
  }));
  const looseMemoriesQuery = createQuery(() => ({
    queryFn: () => api.listMemories("loose"),
    queryKey: queryKeys.memoriesState("loose"),
  }));
  const bucketTriageQuery = createQuery(() => ({
    queryFn: () => api.getBucketTriage(),
    queryKey: queryKeys.bucketItemsView("triage"),
  }));
  const recallQuery = createQuery(() => ({
    queryFn: () => api.listDueRecallPrompts(),
    queryKey: queryKeys.recall,
  }));
  const notificationsQuery = createQuery(() => ({
    queryFn: () => api.listNotifications(),
    queryKey: queryKeys.notifications,
  }));

  const proposalsCount = createMemo(() => proposalsQuery.data?.length ?? 0);
  const inboxCount = createMemo(() => {
    const triage = bucketTriageQuery.data;
    const triageCount = triage
      ? triage.under_specified.length +
        triage.duplicates.length +
        triage.stale.length
      : 0;
    return (
      (looseMemoriesQuery.data?.length ?? 0) +
      triageCount +
      (recallQuery.data?.length ?? 0) +
      (notificationsQuery.data?.length ?? 0)
    );
  });

  return { inboxCount, proposalsCount };
}

function NavBadge(props: { count: number }) {
  return (
    <Show when={props.count > 0}>
      <span class="bg-sidebar-primary text-sidebar-primary-foreground ml-auto inline-flex min-w-5 items-center justify-center rounded-full px-1.5 py-0.5 text-[11px] font-semibold">
        {props.count}
      </span>
    </Show>
  );
}

function useNavItems(): NavItem[] {
  const { inboxCount, proposalsCount } = useBadgeCounts();
  return [
    { label: "Chat", path: "/" },
    { badge: proposalsCount, label: "Proposals", path: "/proposals" },
    { badge: inboxCount, label: "Inbox", path: "/inbox" },
    { label: "Browse", path: "/browse" },
    { label: "Settings", path: "/settings" },
  ];
}

function DesktopSidebar(props: { items: NavItem[] }) {
  const [collapsed, setCollapsed] = createSignal(false);
  const location = useLocation();

  return (
    <aside
      class={cx(
        "border-sidebar-border bg-sidebar text-sidebar-foreground hidden shrink-0 flex-col border-r transition-[width] duration-150 lg:flex",
        collapsed() ? "w-14" : "w-56",
      )}
    >
      <div class="flex items-center justify-between px-3 py-3">
        <Show when={!collapsed()}>
          <span class="text-sm font-bold tracking-wide">Tether</span>
        </Show>
        <button
          aria-label={collapsed() ? "Expand sidebar" : "Collapse sidebar"}
          class="text-sidebar-foreground/70 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground ml-auto rounded-md px-1.5 py-1 text-xs"
          onClick={() => {
            setCollapsed((value) => !value);
          }}
          type="button"
        >
          {collapsed() ? "»" : "«"}
        </button>
      </div>
      <nav aria-label="Main navigation" class="flex flex-col gap-1 px-2">
        <For each={props.items}>
          {(item) => {
            const active = createMemo(() => location.pathname === item.path);
            return (
              <A
                class={cx(
                  "flex items-center gap-2 rounded-md px-2 py-2 text-left text-sm font-medium",
                  active()
                    ? "bg-sidebar-accent text-sidebar-accent-foreground"
                    : "text-sidebar-foreground/80 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
                )}
                end={item.path === "/"}
                href={item.path}
              >
                <span
                  aria-hidden="true"
                  class="bg-sidebar-primary/20 inline-flex size-5 shrink-0 items-center justify-center rounded text-[11px] font-bold"
                >
                  {item.label.charAt(0)}
                </span>
                <Show when={!collapsed()}>
                  <span class="truncate">{item.label}</span>
                </Show>
                <Show when={!collapsed() && item.badge}>
                  {(badge) => <NavBadge count={badge()()} />}
                </Show>
              </A>
            );
          }}
        </For>
      </nav>
    </aside>
  );
}

function MobileBottomTabs(props: { items: NavItem[] }) {
  const location = useLocation();
  return (
    <nav
      aria-label="Main navigation (compact)"
      class="border-sidebar-border bg-sidebar text-sidebar-foreground fixed inset-x-0 bottom-0 z-40 flex border-t lg:hidden"
    >
      <For each={props.items}>
        {(item) => {
          const active = createMemo(() => location.pathname === item.path);
          return (
            <A
              class={cx(
                "relative flex flex-1 flex-col items-center gap-0.5 py-2 text-[11px] font-medium",
                active()
                  ? "text-sidebar-accent-foreground"
                  : "text-sidebar-foreground/70",
              )}
              end={item.path === "/"}
              href={item.path}
            >
              <span>{item.label}</span>
              <Show when={item.badge}>
                {(badge) => (
                  <Show when={badge()() > 0}>
                    <span class="bg-sidebar-primary text-sidebar-primary-foreground absolute top-1 right-3 inline-flex min-w-4 items-center justify-center rounded-full px-1 text-[9px] font-semibold">
                      {badge()()}
                    </span>
                  </Show>
                )}
              </Show>
            </A>
          );
        }}
      </For>
    </nav>
  );
}

export function Shell(props: { children?: JSX.Element }) {
  const items = useNavItems();

  return (
    <div class="flex h-dvh w-dvw overflow-hidden">
      <DesktopSidebar items={items} />
      <main class="flex min-w-0 flex-1 flex-col overflow-y-auto pb-16 lg:pb-0">
        {props.children}
      </main>
      <MobileBottomTabs items={items} />
    </div>
  );
}
