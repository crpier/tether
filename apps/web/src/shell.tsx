import { A, useLocation, useNavigate } from "@solidjs/router";
import { ConversationList } from "@kitn.ai/ui/solid";
import { createQuery } from "@tanstack/solid-query";
import {
  For,
  Show,
  createMemo,
  createSignal,
  onCleanup,
  onMount,
} from "solid-js";
import type { JSX } from "solid-js";

import { useHost } from "./app-context";
import { conversationHref, projectConversations } from "./kitn-chat-projection";
import { cx } from "./lib/cva";
import { queryKeys } from "./lib/query-keys";

interface NavItem {
  badge?: () => number;
  label: string;
  path: string;
}

// Badge counts are client-derived (#250): no count endpoints. Each list query
// mounted here stays warm and invalidate-driven regardless of which page is on
// screen. Inbox sums every kind awaiting adjudication: bucket triage findings,
// due Recall prompts, and undismissed fired-reminder notifications. Memory has
// no Inbox queue.
function useBadgeCounts() {
  const bucket = useHost("bucket");
  const notifications = useHost("notifications");
  const recall = useHost("recall");

  const bucketTriageQuery = createQuery(() => ({
    queryFn: () => bucket.getBucketTriage(),
    queryKey: queryKeys.bucketItemsView("triage"),
  }));
  const recallQuery = createQuery(() => ({
    queryFn: () => recall.listDueRecallPrompts(),
    queryKey: queryKeys.recall,
  }));
  const notificationsQuery = createQuery(() => ({
    queryFn: () => notifications.listNotifications(),
    queryKey: queryKeys.notifications,
  }));

  const inboxCount = createMemo(() => {
    const triage = bucketTriageQuery.data;
    const triageCount = triage
      ? triage.under_specified.length +
        triage.duplicates.length +
        triage.stale.length +
        triage.purchase.buy_now.length +
        triage.purchase.missing_price_context.length +
        triage.purchase.stale_watches.length
      : 0;
    return (
      triageCount +
      (recallQuery.data?.length ?? 0) +
      (notificationsQuery.data?.length ?? 0)
    );
  });

  return { inboxCount };
}

function createMediaQuery(query: string, fallback: boolean) {
  const initialMatches = () => {
    if (
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function"
    ) {
      return fallback;
    }
    return window.matchMedia(query).matches;
  };
  const [matches, setMatches] = createSignal(initialMatches());

  onMount(() => {
    if (
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function"
    ) {
      return;
    }
    const media = window.matchMedia(query);
    const update = () => {
      setMatches(media.matches);
    };
    update();
    media.addEventListener("change", update);
    onCleanup(() => {
      media.removeEventListener("change", update);
    });
  });

  return matches;
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
  const { inboxCount } = useBadgeCounts();
  return [
    { label: "Chat", path: "/chat" },
    { badge: inboxCount, label: "Inbox", path: "/inbox" },
    { label: "Browse", path: "/browse" },
    { label: "Settings", path: "/settings" },
  ];
}

