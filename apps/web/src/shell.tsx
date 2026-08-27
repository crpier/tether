import { A, useLocation, useNavigate } from "@solidjs/router";
import { ConversationList } from "@kitn.ai/ui/solid";
import { createQuery, useQueryClient } from "@tanstack/solid-query";
import {
  For,
  Show,
  createEffect,
  createMemo,
  createSignal,
  onCleanup,
  onMount,
} from "solid-js";
import type { JSX } from "solid-js";

import { useHost } from "./app-context";
import {
  ConversationArchiveBlockedError,
  type UpdateConversation,
} from "./host/chat";
import { conversationHref, projectConversations } from "./kitn-chat-projection";
import { cx } from "./lib/cva";
import { queryKeys } from "./lib/query-keys";
import { Button } from "@/components/ui/button";
import {
  TextField,
  TextFieldInput,
  TextFieldLabel,
  TextFieldTextArea,
} from "@/components/ui/text-field";

interface NavItem {
  label: string;
  path: string;
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

function useNavItems(): NavItem[] {
  return [
    { label: "Chat", path: "/chat" },
    { label: "Health", path: "/health" },
    { label: "Browse", path: "/browse" },
    { label: "Settings", path: "/settings" },
  ];
}

function ConversationNavigation() {
  const chat = useHost("chat");
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
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
  const activeConversation = createMemo(() =>
    conversations().find((candidate) => candidate.id === activeId()),
  );
  const [editing, setEditing] = createSignal(false);
  const [displayName, setDisplayName] = createSignal("");
  const [scopeBrief, setScopeBrief] = createSignal("");
  const [error, setError] = createSignal<string>();
  let previousActiveId: string | undefined;

  createEffect(() => {
    const nextActiveId = activeId();
    if (nextActiveId !== previousActiveId) {
      previousActiveId = nextActiveId;
      setEditing(false);
      setError(undefined);
    }
  });

  const refreshConversations = () =>
    queryClient.invalidateQueries({ queryKey: queryKeys.conversations });
  const beginEditing = () => {
    const current = activeConversation();
    if (current === undefined) {
      return;
    }
    setDisplayName(current.display_name ?? "");
    setScopeBrief(current.scope_brief ?? "");
    setError(undefined);
    setEditing(true);
  };
  const save = () => {
    const current = activeConversation();
    if (current === undefined) {
      return;
    }
    const body: UpdateConversation = {};
    const nextDisplayName = displayName().trim();
    const nextScopeBrief = scopeBrief().trim();
    if (nextDisplayName !== current.display_name) {
      body.display_name = nextDisplayName;
    }
    if (nextScopeBrief !== current.scope_brief) {
      body.scope_brief = nextScopeBrief;
    }
    if (Object.keys(body).length === 0) {
      setEditing(false);
      return;
    }
    void chat
      .updateConversation(current.id, body)
      .then(async () => {
        setEditing(false);
        await refreshConversations();
      })
      .catch(() => setError("Conversation could not be updated."));
  };
  const archive = () => {
    const current = activeConversation();
    if (current === undefined) {
      return;
    }
    setError(undefined);
    void chat
      .archiveConversation(current.id)
      .then(async () => {
        await refreshConversations();
        navigate("/chat");
      })
      .catch((caught: unknown) => {
        if (
          caught instanceof ConversationArchiveBlockedError &&
          caught.blocker === "active_prompt_trigger"
        ) {
          navigate(`/browse/reminders?conversation=${current.id}`);
          return;
        }
        setError(
          caught instanceof ConversationArchiveBlockedError
            ? "Wait for this Conversation's turns to finish before archiving."
            : "Conversation could not be archived.",
        );
      });
  };

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
          <div>
            <Show
              when={
                activeConversation()?.kind === "scoped" &&
                activeConversation()?.status === "active"
              }
            >
              <Show
                fallback={
                  <div class="flex min-h-11 items-center gap-1 px-2">
                    <Button
                      aria-label="Edit conversation"
                      class="flex-1"
                      onClick={beginEditing}
                      size="sm"
                      type="button"
                      variant="ghost"
                    >
                      Rename
                    </Button>
                    <Button
                      aria-label="Archive conversation"
                      class="flex-1"
                      onClick={archive}
                      size="sm"
                      type="button"
                      variant="ghost"
                    >
                      Archive
                    </Button>
                  </div>
                }
                when={editing()}
              >
                <div class="space-y-2 border-b p-3">
                  <TextField onChange={setDisplayName} value={displayName()}>
                    <TextFieldLabel>Conversation name</TextFieldLabel>
                    <TextFieldInput />
                  </TextField>
                  <TextField onChange={setScopeBrief} value={scopeBrief()}>
                    <TextFieldLabel>Scope brief</TextFieldLabel>
                    <TextFieldTextArea rows={3} />
                  </TextField>
                  <div class="flex gap-1">
                    <Button
                      disabled={
                        displayName().trim().length === 0 ||
                        scopeBrief().trim().length === 0
                      }
                      onClick={save}
                      size="sm"
                      type="button"
                    >
                      Save conversation
                    </Button>
                    <Button
                      onClick={() => setEditing(false)}
                      size="sm"
                      type="button"
                      variant="ghost"
                    >
                      Cancel
                    </Button>
                  </div>
                </div>
              </Show>
              <Show when={error()}>
                {(message) => (
                  <p class="text-destructive px-3 py-2 text-xs" role="alert">
                    {message()}
                  </p>
                )}
              </Show>
            </Show>
            <A
              class="text-sidebar-foreground/60 flex min-h-11 items-center px-3 text-xs"
              href="/chat?archived=1"
            >
              Archived Conversations
            </A>
          </div>
        }
        groups={[]}
        header={
          <div class="text-sidebar-foreground/60 flex items-center px-3 py-2 text-[11px] font-semibold uppercase tracking-wide">
            <span>Conversations</span>
            <A
              aria-label="Create Conversation"
              class="ml-auto inline-flex size-11 items-center justify-center text-base"
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
            return (
              <A
                aria-label={item.label}
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

function MobileSidebar(props: { items: NavItem[] }) {
  const [open, setOpen] = createSignal(false);
  const location = useLocation();
  let drawer: HTMLElement | undefined;
  let swipeStart: { pointerId: number; x: number; y: number } | undefined;
  let previousLocation = `${location.pathname}${location.search}`;

  createEffect(() => {
    const currentLocation = `${location.pathname}${location.search}`;
    if (currentLocation !== previousLocation) {
      previousLocation = currentLocation;
      setOpen(false);
    }
  });

  onMount(() => {
    const onPointerDown = (event: PointerEvent) => {
      if (
        open() ||
        event.pointerType !== "touch" ||
        !event.isPrimary ||
        event.clientX > 24
      ) {
        return;
      }
      swipeStart = {
        pointerId: event.pointerId,
        x: event.clientX,
        y: event.clientY,
      };
    };
    const onPointerMove = (event: PointerEvent) => {
      const start = swipeStart;
      if (start?.pointerId !== event.pointerId) {
        return;
      }
      const horizontalDistance = event.clientX - start.x;
      const verticalDistance = Math.abs(event.clientY - start.y);
      if (horizontalDistance >= 56 && horizontalDistance > verticalDistance) {
        swipeStart = undefined;
        setOpen(true);
      } else if (
        verticalDistance > 32 &&
        verticalDistance > horizontalDistance
      ) {
        swipeStart = undefined;
      }
    };
    const finishSwipe = (event: PointerEvent) => {
      if (swipeStart?.pointerId === event.pointerId) {
        swipeStart = undefined;
      }
    };
    window.addEventListener("pointerdown", onPointerDown, { capture: true });
    window.addEventListener("pointermove", onPointerMove, { capture: true });
    window.addEventListener("pointerup", finishSwipe, { capture: true });
    window.addEventListener("pointercancel", finishSwipe, { capture: true });
    onCleanup(() => {
      window.removeEventListener("pointerdown", onPointerDown, {
        capture: true,
      });
      window.removeEventListener("pointermove", onPointerMove, {
        capture: true,
      });
      window.removeEventListener("pointerup", finishSwipe, { capture: true });
      window.removeEventListener("pointercancel", finishSwipe, {
        capture: true,
      });
    });
  });

  createEffect(() => {
    if (!open()) {
      return;
    }
    const previouslyFocused = document.activeElement;
    const focusable = () =>
      Array.from(
        drawer?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      );
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
        return;
      }
      if (event.key !== "Tab") {
        return;
      }
      const elements = focusable();
      if (elements.length === 0) {
        event.preventDefault();
        drawer?.focus();
        return;
      }
      const first = elements[0];
      const last = elements.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    queueMicrotask(() => focusable()[0]?.focus());
    onCleanup(() => {
      document.removeEventListener("keydown", onKeyDown);
      if (previouslyFocused instanceof HTMLElement) {
        previouslyFocused.focus();
      }
    });
  });

  return (
    <>
      <header class="border-sidebar-border bg-sidebar text-sidebar-foreground flex h-11 shrink-0 items-center border-b px-2">
        <button
          aria-label="Open sidebar"
          class="hover:bg-sidebar-accent hover:text-sidebar-accent-foreground inline-flex size-11 items-center justify-center rounded-md text-xl"
          onClick={() => setOpen(true)}
          type="button"
        >
          <span aria-hidden="true">☰</span>
        </button>
        <span class="ml-2 text-sm font-bold tracking-wide">Tether</span>
      </header>
      <Show when={!open()}>
        <div
          aria-hidden="true"
          class="fixed top-11 bottom-0 left-0 z-30 w-4 touch-pan-y"
        />
      </Show>
      <Show when={open()}>
        <button
          aria-hidden="true"
          class="fixed inset-0 z-40 bg-black/50"
          onClick={() => setOpen(false)}
          tabindex={-1}
          type="button"
        />
        <aside
          ref={(element) => {
            drawer = element;
          }}
          aria-label="Navigation sidebar"
          aria-modal="true"
          class="border-sidebar-border bg-sidebar text-sidebar-foreground fixed inset-y-0 left-0 z-50 flex w-[min(20rem,88vw)] flex-col border-r shadow-xl"
          role="dialog"
          tabindex={-1}
        >
          <div class="flex h-11 shrink-0 items-center border-b px-3">
            <span class="text-sm font-bold tracking-wide">Tether</span>
            <button
              aria-label="Close sidebar"
              class="hover:bg-sidebar-accent hover:text-sidebar-accent-foreground ml-auto inline-flex size-11 items-center justify-center rounded-md text-lg"
              onClick={() => setOpen(false)}
              type="button"
            >
              <span aria-hidden="true">×</span>
            </button>
          </div>
          <nav
            aria-label="Main navigation"
            class="flex flex-col gap-1 px-2 py-2"
          >
            <For each={props.items}>
              {(item) => {
                const active = createMemo(() =>
                  item.path === "/chat"
                    ? location.pathname === "/" ||
                      location.pathname.startsWith("/chat")
                    : location.pathname === item.path,
                );
                return (
                  <A
                    aria-label={item.label}
                    class={cx(
                      "flex min-h-11 items-center gap-2 rounded-md px-2 text-sm font-medium",
                      active()
                        ? "bg-sidebar-accent text-sidebar-accent-foreground"
                        : "text-sidebar-foreground/80 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
                    )}
                    href={item.path}
                  >
                    <span
                      aria-hidden="true"
                      class="bg-sidebar-primary/20 inline-flex size-5 shrink-0 items-center justify-center rounded text-[11px] font-bold"
                    >
                      {item.label.charAt(0)}
                    </span>
                    <span>{item.label}</span>
                  </A>
                );
              }}
            </For>
          </nav>
          <ConversationNavigation />
        </aside>
      </Show>
    </>
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
      <main class="flex min-w-0 flex-1 flex-col overflow-y-auto">
        <Show when={!isDesktop()}>
          <MobileSidebar items={items} />
        </Show>
        {props.children}
      </main>
    </div>
  );
}
