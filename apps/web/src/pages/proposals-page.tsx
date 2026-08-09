import { useSearchParams } from "@solidjs/router";
import { createQuery, useQueryClient } from "@tanstack/solid-query";
import {
  For,
  Match,
  Show,
  Switch,
  createEffect,
  createMemo,
  createSignal,
  untrack,
} from "solid-js";

import { useHost } from "../app-context";
import type {
  Grant,
  GrantSuggestion,
  Proposal,
  ProposalAction,
} from "../host/proposals";
import { ApiError } from "../host/error";
import {
  SegmentedControl,
  segmentedPanelId,
  segmentedTabId,
} from "../components/segmented-control";
import { formatDateTime } from "../lib/format";
import { queryKeys } from "../lib/query-keys";
import { cx } from "../lib/cva";
import { Button } from "@/components/ui/button";
import {
  TextField,
  TextFieldInput,
  TextFieldLabel,
} from "@/components/ui/text-field";

type ProposalsView = "queue" | "history" | "grants";
type HistoryStateFilter =
  "all" | "approved" | "executing" | "executed" | "failed" | "rejected";
type SearchValue = string | string[] | undefined;

const PAGE_SIZE = 25;

function singleSearchValue(value: SearchValue): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function parseQueryView(value: SearchValue): ProposalsView | undefined {
  const raw = singleSearchValue(value);
  return raw === "queue" || raw === "history" || raw === "grants"
    ? raw
    : undefined;
}

function parseQueryPage(value: SearchValue): number | undefined {
  const raw = singleSearchValue(value);
  if (raw === undefined) {
    return undefined;
  }
  const parsed = Number(raw);
  return Number.isInteger(parsed) && parsed >= 1 ? parsed : undefined;
}

function pageParam(page: number): string | undefined {
  return page > 1 ? page.toString() : undefined;
}

function pageCount(total: number): number {
  return Math.max(1, Math.ceil(total / PAGE_SIZE));
}

function boundedPage(page: number, total: number): number {
  return Math.min(page, pageCount(total));
}

function boundedGrantTabPage(
  page: number,
  activeGrantTotal: number,
  suggestionTotal: number,
): number {
  return Math.min(
    page,
    Math.max(pageCount(activeGrantTotal), pageCount(suggestionTotal)),
  );
}

function pageItems<T>(items: T[], page: number): T[] {
  const safePage = boundedPage(page, items.length);
  return items.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);
}

function searchable(value: string | null | undefined): string {
  return value?.toLocaleLowerCase() ?? "";
}

function matchesSearch(
  fields: (string | null | undefined)[],
  term: string,
): boolean {
  return (
    term.length === 0 ||
    fields.some((field) => searchable(field).includes(term))
  );
}

function actionCountLabel(count: number): string {
  return `${count.toString()} action${count === 1 ? "" : "s"}`;
}

function grantLabel(kind: string, scope: string | null): string {
  return scope === null ? `Grant: ${kind}` : `Grant: ${kind} (${scope})`;
}

function suggestionLabel(kind: string, scope: string | null): string {
  return scope === null
    ? `Suggestion: ${kind}`
    : `Suggestion: ${kind} (${scope})`;
}

function grantSuggestionActionLabel(
  kind: string,
  scope: string | null,
): string {
  return scope === null ? `Grant ${kind}` : `Grant ${kind} for ${scope}`;
}

// The primary, reviewer-facing line for one action: the consumer-supplied
// human-readable `display` when present, else a best-effort kind (+scope)
// summary for actions composed before display existed. Raw params stay behind
// the "Details" disclosure either way — never the primary text.
function actionPrimary(action: ProposalAction): string {
  if (action.display !== null && action.display.length > 0) {
    return action.display;
  }
  return action.scope !== null
    ? `${action.kind} · ${action.scope}`
    : action.kind;
}

function actionCategory(action: ProposalAction): string {
  return action.scope !== null
    ? `${action.kind} · ${action.scope}`
    : action.kind;
}

function historyActionSearchLabel(action: ProposalAction): string {
  const primary = actionPrimary(action);
  const category = actionCategory(action);
  return primary === category ? primary : `${primary} · ${category}`;
}

function historyActionMatchLabels(item: Proposal, term: string): string[] {
  if (term.length === 0) {
    return [];
  }
  return item.actions
    .map(historyActionSearchLabel)
    .filter((label) => searchable(label).includes(term));
}

function formatWhen(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : formatDateTime(parsed);
}

function decidedTimestampLabel(item: Proposal): string {
  return item.decided_at ? formatWhen(item.decided_at) : "not decided";
}

