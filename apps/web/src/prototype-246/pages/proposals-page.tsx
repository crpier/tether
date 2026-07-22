// PROTOTYPE #246 — throwaway, do not ship
//
// Proposals page: queue / history / grants sub-views. The variant switch
// (?variant=A|B|C) only changes how the *queue* sub-view lays out pending
// proposals — history and grants stay a single simple shape, since the open
// question is specifically "how to show enough of the pending queue at once
// to decide". Approve/reject are stubs: they just mark the item locally.

import { For, Match, Show, Switch, createMemo, createSignal } from "solid-js";

import {
  mockGrantSuggestions,
  mockGrants,
  mockProposalHistory,
  mockProposals,
} from "../mock-data";
import { variant } from "../store";
import type { MockProposalHistoryEntry } from "../types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { panelClass } from "@/lib/panel";
import { cx } from "@/lib/cva";

type SubView = "queue" | "history" | "grants";
type Decision = "approved" | "rejected";

function confidenceTone(confidence: number): string {
  if (confidence >= 0.85) return "text-emerald-600 dark:text-emerald-400";
  if (confidence >= 0.65) return "text-amber-600 dark:text-amber-400";
  return "text-red-600 dark:text-red-400";
}

function historyTone(state: MockProposalHistoryEntry["state"]): string {
  switch (state) {
    case "executed":
      return "text-emerald-600 dark:text-emerald-400";
    case "approved":
      return "text-blue-600 dark:text-blue-400";
    case "rejected":
      return "text-muted-foreground";
    case "failed":
      return "text-red-600 dark:text-red-400";
  }
}

function DiscussButton() {
  return (
    <button
      class="rounded border px-1.5 py-0.5 text-[10px] text-muted-foreground hover:bg-accent"
      title="Discuss in chat (non-functional in prototype)"
      type="button"
    >
      Discuss →
    </button>
  );
}

// ---- Variant A: master-detail ------------------------------------

function VariantAQueue(props: {
  decisions: Partial<Record<string, Decision>>;
  decide: (id: string, decision: Decision) => void;
}) {
  const pending = createMemo(() =>
    mockProposals.filter((p) => !(p.id in props.decisions)),
  );
  const [selectedId, setSelectedId] = createSignal<string | null>(
    mockProposals[0]?.id ?? null,
  );
  const selected = createMemo(() =>
    mockProposals.find((p) => p.id === selectedId()),
  );

  return (
    <div class="flex h-full min-h-0 gap-4">
      <div class="w-80 shrink-0 overflow-y-auto rounded-xl border">
        <For each={mockProposals}>
          {(proposal) => {
            const decision = () => props.decisions[proposal.id];
            return (
              <button
                class={cx(
                  "flex w-full flex-col gap-1 border-b px-3 py-2.5 text-left text-sm last:border-0",
                  selectedId() === proposal.id
                    ? "bg-accent"
                    : "hover:bg-accent/50",
                  decision() && "opacity-50",
                )}
                onClick={() => setSelectedId(proposal.id)}
                type="button"
              >
                <div class="flex items-center justify-between gap-2">
                  <span class="truncate font-medium">{proposal.title}</span>
                  <span
                    class={cx(
                      "text-xs font-semibold",
                      confidenceTone(proposal.confidence),
                    )}
                  >
                    {Math.round(proposal.confidence * 100)}%
                  </span>
                </div>
                <span class="truncate text-xs text-muted-foreground">
                  {proposal.actions.length} action
                  {proposal.actions.length === 1 ? "" : "s"}
                  {decision() ? ` · ${String(decision())}` : ""}
                </span>
              </button>
            );
          }}
        </For>
      </div>

      <div class="min-w-0 flex-1 overflow-y-auto">
        <Show
          fallback={
            <p class="text-sm text-muted-foreground">No proposal selected.</p>
          }
          when={selected()}
        >
          {(proposal) => (
            <div class={cx(panelClass, "flex flex-col gap-4")}>
              <div class="flex items-start justify-between gap-3">
                <div>
                  <h2 class="text-lg font-semibold">{proposal().title}</h2>
                  <p class="mt-1 text-sm text-muted-foreground">
                    {proposal().summary}
                  </p>
                </div>
                <span
                  class={cx(
                    "text-sm font-semibold",
                    confidenceTone(proposal().confidence),
                  )}
                >
                  {Math.round(proposal().confidence * 100)}% confidence
                </span>
              </div>

              <div class="flex flex-col gap-2">
                <h3 class="text-sm font-semibold">
                  Actions ({proposal().actions.length})
                </h3>
                <For each={proposal().actions}>
                  {(action) => (
                    <div class="rounded-lg border p-3 text-sm">
                      <div class="flex items-center justify-between gap-2">
                        <span class="font-medium">{action.display}</span>
                        <Badge variant="outline">{action.kind}</Badge>
                      </div>
                      <p class="mt-1 text-xs text-muted-foreground">
                        {action.sender} · "{action.subject}" · {action.ageDays}d
                        old · {action.count} msg{action.count === 1 ? "" : "s"}
                      </p>
                    </div>
                  )}
                </For>
              </div>

              <div class="flex items-center gap-2 border-t pt-3">
                <Button
                  disabled={pending().every((p) => p.id !== proposal().id)}
                  onClick={() => props.decide(proposal().id, "approved")}
                  size="sm"
                >
                  Approve
                </Button>
                <Button
                  disabled={pending().every((p) => p.id !== proposal().id)}
                  onClick={() => props.decide(proposal().id, "rejected")}
                  size="sm"
                  variant="outline"
                >
                  Reject
                </Button>
                <DiscussButton />
              </div>
            </div>
          )}
        </Show>
      </div>
    </div>
  );
}