function ConversationNavigation() {
  const chat = useHost("chat");
  const location = useLocation();
  const navigate = useNavigate();
  const conversationsQuery = createQuery(() => ({
    queryFn: () => chat.listConversations(),
    queryKey: queryKeys.conversations,
  }));
  const conversations = createMemo(() => conversationsQuery.data ?? []);
  const summaries = createMemo(() => projectConversations(conversations()));
  const activeId = createMemo(() => {
    if (location.pathname === "/" || location.pathname === "/chat") {
      return conversations().find((candidate) => candidate.kind === "main")?.id;
    }
    return location.pathname.startsWith("/chat/")
      ? location.pathname.slice("/chat/".length)
      : undefined;
  });

  return (
    <section
      aria-label="Conversations"
      class="mt-4 flex min-h-0 flex-1 flex-col"
    >
      <ConversationList
        activeId={activeId()}
        class="min-h-0"
        compact
        conversations={summaries()}
        footer={
          <A
            class="text-sidebar-foreground/60 px-3 py-2 text-xs"
            href="/chat?archived=1"
          >
            Archived Conversations
          </A>
        }
        groups={[]}
        header={
          <div class="text-sidebar-foreground/60 flex items-center px-3 py-2 text-[11px] font-semibold uppercase tracking-wide">
            <span>Conversations</span>
            <A
              aria-label="Create Conversation"
              class="ml-auto text-base"
              href="/chat?new=1"
            >
              +
            </A>
          </div>
        }
        onNewChat={() => navigate("/chat?new=1")}
        onSelect={(id) => {
          const selected = conversations().find(
            (candidate) => candidate.id === id,
          );
          if (selected !== undefined) {
            navigate(conversationHref(selected));
          }
        }}
      />
    </section>
  );
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
            const active = createMemo(() =>
              item.path === "/chat"
                ? location.pathname === "/" ||
                  location.pathname.startsWith("/chat")
                : location.pathname === item.path,
            );
            const badgeCount = createMemo(() => item.badge?.() ?? 0);
            const navLabel = createMemo(() =>
              badgeCount() > 0
                ? `${item.label} ${badgeCount().toString()}`
                : item.label,
            );
            return (
              <A
                aria-label={navLabel()}
                class={cx(
                  "group relative flex min-h-11 items-center gap-2 rounded-md px-2 text-left text-sm font-medium",
                  active()
                    ? "bg-sidebar-accent text-sidebar-accent-foreground"
                    : "text-sidebar-foreground/80 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
                )}
                end={item.path === "/"}
                href={item.path}
                title={item.label}
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
                <Show when={collapsed()}>
                  <span
                    aria-hidden="true"
                    class="bg-popover text-popover-foreground pointer-events-none absolute left-full z-50 ml-2 hidden rounded-md border px-2 py-1 text-xs shadow-sm group-focus-visible:block group-hover:block"
                  >
                    {item.label}
                  </span>
                </Show>
                <Show when={!collapsed() && badgeCount() > 0}>
                  <NavBadge count={badgeCount()} />
                </Show>
              </A>
            );
          }}
        </For>
      </nav>
      <Show when={!collapsed()}>
        <ConversationNavigation />
      </Show>
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
          const active = createMemo(() =>
            item.path === "/chat"
              ? location.pathname === "/" ||
                location.pathname.startsWith("/chat")
              : location.pathname === item.path,
          );
          const badgeCount = createMemo(() => item.badge?.() ?? 0);
          const navLabel = createMemo(() =>
            badgeCount() > 0
              ? `${item.label} ${badgeCount().toString()}`
              : item.label,
          );
          return (
            <A
              aria-label={navLabel()}
              class={cx(
                "relative flex min-h-11 flex-1 flex-col items-center justify-center gap-0.5 text-[11px] font-medium",
                active()
                  ? "text-sidebar-accent-foreground"
                  : "text-sidebar-foreground/70",
              )}
              end={item.path === "/"}
              href={item.path}
            >
              <span>{item.label}</span>
              <Show when={badgeCount() > 0}>
                <span class="bg-sidebar-primary text-sidebar-primary-foreground absolute top-1 right-3 inline-flex min-w-4 items-center justify-center rounded-full px-1 text-[9px] font-semibold">
                  {badgeCount()}
                </span>
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
  const isDesktop = createMediaQuery("(min-width: 1024px)", true);

  return (
    <div class="flex h-dvh w-dvw overflow-hidden">
      <Show when={isDesktop()}>
        <DesktopSidebar items={items} />
      </Show>
      <main class="flex min-w-0 flex-1 flex-col overflow-y-auto pb-16 lg:pb-0">
        {props.children}
      </main>
      <Show when={!isDesktop()}>
        <MobileBottomTabs items={items} />
      </Show>
    </div>
  );
}