// The fields a 409 retry is judged against (mirrors triggers.tsx's
// `sameDefinition`): a mere version bump (e.g. a sibling action finished
// executing) is safe to retry, while a genuinely changed title, summary, or
// action set must stop and let the human re-review.
function actionBasis(action: ProposalAction) {
  return {
    disposition: action.disposition,
    id: action.id,
    kind: action.kind,
    params: action.params,
    scope: action.scope,
  };
}

function sameProposalBasis(a: Proposal, b: Proposal): boolean {
  return (
    a.title === b.title &&
    a.summary === b.summary &&
    JSON.stringify(a.actions.map(actionBasis)) ===
      JSON.stringify(b.actions.map(actionBasis))
  );
}

export interface ProposalsPageProps {
  initialView?: ProposalsView;
}

export function ProposalsPage(props: ProposalsPageProps = {}) {
  const api = useHost("proposals");
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  let useInitialView = props.initialView !== undefined;
  const initialView =
    parseQueryView(searchParams.tab) ?? props.initialView ?? "queue";
  const initialPage = parseQueryPage(searchParams.page) ?? 1;
  const initialHistorySearch = singleSearchValue(searchParams.search) ?? "";
  const [view, setView] = createSignal<ProposalsView>(initialView);
  const [selectedId, setSelectedId] = createSignal<string | undefined>();
  // Action ids unticked per proposal, keyed by proposal id, before approval.
  const [deselections, setDeselections] = createSignal<
    Record<string, string[]>
  >({});
  const [error, setError] = createSignal<string | undefined>();
  const [rejecting, setRejecting] = createSignal<
    { id: string; version: number } | undefined
  >();
  const [rejectReason, setRejectReason] = createSignal("");
  const [revocationOffers, setRevocationOffers] = createSignal<
    Record<string, string[]>
  >({});
  const [historySearch, setHistorySearch] = createSignal(initialHistorySearch);
  const [historyState, setHistoryState] =
    createSignal<HistoryStateFilter>("all");
  const [historyPage, setHistoryPage] = createSignal(
    initialView === "history" ? initialPage : 1,
  );
  const [grantSearch, setGrantSearch] = createSignal("");
  const [grantPage, setGrantPage] = createSignal(
    initialView === "grants" ? initialPage : 1,
  );
  const [suggestionSearch, setSuggestionSearch] = createSignal("");
  let pendingHistorySearchParam: string | undefined;

  const queueQuery = createQuery(() => ({
    queryFn: () => api.listProposals("pending"),
    queryKey: queryKeys.proposalsState("pending"),
  }));
  const historyQuery = createQuery(() => ({
    queryFn: () => api.listProposals(),
    queryKey: queryKeys.proposalsAll,
  }));
  const grantsQuery = createQuery(() => ({
    queryFn: () => api.listGrants(),
    queryKey: queryKeys.grants,
  }));
  const suggestionsQuery = createQuery(() => ({
    queryFn: () => api.grantSuggestions(),
    queryKey: queryKeys.grantSuggestions,
  }));

  const historyItems = createMemo(() =>
    (historyQuery.data ?? [])
      .filter((item) => item.state !== "pending")
      .toSorted((a, b) =>
        (b.decided_at ?? b.updated_at).localeCompare(
          a.decided_at ?? a.updated_at,
        ),
      ),
  );
  const filteredHistoryItems = createMemo(() => {
    const term = historySearch().trim().toLocaleLowerCase();
    const state = historyState();
    return historyItems().filter((item) => {
      const count = actionCountLabel(item.actions.length);
      const decided = decidedTimestampLabel(item);
      return (
        (state === "all" || item.state === state) &&
        matchesSearch(
          [
            item.title,
            count,
            `${item.title} ${count}`,
            `${item.title}: ${count}`,
            item.rejection_reason,
            decided,
            ...item.actions.map(historyActionSearchLabel),
          ],
          term,
        )
      );
    });
  });
  const visibleHistoryItems = createMemo(() =>
    pageItems(filteredHistoryItems(), historyPage()),
  );

  const queueItems = createMemo(() => queueQuery.data ?? []);
  const grants = createMemo(() => grantsQuery.data ?? []);
  const suggestions = createMemo(() =>
    [...(suggestionsQuery.data ?? [])].sort(
      (a, b) => b.seen - a.seen || a.kind.localeCompare(b.kind),
    ),
  );
  const filteredGrants = createMemo(() => {
    const term = grantSearch().trim().toLocaleLowerCase();
    return grants().filter((grant) =>
      matchesSearch([grant.kind, grant.scope], term),
    );
  });
  const visibleGrants = createMemo(() =>
    pageItems(filteredGrants(), grantPage()),
  );
  const filteredSuggestions = createMemo(() => {
    const term = suggestionSearch().trim().toLocaleLowerCase();
    return suggestions().filter((suggestion) =>
      matchesSearch([suggestion.kind, suggestion.scope], term),
    );
  });
  const visibleSuggestions = createMemo(() =>
    pageItems(filteredSuggestions(), grantPage()),
  );
  const selected = createMemo(() =>
    queueItems().find((item) => item.id === selectedId()),
  );

  createEffect(() => {
    const nextView =
      parseQueryView(searchParams.tab) ??
      (useInitialView ? props.initialView : undefined) ??
      "queue";
    useInitialView = false;
    const nextPage = parseQueryPage(searchParams.page) ?? 1;
    const nextHistorySearch = singleSearchValue(searchParams.search) ?? "";
    untrack(() => {
      setView(nextView);
      if (nextView === "history") {
        setHistoryPage(nextPage);
        if (pendingHistorySearchParam === nextHistorySearch) {
          pendingHistorySearchParam = undefined;
        } else {
          setHistorySearch(nextHistorySearch);
        }
      }
      if (nextView === "grants") {
        setGrantPage(nextPage);
      }
    });
  });

  createEffect(() => {
    if (historyQuery.data === undefined) {
      return;
    }
    const safePage = boundedPage(historyPage(), filteredHistoryItems().length);
    if (safePage !== historyPage()) {
      setHistoryPage(safePage);
    }
  });

  createEffect(() => {
    if (grantsQuery.data === undefined || suggestionsQuery.data === undefined) {
      return;
    }
    const safePage = boundedGrantTabPage(
      grantPage(),
      filteredGrants().length,
      filteredSuggestions().length,
    );
    if (safePage !== grantPage()) {
      setGrantPage(safePage);
    }
  });

  createEffect(() => {
    const nextTab = view() === "queue" ? undefined : view();
    const nextPage =
      view() === "history"
        ? pageParam(historyPage())
        : view() === "grants"
          ? pageParam(grantPage())
          : undefined;
    const nextSearch =
      view() === "history" ? historySearch().trim() || undefined : undefined;
    if (
      singleSearchValue(searchParams.tab) !== nextTab ||
      singleSearchValue(searchParams.page) !== nextPage ||
      singleSearchValue(searchParams.search) !== nextSearch
    ) {
      pendingHistorySearchParam = nextSearch ?? "";
      setSearchParams(
        { page: nextPage, search: nextSearch, tab: nextTab },
        { replace: true, scroll: false },
      );
    }
  });

  const refresh = () => {
    void queryClient.invalidateQueries({
      queryKey: queryKeys.proposals,
      refetchType: "none",
    });
    void queryClient.refetchQueries({
      queryKey: queryKeys.proposalsState("pending"),
    });
    void queryClient.refetchQueries({
      queryKey: queryKeys.proposalsAll,
    });
  };

  const patchProposalCache = (fresh: Proposal) => {
    queryClient.setQueryData<Proposal[]>(
      queryKeys.proposalsState("pending"),
      (current) =>
        current === undefined
          ? current
          : fresh.state === "pending"
            ? current.map((existing) =>
                existing.id === fresh.id ? fresh : existing,
              )
            : current.filter((existing) => existing.id !== fresh.id),
    );
  };

  const dropDeselections = (proposalId: string) => {
    setDeselections((current) => {
      if (!(proposalId in current)) {
        return current;
      }
      return Object.fromEntries(
        Object.entries(current).filter(([id]) => id !== proposalId),
      );
    });
  };

  const toggleDeselected = (proposalId: string, actionId: string) => {
    setDeselections((current) => {
      const existing = current[proposalId] ?? [];
      const next = existing.includes(actionId)
        ? existing.filter((id) => id !== actionId)
        : [...existing, actionId];
      return { ...current, [proposalId]: next };
    });
  };

  const approve = (item: Proposal) => {
    setError(undefined);
    const deselected = deselections()[item.id] ?? [];
    void (async () => {
      try {
        await api.approveProposal(item.id, {
          deselectedActionIds: deselected,
          version: item.version,
        });
        dropDeselections(item.id);
        refresh();
      } catch (caught) {
        if (caught instanceof ApiError && caught.status === 409) {
          setError(await recoverApproveConflict(item, deselected));
          return;
        }
        setError(
          caught instanceof Error
            ? caught.message
            : "Could not approve the proposal",
        );
      }
    })();
  };

  const recoverApproveConflict = async (
    basis: Proposal,
    deselected: string[],
  ): Promise<string | undefined> => {
    const fresh = await api.getProposal(basis.id);
    patchProposalCache(fresh);
    if (fresh.state !== "pending") {
      refresh();
      return undefined;
    }
    if (!sameProposalBasis(basis, fresh)) {
      return "This proposal changed — review it again before approving.";
    }
    try {
      await api.approveProposal(basis.id, {
        deselectedActionIds: deselected,
        version: fresh.version,
      });
      dropDeselections(basis.id);
      refresh();
      return undefined;
    } catch (retryCaught) {
      return retryCaught instanceof Error
        ? retryCaught.message
        : "Could not approve the proposal";
    }
  };

  const startReject = (item: Proposal) => {
    setError(undefined);
    setRejecting({ id: item.id, version: item.version });
    setRejectReason("");
  };

  const cancelReject = () => {
    setRejecting(undefined);
    setRejectReason("");
  };

  const offerRevocations = (proposalId: string, grantIds: string[]) => {
    if (grantIds.length === 0) {
      return;
    }
    setRevocationOffers((current) => ({ ...current, [proposalId]: grantIds }));
  };

  const confirmReject = () => {
    const target = rejecting();
    if (target === undefined) {
      return;
    }
    setError(undefined);
    const reason = rejectReason().trim();
    void (async () => {
      try {
        const result = await api.rejectProposal(target.id, {
          reason: reason.length > 0 ? reason : undefined,
          version: target.version,
        });
        cancelReject();
        offerRevocations(target.id, result.revocable_grant_ids);
        refresh();
      } catch (caught) {
        if (caught instanceof ApiError && caught.status === 409) {
          setError(await recoverRejectConflict(target, reason));
          return;
        }
        setError(
          caught instanceof Error
            ? caught.message
            : "Could not reject the proposal",
        );
      }
    })();
  };

  const recoverRejectConflict = async (
    target: { id: string; version: number },
    reason: string,
  ): Promise<string | undefined> => {
    const fresh = await api.getProposal(target.id);
    patchProposalCache(fresh);
    if (fresh.state !== "pending") {
      refresh();
      return undefined;
    }
    try {
      const result = await api.rejectProposal(target.id, {
        reason: reason.length > 0 ? reason : undefined,
        version: fresh.version,
      });
      offerRevocations(target.id, result.revocable_grant_ids);
      refresh();
      return undefined;
    } catch (retryCaught) {
      return retryCaught instanceof Error
        ? retryCaught.message
        : "Could not reject the proposal";
    }
  };

  const dismissOffer = (proposalId: string) => {
    setRevocationOffers((current) => {
      if (!(proposalId in current)) {
        return current;
      }
      return Object.fromEntries(
        Object.entries(current).filter(([id]) => id !== proposalId),
      );
    });
  };

  const refreshGrants = () => {
    void queryClient.invalidateQueries({
      queryKey: queryKeys.grants,
      refetchType: "none",
    });
    void queryClient.refetchQueries({ queryKey: queryKeys.grants });
  };

  const revoke = (grantId: string) => {
    void (async () => {
      setError(undefined);
      try {
        await api.revokeGrant(grantId);
        refreshGrants();
      } catch (caught) {
        setError(
          caught instanceof Error ? caught.message : "Could not revoke grant",
        );
      }
    })();
  };

  const revokeOffered = (proposalId: string, grantId: string) => {
    void (async () => {
      setError(undefined);
      try {
        await api.revokeGrant(grantId);
      } catch (caught) {
        setError(
          caught instanceof Error ? caught.message : "Could not revoke grant",
        );
        return;
      }
      setRevocationOffers((current) => {
        const remaining = (current[proposalId] ?? []).filter(
          (id) => id !== grantId,
        );
        const withoutOffer = Object.fromEntries(
          Object.entries(current).filter(([id]) => id !== proposalId),
        );
        return remaining.length > 0
          ? { ...withoutOffer, [proposalId]: remaining }
          : withoutOffer;
      });
      refreshGrants();
    })();
  };

  const grantFromSuggestion = (suggestion: GrantSuggestion) => {
    void (async () => {
      setError(undefined);
      try {
        await api.createGrant({
          kind: suggestion.kind,
          scope: suggestion.scope,
        });
        refreshGrants();
        void queryClient.refetchQueries({
          queryKey: queryKeys.grantSuggestions,
        });
      } catch (caught) {
        setError(
          caught instanceof Error ? caught.message : "Could not grant that",
        );
      }
    })();
  };

  return (
    <section
      aria-labelledby="proposals-title"
      class="flex min-h-full flex-1 flex-col"
    >
      <header class="bg-card flex flex-wrap items-center gap-x-4 gap-y-2 border-b px-4 py-3 sm:px-5">
        <h1
          id="proposals-title"
          class="mr-auto text-lg font-semibold tracking-tight"
        >
          Proposals
        </h1>
        <SegmentedControl
          aria-label="Proposals view"
          id="proposals-view"
          onChange={setView}
          options={[
            {
              label: `Queue (${queueItems().length.toString()})`,
              value: "queue",
            },
            {
              label: `Decided (${historyItems().length.toString()})`,
              value: "history",
            },
            {
              label: `Grants (${(grants().length + suggestions().length).toString()})`,
              value: "grants",
            },
          ]}
          value={view()}
        />
      </header>
      <div class="flex-1 overflow-y-auto p-4 sm:p-5">
        <Show when={error()}>
          {(message) => (
            <p class="text-destructive mb-3 text-sm" role="alert">
              {message()}
            </p>
          )}
        </Show>
        <Switch>
          <Match when={view() === "queue"}>
            <div
              aria-labelledby={segmentedTabId("proposals-view", "queue")}
              id={segmentedPanelId("proposals-view", "queue")}
              role="tabpanel"
            >
              <Show
                fallback={
                  <p class="text-muted-foreground text-sm">
                    No pending proposals
                  </p>
                }
                when={queueItems().length > 0}
              >
                <div class="flex min-h-0 flex-1 gap-4 lg:h-[calc(100vh-9rem)]">
                  <ul class="w-full shrink-0 overflow-y-auto rounded-xl border lg:w-80">
                    <For each={queueItems()}>
                      {(item) => (
                        <li>
                          <button
                            aria-current={selectedId() === item.id}
                            class={cx(
                              "flex w-full flex-col gap-1 border-b px-3 py-2.5 text-left text-sm last:border-0",
                              selectedId() === item.id
                                ? "bg-accent"
                                : "hover:bg-accent/50",
                            )}
                            data-id={item.id}
                            onClick={() => {
                              setSelectedId(item.id);
                            }}
                            type="button"
                          >
                            <span class="truncate font-medium">
                              {item.title}
                            </span>
                            <span class="text-muted-foreground truncate text-xs">
                              {`${item.consumer} · ${actionCountLabel(item.actions.length)}`}
                            </span>
                          </button>
                        </li>
                      )}
                    </For>
                  </ul>
                  <div class="hidden min-w-0 flex-1 overflow-y-auto lg:block">
                    <Show
                      fallback={
                        <p class="text-muted-foreground text-sm">
                          Select a proposal to review it.
                        </p>
                      }
                      when={selected()}
                    >
                      {(item) => (
                        <ProposalDetail
                          confirmReject={confirmReject}
                          deselectedIds={deselections()[item().id] ?? []}
                          dismissOffer={dismissOffer}
                          item={item()}
                          onApprove={approve}
                          onCancelReject={cancelReject}
                          onReject={startReject}
                          onToggleAction={toggleDeselected}
                          rejectReason={rejectReason()}
                          rejecting={rejecting()?.id === item().id}
                          revocationOffers={revocationOffers()[item().id] ?? []}
                          revokeOffered={revokeOffered}
                          setRejectReason={setRejectReason}
                        />
                      )}
                    </Show>
                  </div>
                  {/* Narrow-width drill-in: the detail pane replaces the list
                    entirely once a proposal is selected, and a Back control
                    returns to the list. */}
                  <Show when={selected()}>
                    {(item) => (
                      <div class="fixed inset-0 z-30 flex flex-col overflow-y-auto bg-background p-4 lg:hidden">
                        <Button
                          class="mb-3 self-start"
                          onClick={() => {
                            setSelectedId(undefined);
                          }}
                          size="sm"
                          type="button"
                          variant="ghost"
                        >
                          ← Back to queue
                        </Button>
                        <ProposalDetail
                          confirmReject={confirmReject}
                          deselectedIds={deselections()[item().id] ?? []}
                          dismissOffer={dismissOffer}
                          item={item()}
                          onApprove={approve}
                          onCancelReject={cancelReject}
                          onReject={startReject}
                          onToggleAction={toggleDeselected}
                          rejectReason={rejectReason()}
                          rejecting={rejecting()?.id === item().id}
                          revocationOffers={revocationOffers()[item().id] ?? []}
                          revokeOffered={revokeOffered}
                          setRejectReason={setRejectReason}
                        />
                      </div>
                    )}
                  </Show>
                </div>
              </Show>
            </div>
          </Match>
          <Match when={view() === "history"}>
            <div
              aria-labelledby={segmentedTabId("proposals-view", "history")}
              id={segmentedPanelId("proposals-view", "history")}
              role="tabpanel"
            >
              <div class="mb-3 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
                <div>
                  <h2 class="text-base font-semibold">
                    {`Decided proposals (${historyItems().length.toString()})`}
                  </h2>
                  <p class="text-muted-foreground text-xs">
                    {rangeSummary(
                      filteredHistoryItems().length,
                      historyItems().length,
                      historyPage(),
                      "proposal",
                    )}
                  </p>
                </div>
                <div class="flex flex-col gap-2 sm:flex-row">
                  <label class="text-muted-foreground text-xs font-medium">
                    Search decided proposals
                    <input
                      aria-label="Search decided proposals"
                      class="border-input bg-background mt-1 h-9 w-full rounded-md border px-3 text-sm sm:w-64"
                      onInput={(event) => {
                        setHistorySearch(event.currentTarget.value);
                        setHistoryPage(1);
                      }}
                      placeholder="Title, count, reason…"
                      type="search"
                      value={historySearch()}
                    />
                  </label>
                  <label class="text-muted-foreground text-xs font-medium">
                    State
                    <select
                      aria-label="Filter decided proposals by state"
                      class="border-input bg-background mt-1 h-9 rounded-md border px-2 text-sm"
                      onChange={(event) => {
                        setHistoryState(
                          event.currentTarget.value as HistoryStateFilter,
                        );
                        setHistoryPage(1);
                      }}
                      value={historyState()}
                    >
                      <option value="all">All states</option>
                      <option value="approved">Approved</option>
                      <option value="executing">Executing</option>
                      <option value="executed">Executed</option>
                      <option value="failed">Failed</option>
                      <option value="rejected">Rejected</option>
                    </select>
                  </label>
                </div>
              </div>
              <Show
                fallback={
                  <p class="text-muted-foreground text-sm">
                    No decided proposals match.
                  </p>
                }
                when={visibleHistoryItems().length > 0}
              >
                <ul class="space-y-2">
                  <For each={visibleHistoryItems()}>
                    {(item) => (
                      <HistoryProposalItem
                        item={item}
                        searchTerm={historySearch().trim().toLocaleLowerCase()}
                      />
                    )}
                  </For>
                </ul>
              </Show>
              <Pager
                label="Decided proposals"
                onPage={setHistoryPage}
                page={boundedPage(historyPage(), filteredHistoryItems().length)}
                total={filteredHistoryItems().length}
              />
            </div>
          </Match>
          <Match when={view() === "grants"}>
            <div
              aria-labelledby={segmentedTabId("proposals-view", "grants")}
              id={segmentedPanelId("proposals-view", "grants")}
              role="tabpanel"
            >
              <section aria-labelledby="active-grants-title">
                <div class="mb-3 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
                  <div>
                    <h2
                      class="text-base font-semibold"
                      id="active-grants-title"
                    >
                      {`Active grants (${grants().length.toString()})`}
                    </h2>
                    <p class="text-muted-foreground text-xs">
                      {rangeSummary(
                        filteredGrants().length,
                        grants().length,
                        grantPage(),
                        "grant",
                      )}
                    </p>
                  </div>
                  <label class="text-muted-foreground text-xs font-medium">
                    Search active grants
                    <input
                      aria-label="Search active grants"
                      class="border-input bg-background mt-1 h-9 w-full rounded-md border px-3 text-sm sm:w-64"
                      onInput={(event) => {
                        setGrantSearch(event.currentTarget.value);
                        setGrantPage(1);
                      }}
                      placeholder="Kind or scope…"
                      type="search"
                      value={grantSearch()}
                    />
                  </label>
                </div>
                <Show
                  fallback={
                    <p class="text-muted-foreground mt-2 text-sm">
                      No active grants match.
                    </p>
                  }
                  when={visibleGrants().length > 0}
                >
                  <ul class="mt-2 space-y-2">
                    <For each={visibleGrants()}>
                      {(g) => (
                        <li
                          aria-label={grantLabel(g.kind, g.scope)}
                          class="bg-muted flex items-center gap-2 rounded-md border px-3 py-2 text-sm"
                          data-id={g.id}
                        >
                          <GrantSummary grant={g} />
                          <Button
                            aria-label={`Revoke ${grantLabel(g.kind, g.scope)}`}
                            onClick={() => {
                              revoke(g.id);
                            }}
                            size="sm"
                            type="button"
                            variant="ghost"
                          >
                            Revoke
                          </Button>
                        </li>
                      )}
                    </For>
                  </ul>
                </Show>
                <Pager
                  label="Active grants"
                  onPage={setGrantPage}
                  page={boundedPage(grantPage(), filteredGrants().length)}
                  total={filteredGrants().length}
                />
              </section>
              <section aria-labelledby="grant-suggestions-title" class="mt-5">
                <div class="mb-3 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
                  <div>
                    <h2
                      class="text-base font-semibold"
                      id="grant-suggestions-title"
                    >
                      {`Suggestions (${suggestions().length.toString()})`}
                    </h2>
                    <p class="text-muted-foreground text-xs">
                      {rangeSummary(
                        filteredSuggestions().length,
                        suggestions().length,
                        grantPage(),
                        "suggestion",
                      )}
                    </p>
                  </div>
                  <label class="text-muted-foreground text-xs font-medium">
                    Search grant suggestions
                    <input
                      aria-label="Search grant suggestions"
                      class="border-input bg-background mt-1 h-9 w-full rounded-md border px-3 text-sm sm:w-64"
                      onInput={(event) => {
                        setSuggestionSearch(event.currentTarget.value);
                        setGrantPage(1);
                      }}
                      placeholder="Kind or scope…"
                      type="search"
                      value={suggestionSearch()}
                    />
                  </label>
                </div>
                <Show
                  fallback={
                    <p class="text-muted-foreground mt-2 text-sm">
                      No suggestions match.
                    </p>
                  }
                  when={visibleSuggestions().length > 0}
                >
                  <ul class="mt-2 space-y-2">
                    <For each={visibleSuggestions()}>
                      {(s) => (
                        <li
                          aria-label={suggestionLabel(s.kind, s.scope)}
                          class="bg-muted flex items-center gap-2 rounded-md border px-3 py-2 text-sm"
                        >
                          <span class="flex-1">
                            <span class="font-medium">{s.kind}</span>
                            <Show when={s.scope}>
                              {(scope) => (
                                <span class="text-muted-foreground">
                                  {` · ${scope()}`}
                                </span>
                              )}
                            </Show>
                            <span class="text-muted-foreground block text-xs">
                              {`seen ${s.seen.toString()} · approved ${s.approved.toString()} · rejected ${s.rejected.toString()} · edited ${s.edited.toString()}`}
                            </span>
                          </span>
                          <Button
                            aria-label={grantSuggestionActionLabel(
                              s.kind,
                              s.scope,
                            )}
                            onClick={() => {
                              grantFromSuggestion(s);
                            }}
                            size="sm"
                            type="button"
                          >
                            Grant
                          </Button>
                        </li>
                      )}
                    </For>
                  </ul>
                </Show>
                <Pager
                  label="Grant suggestions"
                  onPage={setGrantPage}
                  page={boundedPage(grantPage(), filteredSuggestions().length)}
                  total={filteredSuggestions().length}
                />
              </section>
            </div>
          </Match>
        </Switch>
      </div>
    </section>
  );
}