// ---- Variant B: full-width stacked cards --------------------------

function VariantBQueue(props: {
  decisions: Partial<Record<string, Decision>>;
  decide: (id: string, decision: Decision) => void;
}) {
  return (
    <div class="flex flex-col gap-4">
      <For each={mockProposals}>
        {(proposal) => {
          const decision = () => props.decisions[proposal.id];
          return (
            <div
              class={cx(
                panelClass,
                "flex flex-col gap-3",
                decision() && "opacity-60",
              )}
            >
              <div class="flex items-start justify-between gap-3">
                <div>
                  <h2 class="font-semibold">{proposal.title}</h2>
                  <p class="mt-1 text-sm text-muted-foreground">
                    {proposal.summary}
                  </p>
                </div>
                <span
                  class={cx(
                    "shrink-0 text-sm font-semibold",
                    confidenceTone(proposal.confidence),
                  )}
                >
                  {Math.round(proposal.confidence * 100)}%
                </span>
              </div>

              <div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
                <For each={proposal.actions}>
                  {(action) => (
                    <div class="rounded-lg border p-2.5 text-sm">
                      <div class="flex items-center justify-between gap-2">
                        <span class="font-medium">{action.display}</span>
                        <Badge variant="outline">{action.kind}</Badge>
                      </div>
                      <p class="mt-1 text-xs text-muted-foreground">
                        {action.sender} · "{action.subject}" · {action.ageDays}d
                        old · {action.count} msg{action.count === 1 ? "" : "s"}
                      </p>
                    </div>
                  )}
                </For>
              </div>

              <div class="flex items-center gap-2 border-t pt-3">
                <Show
                  fallback={
                    <span class="text-sm capitalize text-muted-foreground">
                      {decision()}
                    </span>
                  }
                  when={!decision()}
                >
                  <Button
                    onClick={() => props.decide(proposal.id, "approved")}
                    size="sm"
                  >
                    Approve
                  </Button>
                  <Button
                    onClick={() => props.decide(proposal.id, "rejected")}
                    size="sm"
                    variant="outline"
                  >
                    Reject
                  </Button>
                </Show>
                <DiscussButton />
              </div>
            </div>
          );
        }}
      </For>
    </div>
  );
}

// ---- Variant C: dense table / spreadsheet -------------------------

