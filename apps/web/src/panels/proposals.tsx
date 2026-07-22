import { createQuery, useQueryClient } from "@tanstack/solid-query";
import { For, Match, Show, Switch, createMemo, createSignal } from "solid-js";

import { ApiError } from "../api";
import type {
  GrantSuggestion,
  Proposal,
  ProposalAction,
  TetherApi,
} from "../api";
import { formatDateTime } from "../lib/format";
import { panelClass } from "../lib/panel";
import { queryKeys } from "../lib/query-keys";
import { Button } from "@/components/ui/button";
import {
  TextField,
  TextFieldInput,
  TextFieldLabel,
} from "@/components/ui/text-field";

// Queue: pending proposals awaiting a human decision. History: every decided
// proposal (approved/executing/executed/failed/rejected), for auditing what
// actually ran. Grants: the standing autonomy grants plus read-time
// calibration suggestions for categories with a track record.
type ProposalsView = "queue" | "history" | "grants";

function proposalLabel(title: string): string {
  return `Proposal: ${title}`;
}

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

export function ProposalsPanel(props: { api: TetherApi }) {
  const queryClient = useQueryClient();
  const [view, setView] = createSignal<ProposalsView>("queue");
  const [expanded, setExpanded] = createSignal<string | undefined>();
  // Action ids unticked per proposal, keyed by proposal id, before approval.
  const [deselections, setDeselections] = createSignal<
    Record<string, string[]>
  >({});
  const [error, setError] = createSignal<string | undefined>();
  // The proposal mid-reject: its observed version and a draft reason.
  const [rejecting, setRejecting] = createSignal<
    { id: string; version: number } | undefined
  >();
  const [rejectReason, setRejectReason] = createSignal("");
  // Grant ids offered for revocation after a reject (user story 15: offered,
  // never forced), keyed by the proposal that produced the offer.
  const [revocationOffers, setRevocationOffers] = createSignal<
    Record<string, string[]>
  >({});

  const queueQuery = createQuery(() => ({
    queryFn: () => props.api.listProposals("pending"),
    queryKey: queryKeys.proposalsState("pending"),
  }));
  const historyQuery = createQuery(() => ({
    enabled: view() === "history",
    queryFn: () => props.api.listProposals(),
    queryKey: queryKeys.proposalsAll,
  }));
  const grantsQuery = createQuery(() => ({
    enabled: view() === "grants",
    queryFn: () => props.api.listGrants(),
    queryKey: queryKeys.grants,
  }));
  const suggestionsQuery = createQuery(() => ({
    enabled: view() === "grants",
    queryFn: () => props.api.grantSuggestions(),
    queryKey: queryKeys.grantSuggestions,
  }));

  const historyItems = createMemo(() =>
    (historyQuery.data ?? []).filter((item) => item.state !== "pending"),
  );

  const refresh = () => {
    // Mark every proposals query stale but only refetch what is on screen —
    // an invalidate covers the queue, history and grants queries alike (they
    // all nest under the "proposals" prefix), but the disabled ones refetch
    // when their view is next looked at.
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

  // Patches the on-screen pending list with a freshly refetched row — used
  // while recovering from a 409 so the row reflects the current server state
  // even before the next full refresh.
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
        await props.api.approveProposal(item.id, {
          deselectedActionIds: deselected,
          version: item.version,
        });
        dropDeselections(item.id);
        refresh();
      } catch (caught) {
        // Same optimistic-concurrency race as elsewhere: something (the
        // agent producing a sibling proposal, another tab) touched this
        // proposal after we loaded it. Refetch and decide whether the change
        // is a mere version bump (safe to retry) or a genuine content change
        // (must stop and let the human re-review before approving unseen
        // actions).
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
    const fresh = await props.api.getProposal(basis.id);
    patchProposalCache(fresh);
    if (fresh.state !== "pending") {
      // Already decided elsewhere (e.g. approved from another tab) — the
      // intent is settled, nothing left to approve here.
      refresh();
      return undefined;
    }
    if (!sameProposalBasis(basis, fresh)) {
      return "This proposal changed — review it again before approving.";
    }
    try {
      await props.api.approveProposal(basis.id, {
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
        const result = await props.api.rejectProposal(target.id, {
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

  // Reject stays blind on a 409, same as the memories panel: rejecting
  // changed content is safe (there is nothing left to vouch for), so this
  // refetches the current version and retries once rather than surfacing
  // the conflict.
  const recoverRejectConflict = async (
    target: { id: string; version: number },
    reason: string,
  ): Promise<string | undefined> => {
    const fresh = await props.api.getProposal(target.id);
    patchProposalCache(fresh);
    if (fresh.state !== "pending") {
      refresh();
      return undefined;
    }
    try {
      const result = await props.api.rejectProposal(target.id, {
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
        await props.api.revokeGrant(grantId);
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
        await props.api.revokeGrant(grantId);
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
        await props.api.createGrant({
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

  const proposalRow = (item: Proposal) => (
    <li
      aria-label={proposalLabel(item.title)}
      class="bg-muted rounded-md border px-3 py-2 text-sm"
    >
      <button
        aria-expanded={expanded() === item.id}
        class="flex w-full items-center gap-2 text-left"
        onClick={() => {
          setExpanded((current) => (current === item.id ? undefined : item.id));
        }}
        type="button"
      >
        <span aria-hidden="true" class="text-[0.6rem]">
          {expanded() === item.id ? "▾" : "▸"}
        </span>
        <span class="font-medium">{item.title}</span>
        <span class="text-muted-foreground text-xs">
          {`${item.consumer} · ${item.actions.length.toString()} action${item.actions.length === 1 ? "" : "s"}${item.state === "pending" ? "" : ` · ${item.state}`}`}
        </span>
      </button>
      <Show when={expanded() === item.id}>
        <div class="mt-2 space-y-2">
          <p class="text-muted-foreground text-xs">{item.summary}</p>
          <ul class="space-y-1">
            <For each={item.actions}>
              {(action) => (
                <li class="rounded border px-2 py-1 text-xs">
                  <label class="flex items-start gap-2">
                    <Show when={item.state === "pending"}>
                      <input
                        checked={
                          !(deselections()[item.id] ?? []).includes(action.id)
                        }
                        onChange={() => {
                          toggleDeselected(item.id, action.id);
                        }}
                        type="checkbox"
                      />
                    </Show>
                    <span class="flex-1">
                      <span class="block font-medium">
                        {actionPrimary(action)}
                      </span>
                      <Show when={action.display !== null}>
                        <span class="text-muted-foreground block text-[11px]">
                          <span class="font-medium">{action.kind}</span>
                          <Show when={action.scope}>
                            {(scope) => ` · ${scope()}`}
                          </Show>
                        </span>
                      </Show>
                      <details class="mt-1">
                        <summary class="text-muted-foreground cursor-pointer text-[11px] select-none">
                          Details
                        </summary>
                        <pre class="bg-background/40 mt-1 max-h-40 overflow-auto rounded px-2 py-1 font-mono text-[11px] break-words whitespace-pre-wrap">
                          {JSON.stringify(action.params, null, 2)}
                        </pre>
                      </details>
                      <Show when={item.state !== "pending"}>
                        <p class="text-muted-foreground mt-1">
                          {`${action.disposition}${action.outcome !== null ? ` · ${action.outcome}` : ""}`}
                          <Show when={action.outcome_detail}>
                            {(detail) => ` — ${detail()}`}
                          </Show>
                        </p>
                      </Show>
                    </span>
                  </label>
                </li>
              )}
            </For>
          </ul>
          <Show when={item.state === "pending"}>
            <div class="flex flex-wrap items-center gap-2">
              <Button
                onClick={() => {
                  approve(item);
                }}
                size="sm"
                type="button"
              >
                Approve
              </Button>
              <Button
                onClick={() => {
                  startReject(item);
                }}
                size="sm"
                type="button"
                variant="ghost"
              >
                Reject
              </Button>
            </div>
            <Show when={rejecting()?.id === item.id}>
              <div class="space-y-2">
                <TextField onChange={setRejectReason} value={rejectReason()}>
                  <TextFieldLabel>Reason (optional)</TextFieldLabel>
                  <TextFieldInput name="reason" />
                </TextField>
                <div class="flex justify-end gap-2">
                  <Button onClick={confirmReject} size="sm" type="button">
                    Confirm reject
                  </Button>
                  <Button
                    onClick={cancelReject}
                    size="sm"
                    type="button"
                    variant="ghost"
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            </Show>
          </Show>
          <Show when={item.state !== "pending"}>
            <p class="text-muted-foreground text-xs">
              {item.decided_at
                ? `${item.state} · ${formatWhen(item.decided_at)}`
                : item.state}
            </p>
            <Show when={item.rejection_reason}>
              {(reason) => (
                <p class="text-muted-foreground text-xs">
                  {`Reason: ${reason()}`}
                </p>
              )}
            </Show>
          </Show>
          <Show when={(revocationOffers()[item.id] ?? []).length > 0}>
            <div class="border-t pt-2">
              <p class="text-xs">Revoke the grants used for this?</p>
              <div class="mt-1 flex flex-wrap gap-2">
                <For each={revocationOffers()[item.id]}>
                  {(grantId) => (
                    <Button
                      onClick={() => {
                        revokeOffered(item.id, grantId);
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
                    dismissOffer(item.id);
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
      </Show>
    </li>
  );

  return (
    <section aria-label="Proposals" class={panelClass}>
      <div class="mb-3 flex items-center justify-between">
        <h2 class="text-sm font-semibold">Proposals</h2>
        <div class="flex gap-1" role="group" aria-label="Proposals view">
          <For each={["queue", "history", "grants"] as const}>
            {(candidate) => (
              <Button
                aria-pressed={view() === candidate}
                onClick={() => {
                  setView(candidate);
                }}
                size="sm"
                type="button"
                variant={view() === candidate ? "secondary" : "ghost"}
              >
                {candidate === "queue"
                  ? "Queue"
                  : candidate === "history"
                    ? "Decided"
                    : "Grants"}
              </Button>
            )}
          </For>
        </div>
      </div>
      <Switch>
        <Match when={view() === "queue"}>
          <Show
            fallback={
              <p class="text-muted-foreground text-sm">No pending proposals</p>
            }
            when={(queueQuery.data ?? []).length > 0}
          >
            <ul class="space-y-2">
              <For each={queueQuery.data ?? []}>{proposalRow}</For>
            </ul>
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
              <For each={historyItems()}>{proposalRow}</For>
            </ul>
          </Show>
        </Match>
        <Match when={view() === "grants"}>
          <div>
            <h3 class="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
              Active grants
            </h3>
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
            <h3 class="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
              Suggestions
            </h3>
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
                        <Show when={s.last_rejection}>
                          {(when) => (
                            <span class="text-muted-foreground block text-xs">
                              {`last rejection ${formatWhen(when())}`}
                            </span>
                          )}
                        </Show>
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
      <Show when={error()}>
        {(message) => (
          <p class="text-destructive mt-2 text-sm" role="alert">
            {message()}
          </p>
        )}
      </Show>
    </section>
  );
}