function rangeNoun(noun: string, count: number): string {
  return count === 1 ? noun : `${noun}s`;
}

function rangeSummary(
  filtered: number,
  total: number,
  page: number,
  noun = "item",
): string {
  if (total === 0) {
    return "No items";
  }
  if (filtered === 0) {
    return `No matching ${rangeNoun(noun, 0)} (${total.toString()} total)`;
  }
  const safePage = boundedPage(page, filtered);
  const start = (safePage - 1) * PAGE_SIZE + 1;
  const end = Math.min(safePage * PAGE_SIZE, filtered);
  const base = `Showing ${start.toString()}-${end.toString()} of ${filtered.toString()}`;
  return filtered === total
    ? base
    : `${base} matching ${rangeNoun(noun, filtered)} (${total.toString()} total)`;
}

function Pager(props: {
  label: string;
  onPage: (page: number) => void;
  page: number;
  total: number;
}) {
  return (
    <Show when={props.total > PAGE_SIZE}>
      <nav
        aria-label={`${props.label} pages`}
        class="mt-3 flex items-center justify-between gap-2 text-sm"
      >
        <Button
          disabled={props.page <= 1}
          onClick={() => {
            props.onPage(props.page - 1);
          }}
          size="sm"
          type="button"
          variant="outline"
        >
          Previous
        </Button>
        <span class="text-muted-foreground text-xs">
          {`Page ${props.page.toString()} of ${pageCount(props.total).toString()}`}
        </span>
        <Button
          disabled={props.page >= pageCount(props.total)}
          onClick={() => {
            props.onPage(props.page + 1);
          }}
          size="sm"
          type="button"
          variant="outline"
        >
          Next
        </Button>
      </nav>
    </Show>
  );
}