function VariantCQueue(props: {
  decisions: Partial<Record<string, Decision>>;
  decide: (id: string, decision: Decision) => void;
}) {
  const [expanded, setExpanded] = createSignal<string | null>(null);
  const [selected, setSelected] = createSignal<Set<string>>(new Set());

  const toggleSelected = (id: string) => {
    const next = new Set(selected());
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  };

  const bulkDecide = (decision: Decision) => {
    for (const id of selected()) {
      props.decide(id, decision);
    }
    setSelected(new Set<string>());
  };

  return (
    <div class="flex flex-col gap-2">
      <Show when={selected().size > 0}>
        <div class="flex items-center gap-2 rounded-lg border bg-accent px-3 py-2 text-sm">
          <span class="font-medium">{selected().size} selected</span>
          <Button onClick={() => bulkDecide("approved")} size="sm">
            Bulk approve
          </Button>
          <Button
            onClick={() => bulkDecide("rejected")}
            size="sm"
            variant="outline"
          >
            Bulk reject
          </Button>
        </div>
      </Show>

      <div class="overflow-x-auto rounded-xl border">
        <table class="w-full min-w-[720px] text-left text-sm">
          <thead class="bg-muted/50 text-xs uppercase text-muted-foreground">
            <tr>
              <th class="w-8 px-2 py-2"></th>
              <th class="px-2 py-2">Title</th>
              <th class="px-2 py-2">Category</th>
              <th class="px-2 py-2">Actions</th>
              <th class="px-2 py-2">Confidence</th>
              <th class="px-2 py-2">Status</th>
              <th class="px-2 py-2"></th>
            </tr>
          </thead>
          <tbody>
            <For each={mockProposals}>
              {(proposal) => {
                const decision = () => props.decisions[proposal.id];
                const isExpanded = () => expanded() === proposal.id;
                return (
                  <>
                    <tr class={cx("border-t", decision() && "opacity-50")}>
                      <td class="px-2 py-1.5">
                        <input
                          checked={selected().has(proposal.id)}
                          disabled={!!decision()}
                          onChange={() => toggleSelected(proposal.id)}
                          type="checkbox"
                        />
                      </td>
                      <td class="px-2 py-1.5">
                        <button
                          class="font-medium hover:underline"
                          onClick={() =>
                            setExpanded(isExpanded() ? null : proposal.id)
                          }
                          type="button"
                        >
                          {isExpanded() ? "▾" : "▸"} {proposal.title}
                        </button>
                      </td>
                      <td class="px-2 py-1.5 text-muted-foreground">
                        {proposal.category}
                      </td>
                      <td class="px-2 py-1.5">{proposal.actions.length}</td>
                      <td
                        class={cx(
                          "px-2 py-1.5 font-semibold",
                          confidenceTone(proposal.confidence),
                        )}
                      >
                        {Math.round(proposal.confidence * 100)}%
                      </td>
                      <td class="px-2 py-1.5">
                        <Show
                          fallback={
                            <div class="flex gap-1">
                              <Button
                                onClick={() =>
                                  props.decide(proposal.id, "approved")
                                }
                                size="sm"
                                variant="outline"
                              >
                                ✓
                              </Button>
                              <Button
                                onClick={() =>
                                  props.decide(proposal.id, "rejected")
                                }
                                size="sm"
                                variant="outline"
                              >
                                ✗
                              </Button>
                            </div>
                          }
                          when={decision()}
                        >
                          <span class="capitalize text-muted-foreground">
                            {decision()}
                          </span>
                        </Show>
                      </td>
                      <td class="px-2 py-1.5">
                        <DiscussButton />
                      </td>
                    </tr>
                    <Show when={isExpanded()}>
                      <tr class="border-t bg-muted/30">
                        <td class="px-2 py-2" colspan={7}>
                          <p class="mb-2 text-xs text-muted-foreground">
                            {proposal.summary}
                          </p>
                          <div class="flex flex-col gap-1">
                            <For each={proposal.actions}>
                              {(action) => (
                                <div class="flex items-center justify-between gap-2 rounded border bg-background px-2 py-1 text-xs">
                                  <span>
                                    <span class="font-medium">
                                      {action.display}
                                    </span>{" "}
                                    — {action.sender} · "{action.subject}" ·{" "}
                                    {action.ageDays}d old
                                  </span>
                                  <Badge variant="outline">{action.kind}</Badge>
                                </div>
                              )}
                            </For>
                          </div>
                        </td>
                      </tr>
                    </Show>
                  </>
                );
              }}
            </For>
          </tbody>
        </table>
      </div>
    </div>
  );
}

