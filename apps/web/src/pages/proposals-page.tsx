import { createQuery, useQueryClient } from "@tanstack/solid-query";
import { For, Match, Show, Switch, createMemo, createSignal } from "solid-js";

import { useAppContext } from "../app-context";
import { ApiError } from "../api";
import type { GrantSuggestion, Proposal, ProposalAction } from "../api";
import { SegmentedControl } from "../components/segmented-control";
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

function grantLabel(kind: string, scope: string | null): string {
  return scope === null ? `Grant: ${kind}` : `Grant: ${kind} (${scope})`;
}

function suggestionLabel(kind: string, scope: string | null): string {
  return scope === null
    ? `Suggestion: ${kind}`
    : `Suggestion: ${kind} (${scope})`;
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

function formatWhen(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : formatDateTime(parsed);
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

export function ProposalsPage() {
  const { api } = useAppContext();
  const queryClient = useQueryClient();
  const [view, setView] = createSignal<ProposalsView>("queue");
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

  const queueQuery = createQuery(() => ({
    queryFn: () => api.listProposals("pending"),
    queryKey: queryKeys.proposalsState("pending"),
  }));
  const historyQuery = createQuery(() => ({
    enabled: view() === "history",
    queryFn: () => api.listProposals(),
    queryKey: queryKeys.proposalsAll,
  }));
  const grantsQuery = createQuery(() => ({
    enabled: view() === "grants",
    queryFn: () => api.listGrants(),
    queryKey: queryKeys.grants,
  }));
  const suggestionsQuery = createQuery(() => ({
    enabled: view() === "grants",
    queryFn: () => api.grantSuggestions(),
    queryKey: queryKeys.grantSuggestions,
  }));

  const historyItems = createMemo(() =>
    (historyQuery.data ?? []).filter((item) => item.state !== "pending"),
  );

  const queueItems = createMemo(() => queueQuery.data ?? []);
  const selected = createMemo(() =>
    queueItems().find((item) => item.id === selectedId()),
  );

  const refresh = () => {
    void queryClient.invalidateQueries({
      queryKey: queryKeys.proposals,
      refetchType: "none",
    });
    void queryClient.refetchQueries({
      queryKey: queryKeys.proposalsState("pending"),
    });
    if (view() === "history") {
      void queryClient.refetchQueries({ queryKey: queryKeys.proposalsAll });
    }
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
    <main
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
          onChange={setView}
          options={[
            { label: "Queue", value: "queue" },
            { label: "Decided", value: "history" },
            { label: "Grants", value: "grants" },
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
                          <span class="truncate font-medium">{item.title}</span>
                          <span class="text-muted-foreground truncate text-xs">
                            {`${item.consumer} · ${item.actions.length.toString()} action${item.actions.length === 1 ? "" : "s"}`}
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
          </Match>
          <Match when={view() === "history"}>
            <Show
              fallback={
                <p class="text-muted-foreground text-sm">
                  No decided proposals yet
                </p>
              }
              when={historyItems().length > 0}
            >
              <ul class="space-y-2">
                <For each={historyItems()}>
                  {(item) => (
                    <li
                      aria-label={`Proposal: ${item.title}`}
                      class="bg-muted rounded-md border px-3 py-2 text-sm"
                      data-id={item.id}
                    >
                      <div class="flex items-center justify-between gap-2">
                        <span class="font-medium">{item.title}</span>
                        <span class="text-muted-foreground text-xs">
                          {item.state}
                        </span>
                      </div>
                      <p class="text-muted-foreground text-xs">
                        {item.decided_at
                          ? formatWhen(item.decided_at)
                          : "not decided"}
                      </p>
                      <Show when={item.rejection_reason}>
                        {(reason) => (
                          <p class="text-muted-foreground text-xs">
                            {`Reason: ${reason()}`}
                          </p>
                        )}
                      </Show>
                    </li>
                  )}
                </For>
              </ul>
            </Show>
          </Match>
          <Match when={view() === "grants"}>
            <div>
              <h2 class="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
                Active grants
              </h2>
              <Show
                fallback={
                  <p class="text-muted-foreground mt-2 text-sm">
                    No active grants
                  </p>
                }
                when={(grantsQuery.data ?? []).length > 0}
              >
                <ul class="mt-2 space-y-2">
                  <For each={grantsQuery.data ?? []}>
                    {(g) => (
                      <li
                        aria-label={grantLabel(g.kind, g.scope)}
                        class="bg-muted flex items-center gap-2 rounded-md border px-3 py-2 text-sm"
                        data-id={g.id}
                      >
                        <span class="flex-1">
                          <span class="font-medium">{g.kind}</span>
                          <Show when={g.scope}>
                            {(scope) => (
                              <span class="text-muted-foreground">
                                {` · ${scope()}`}
                              </span>
                            )}
                          </Show>
                          <span class="text-muted-foreground block text-xs">
                            {`granted ${formatWhen(g.granted_at)}`}
                          </span>
                        </span>
                        <Button
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
            </div>
            <div class="mt-4">
              <h2 class="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
                Suggestions
              </h2>
              <Show
                fallback={
                  <p class="text-muted-foreground mt-2 text-sm">
                    No suggestions yet
                  </p>
                }
                when={(suggestionsQuery.data ?? []).length > 0}
              >
                <ul class="mt-2 space-y-2">
                  <For each={suggestionsQuery.data ?? []}>
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
            </div>
          </Match>
        </Switch>
      </div>
    </main>
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
          onClick={() => {
            props.onApprove(props.item);
          }}
          size="sm"
          type="button"
        >
          Approve
        </Button>
        <Button
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