function GrantSummary(props: { grant: Grant }) {
  return (
    <span class="flex-1">
      <span class="font-medium">{props.grant.kind}</span>
      <Show when={props.grant.scope}>
        {(scope) => (
          <span class="text-muted-foreground">{` · ${scope()}`}</span>
        )}
      </Show>
      <span class="text-muted-foreground block text-xs">
        {`granted ${formatWhen(props.grant.granted_at)}`}
      </span>
    </span>
  );
}

function HistoryProposalItem(props: { item: Proposal; searchTerm: string }) {
  const matchedActions = createMemo(() =>
    historyActionMatchLabels(props.item, props.searchTerm),
  );
  return (
    <li
      aria-label={`Proposal: ${props.item.title}`}
      class="bg-muted rounded-md border px-3 py-2 text-sm"
      data-id={props.item.id}
    >
      <div class="flex items-center justify-between gap-2">
        <span class="font-medium">{props.item.title}</span>
        <span class="text-muted-foreground text-xs">
          {`${props.item.state} · ${actionCountLabel(props.item.actions.length)}`}
        </span>
      </div>
      <p class="text-muted-foreground text-xs">
        {decidedTimestampLabel(props.item)}
      </p>
      <Show when={props.item.rejection_reason}>
        {(reason) => (
          <p class="text-muted-foreground text-xs">{`Reason: ${reason()}`}</p>
        )}
      </Show>
      <Show when={matchedActions().length > 0}>
        <ul class="mt-1 space-y-1">
          <For each={matchedActions()}>
            {(label) => (
              <li class="text-muted-foreground text-xs">
                {`Matched action: ${label}`}
              </li>
            )}
          </For>
        </ul>
      </Show>
    </li>
  );
}