function QueueView() {
  const [decisions, setDecisions] = createSignal<
    Partial<Record<string, Decision>>
  >({});
  const decide = (id: string, decision: Decision) =>
    setDecisions({ ...decisions(), [id]: decision });

  return (
    <Switch>
      <Match when={variant() === "A"}>
        <VariantAQueue decide={decide} decisions={decisions()} />
      </Match>
      <Match when={variant() === "B"}>
        <VariantBQueue decide={decide} decisions={decisions()} />
      </Match>
      <Match when={variant() === "C"}>
        <VariantCQueue decide={decide} decisions={decisions()} />
      </Match>
    </Switch>
  );
}

function HistoryView() {
  return (
    <div class="flex flex-col gap-2">
      <For each={mockProposalHistory}>
        {(entry) => (
          <div class={cx(panelClass, "flex items-center justify-between")}>
            <div>
              <p class="font-medium">{entry.title}</p>
              <p class="text-xs text-muted-foreground">
                {entry.actionCount} action
                {entry.actionCount === 1 ? "" : "s"} ·{" "}
                {new Date(entry.decidedAt).toLocaleString()}
              </p>
            </div>
            <span
              class={cx(
                "text-sm font-semibold capitalize",
                historyTone(entry.state),
              )}
            >
              {entry.state}
            </span>
          </div>
        )}
      </For>
    </div>
  );
}

function GrantsView() {
  return (
    <div class="flex flex-col gap-6">
      <div>
        <h3 class="mb-2 text-sm font-semibold">Standing grants</h3>
        <div class="flex flex-col gap-2">
          <For each={mockGrants}>
            {(grant) => (
              <div class={cx(panelClass, "flex items-center justify-between")}>
                <div>
                  <p class="font-medium">
                    {grant.kind}
                    {grant.scope ? ` · ${grant.scope}` : ""}
                  </p>
                  <p class="text-xs text-muted-foreground">
                    auto-approve under {grant.autoApproveUnder} items · granted{" "}
                    {new Date(grant.grantedAt).toLocaleDateString()}
                  </p>
                </div>
                <Button size="sm" variant="outline">
                  Revoke
                </Button>
              </div>
            )}
          </For>
        </div>
      </div>

      <div>
        <h3 class="mb-2 text-sm font-semibold">Suggested grants</h3>
        <div class="flex flex-col gap-2">
          <For each={mockGrantSuggestions}>
            {(suggestion) => (
              <div class={cx(panelClass, "flex items-center justify-between")}>
                <div>
                  <p class="font-medium">
                    {suggestion.kind}
                    {suggestion.scope ? ` · ${suggestion.scope}` : ""}
                  </p>
                  <p class="text-xs text-muted-foreground">
                    {suggestion.reason} (
                    {Math.round(suggestion.approvalRate * 100)}% of{" "}
                    {suggestion.sampleSize})
                  </p>
                </div>
                <Button size="sm">Grant</Button>
              </div>
            )}
          </For>
        </div>
      </div>
    </div>
  );
}

export function ProposalsPage() {
  const [subView, setSubView] = createSignal<SubView>("queue");

  return (
    <div class="flex flex-1 flex-col gap-4 p-4">
      <div class="flex items-center gap-2 border-b pb-2">
        <For each={["queue", "history", "grants"] as SubView[]}>
          {(view) => (
            <button
              class={cx(
                "rounded-md px-3 py-1.5 text-sm font-medium capitalize",
                subView() === view
                  ? "bg-accent text-accent-foreground"
                  : "text-muted-foreground hover:bg-accent/50",
              )}
              onClick={() => setSubView(view)}
              type="button"
            >
              {view}
              {view === "queue" ? ` (${String(mockProposals.length)})` : ""}
            </button>
          )}
        </For>
      </div>

      <Switch>
        <Match when={subView() === "queue"}>
          <QueueView />
        </Match>
        <Match when={subView() === "history"}>
          <HistoryView />
        </Match>
        <Match when={subView() === "grants"}>
          <GrantsView />
        </Match>
      </Switch>
    </div>
  );
}