function ProposalDetail(props: {
  confirmReject: () => void;
  deselectedIds: string[];
  dismissOffer: (proposalId: string) => void;
  item: Proposal;
  onApprove: (item: Proposal) => void;
  onCancelReject: () => void;
  onReject: (item: Proposal) => void;
  onToggleAction: (proposalId: string, actionId: string) => void;
  rejectReason: string;
  rejecting: boolean;
  revocationOffers: string[];
  revokeOffered: (proposalId: string, grantId: string) => void;
  setRejectReason: (value: string) => void;
}) {
  return (
    <div
      aria-label={`Proposal: ${props.item.title}`}
      class="bg-card flex flex-col gap-4 rounded-xl border p-4 shadow-sm"
      data-id={props.item.id}
    >
      <div>
        <h2 class="text-lg font-semibold">{props.item.title}</h2>
        <p class="text-muted-foreground mt-1 text-sm">{props.item.summary}</p>
      </div>
      <div class="flex flex-col gap-2">
        <h3 class="text-sm font-semibold">
          Actions ({props.item.actions.length})
        </h3>
        <ul class="space-y-2">
          <For each={props.item.actions}>
            {(action) => (
              <li class="rounded-lg border p-3 text-sm">
                <label class="flex items-start gap-2">
                  <input
                    checked={!props.deselectedIds.includes(action.id)}
                    onChange={() => {
                      props.onToggleAction(props.item.id, action.id);
                    }}
                    type="checkbox"
                  />
                  <span class="flex-1">
                    <span class="block font-medium">
                      {actionPrimary(action)}
                    </span>
                    <details class="mt-1">
                      <summary class="text-muted-foreground cursor-pointer text-[11px] select-none">
                        Details
                      </summary>
                      <pre class="bg-muted/40 mt-1 max-h-40 overflow-auto rounded px-2 py-1 font-mono text-[11px] break-words whitespace-pre-wrap">
                        {JSON.stringify(action.params, null, 2)}
                      </pre>
                    </details>
                  </span>
                </label>
              </li>
            )}
          </For>
        </ul>
      </div>
      <div class="flex flex-wrap items-center gap-2 border-t pt-3">
        <Button
          aria-label={`Approve proposal ${props.item.title}`}
          onClick={() => {
            props.onApprove(props.item);
          }}
          size="sm"
          type="button"
        >
          Approve
        </Button>
        <Button
          aria-label={`Reject proposal ${props.item.title}`}
          onClick={() => {
            props.onReject(props.item);
          }}
          size="sm"
          type="button"
          variant="outline"
        >
          Reject
        </Button>
      </div>
      <Show when={props.rejecting}>
        <div class="space-y-2">
          <TextField
            onChange={props.setRejectReason}
            value={props.rejectReason}
          >
            <TextFieldLabel>Reason (optional)</TextFieldLabel>
            <TextFieldInput name="reason" />
          </TextField>
          <div class="flex justify-end gap-2">
            <Button onClick={props.confirmReject} size="sm" type="button">
              Confirm reject
            </Button>
            <Button
              onClick={props.onCancelReject}
              size="sm"
              type="button"
              variant="ghost"
            >
              Cancel
            </Button>
          </div>
        </div>
      </Show>
      <Show when={props.revocationOffers.length > 0}>
        <div class="border-t pt-2">
          <p class="text-xs">Revoke the grants used for this?</p>
          <div class="mt-1 flex flex-wrap gap-2">
            <For each={props.revocationOffers}>
              {(grantId) => (
                <Button
                  onClick={() => {
                    props.revokeOffered(props.item.id, grantId);
                  }}
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  {`Revoke ${grantId.slice(0, 8)}`}
                </Button>
              )}
            </For>
            <Button
              onClick={() => {
                props.dismissOffer(props.item.id);
              }}
              size="sm"
              type="button"
              variant="ghost"
            >
              Dismiss
            </Button>
          </div>
        </div>
      </Show>
    </div>
  );
}
