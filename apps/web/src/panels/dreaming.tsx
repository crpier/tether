import {
  createMutation,
  createQuery,
  useQueryClient,
} from "@tanstack/solid-query";
import { For, Show, createMemo, createSignal } from "solid-js";

import { Button } from "@/components/ui/button";
import { EvidenceLink } from "../components/evidence-link";
import {
  SegmentedControl,
  segmentedPanelId,
  segmentedTabId,
} from "../components/segmented-control";
import { Badge } from "../components/ui/badge";
import type {
  DreamingHost,
  DreamingMutation,
  DreamRun,
} from "../host/dreaming";
import { formatDateTime } from "../lib/format";
import { panelClass } from "../lib/panel";
import { queryKeys } from "../lib/query-keys";

function statusLabel(status: string): string {
  switch (status) {
    case "success":
      return "Changed";
    case "no_op":
      return "No changes";
    case "failed":
      return "Failed";
    case "running":
      return "Running";
    case "queued":
      return "Queued";
    default:
      return status;
  }
}

function statusVariant(
  status: string,
): "default" | "destructive" | "outline" | "secondary" {
  switch (status) {
    case "success":
    case "running":
      return "default";
    case "failed":
      return "destructive";
    case "no_op":
      return "outline";
    default:
      return "secondary";
  }
}

function kindLabel(kind: string): string {
  switch (kind) {
    case "assimilation":
      return "Automatic";
    case "maintenance":
      return "Maintenance";
    case "manual":
      return "Manual";
    default:
      return kind;
  }
}

function conversationLabel(run: DreamRun): string {
  return (
    run.conversation_title ?? `Conversation ${run.conversation_id.slice(0, 8)}`
  );
}

function mutationLabel(operation: string): string {
  switch (operation) {
    case "delete":
      return "Deleted";
    case "move":
      return "Moved";
    case "restore":
      return "Restored";
    case "write":
      return "Wrote";
    default:
      return operation;
  }
}

function mutationStatusLabel(status: string): string {
  return status.charAt(0).toUpperCase() + status.slice(1);
}

function durationLabel(run: DreamRun): string | undefined {
  if (run.completed_at === null) {
    return undefined;
  }
  const started = new Date(run.created_at).getTime();
  const completed = new Date(run.completed_at).getTime();
  if (Number.isNaN(started) || Number.isNaN(completed) || completed < started) {
    return undefined;
  }
  const seconds = Math.round((completed - started) / 1000);
  if (seconds < 60) {
    return `${String(seconds)}s total`;
  }
  return `${String(Math.round(seconds / 60))}m total`;
}

type DreamRunFilter = "all" | "changed" | "no_changes" | "failed";

function matchesFilter(run: DreamRun, filter: DreamRunFilter): boolean {
  switch (filter) {
    case "changed":
      return run.status === "success";
    case "no_changes":
      return run.status === "no_op";
    case "failed":
      return run.status === "failed";
    case "all":
      return true;
  }
}

function mutationRow(
  mutation: DreamingMutation,
  onOpenEvidence: (uri: string) => void,
) {
  return (
    <li class="bg-muted rounded-md border px-3 py-2 text-sm">
      <div class="flex flex-wrap items-center gap-2">
        <span class="font-medium">{mutationLabel(mutation.operation)}</span>
        <Badge class="ml-auto" variant="outline">
          {mutationStatusLabel(mutation.status)}
        </Badge>
      </div>
      <Show
        fallback={
          <p class="text-muted-foreground mt-2 text-xs">
            No fact-level diff is available for this change.
          </p>
        }
        when={mutation.fact_changes.length > 0}
      >
        <ul aria-label="Fact changes" class="mt-2 space-y-1">
          <For each={mutation.fact_changes}>
            {(change) => {
              const added = change.kind === "added";
              return (
                <li
                  class={`flex gap-2 rounded px-2 py-1.5 font-mono text-xs ${
                    added
                      ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                      : "bg-red-500/10 text-red-700 dark:text-red-300"
                  }`}
                >
                  <span class="w-3 shrink-0 font-bold">
                    {added ? "+" : "−"}
                  </span>
                  <span class="min-w-0 break-words">
                    <Show when={change.topic}>
                      {(topic) => (
                        <span class="mr-2 opacity-70">{topic()}</span>
                      )}
                    </Show>
                    {change.text}
                    <Show when={change.evidence.length > 0}>
                      <span class="mt-1 flex flex-wrap gap-2 font-sans">
                        <For each={change.evidence}>
                          {(evidence) => (
                            <EvidenceLink
                              onOpen={onOpenEvidence}
                              uri={evidence}
                            >
                              source
                            </EvidenceLink>
                          )}
                        </For>
                      </span>
                    </Show>
                  </span>
                </li>
              );
            }}
          </For>
        </ul>
      </Show>
      <p class="text-muted-foreground mt-2 text-[11px]">
        File <code class="break-all">{mutation.workspace_path}</code>
      </p>
      <Show when={mutation.attempts > 1}>
        <p class="text-muted-foreground mt-1 text-xs">
          {mutation.attempts} attempts
        </p>
      </Show>
      <Show when={mutation.error}>
        {(error) => (
          <p class="text-destructive mt-1 text-xs" role="alert">
            {error()}
          </p>
        )}
      </Show>
    </li>
  );
}

export function DreamingPanel(props: {
  api: DreamingHost;
  onOpenEvidence: (uri: string) => void;
}) {
  const [filter, setFilter] = createSignal<DreamRunFilter>("all");
  const [selectedRunId, setSelectedRunId] = createSignal<string>();
  const queryClient = useQueryClient();
  const dreamNowMutation = createMutation(() => ({
    mutationFn: () => props.api.dreamNow(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dreamRuns });
    },
  }));
  const runsQuery = createQuery(() => ({
    queryFn: () => props.api.listDreamRuns(),
    queryKey: queryKeys.dreamRuns,
    refetchInterval: (query) =>
      query.state.data?.some((run) =>
        ["queued", "running"].includes(run.status),
      ) === true
        ? 1_000
        : false,
  }));
  const runs = createMemo(() => runsQuery.data ?? []);
  const filteredRuns = createMemo(() =>
    runs().filter((run) => matchesFilter(run, filter())),
  );
  const activeRunCount = createMemo(
    () =>
      runs().filter((run) => ["queued", "running"].includes(run.status)).length,
  );
  const failedRunCount = createMemo(
    () => runs().filter((run) => run.status === "failed").length,
  );
  const lastChangedRun = createMemo(() =>
    runs().find((run) => run.status === "success"),
  );
  const selectedRun = createMemo(() =>
    runs().find((run) => run.id === selectedRunId()),
  );
  const detailQuery = createQuery(() => {
    const runId = selectedRunId();
    return {
      enabled: runId !== undefined,
      queryFn: () =>
        runId === undefined
          ? Promise.reject(new Error("No Dream run selected"))
          : props.api.getDreamRun(runId),
      queryKey: queryKeys.dreamRun(runId ?? "none"),
      refetchInterval:
        selectedRun()?.status === "queued" ||
        selectedRun()?.status === "running"
          ? 1_000
          : false,
    };
  });

  return (
    <section aria-label="Dreaming" class={panelClass}>
      <div class="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 class="text-sm font-semibold">Dreaming</h2>
          <p class="text-muted-foreground mt-1 text-xs">
            Background runs that assimilate settled Evidence into current
            Memory.
          </p>
        </div>
        <div class="flex items-center gap-2">
          <Show
            when={runs().some((run) =>
              ["queued", "running"].includes(run.status),
            )}
          >
            <Badge variant="secondary">Dreaming active</Badge>
          </Show>
          <Button
            disabled={dreamNowMutation.isPending}
            onClick={() => {
              dreamNowMutation.mutate();
            }}
            size="sm"
            title="Assimilate settled Evidence into Memory right now"
            type="button"
          >
            Dream now
          </Button>
        </div>
      </div>
      <Show when={dreamNowMutation.isError}>
        <p class="text-destructive mb-3 text-sm" role="alert">
          Could not start a Dream run. Try again shortly.
        </p>
      </Show>
      <Show when={runsQuery.isError}>
        <p class="text-destructive text-sm" role="alert">
          Could not load Dream history. Try again shortly.
        </p>
      </Show>
      <Show
        fallback={
          <Show when={!runsQuery.isError}>
            <p class="text-muted-foreground text-sm">No Dream runs yet</p>
          </Show>
        }
        when={!runsQuery.isError && runs().length > 0}
      >
        <div class="space-y-4">
          <section
            aria-label="Dreaming status"
            class="grid gap-2 sm:grid-cols-3"
          >
            <div class="bg-muted rounded-md border px-3 py-2">
              <p class="text-muted-foreground text-xs">Activity</p>
              <p class="text-sm font-medium">
                {activeRunCount() === 0
                  ? "Idle"
                  : `${String(activeRunCount())} active ${activeRunCount() === 1 ? "run" : "runs"}`}
              </p>
            </div>
            <div class="bg-muted rounded-md border px-3 py-2">
              <p class="text-muted-foreground text-xs">Last change</p>
              <p class="text-sm font-medium">
                {lastChangedRun() === undefined
                  ? "None yet"
                  : formatDateTime(
                      new Date(
                        lastChangedRun()!.completed_at ??
                          lastChangedRun()!.created_at,
                      ),
                    )}
              </p>
            </div>
            <div class="bg-muted rounded-md border px-3 py-2">
              <p class="text-muted-foreground text-xs">Failures</p>
              <p class="text-sm font-medium">
                {failedRunCount() === 0
                  ? "No failed runs"
                  : `${String(failedRunCount())} failed ${failedRunCount() === 1 ? "run" : "runs"}`}
              </p>
            </div>
          </section>
          <div class="grid gap-4 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
            <div>
              <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
                <h3 class="text-muted-foreground text-xs font-semibold uppercase tracking-wider">
                  Run history
                </h3>
                <SegmentedControl
                  aria-label="Dream run filter"
                  id="dream-run-filter"
                  onChange={(nextFilter) => {
                    setFilter(nextFilter);
                    setSelectedRunId(undefined);
                  }}
                  options={[
                    { label: "All", value: "all" },
                    { label: "Changed", value: "changed" },
                    { label: "No changes", value: "no_changes" },
                    { label: "Failed", value: "failed" },
                  ]}
                  value={filter()}
                />
              </div>
              <div
                aria-labelledby={segmentedTabId("dream-run-filter", filter())}
                id={segmentedPanelId("dream-run-filter", filter())}
                role="tabpanel"
              >
                <Show
                  fallback={
                    <p class="text-muted-foreground text-sm">
                      No Dream runs match this filter.
                    </p>
                  }
                  when={filteredRuns().length > 0}
                >
                  <ul class="space-y-2">
                    <For each={filteredRuns()}>
                      {(run) => {
                        const label = () => statusLabel(run.status);
                        return (
                          <li>
                            <button
                              aria-label={`${label()} — ${conversationLabel(run)}`}
                              aria-pressed={selectedRunId() === run.id}
                              class="bg-muted hover:bg-accent aria-pressed:border-primary w-full rounded-md border px-3 py-2 text-left text-sm transition-colors"
                              onClick={() => {
                                setSelectedRunId(run.id);
                              }}
                              type="button"
                            >
                              <div class="flex flex-wrap items-center gap-2">
                                <Badge variant={statusVariant(run.status)}>
                                  {label()}
                                </Badge>
                                <span class="min-w-0 truncate font-medium">
                                  {conversationLabel(run)}
                                </span>
                                <span class="text-muted-foreground ml-auto text-xs">
                                  {formatDateTime(new Date(run.created_at))}
                                </span>
                              </div>
                              <div class="text-muted-foreground mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs">
                                <span>{kindLabel(run.kind)}</span>
                                <span>
                                  Messages {run.evidence_start_seq}–
                                  {run.evidence_end_seq}
                                </span>
                                <span>
                                  {run.mutation_count} Memory{" "}
                                  {run.mutation_count === 1
                                    ? "change"
                                    : "changes"}
                                </span>
                              </div>
                            </button>
                          </li>
                        );
                      }}
                    </For>
                  </ul>
                </Show>
              </div>
            </div>
            <Show
              fallback={
                <div class="text-muted-foreground rounded-md border border-dashed p-4 text-sm">
                  Select a run to inspect its Evidence bounds and Memory
                  changes.
                </div>
              }
              when={selectedRun()}
            >
              {(run) => (
                <section
                  aria-label="Dream run details"
                  class="rounded-md border p-4"
                >
                  <div class="flex flex-wrap items-center gap-2">
                    <h3 class="font-semibold">{conversationLabel(run())}</h3>
                    <Badge variant={statusVariant(run().status)}>
                      {statusLabel(run().status)}
                    </Badge>
                  </div>
                  <dl class="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                    <div>
                      <dt class="text-muted-foreground text-xs">Trigger</dt>
                      <dd>{kindLabel(run().kind)}</dd>
                    </div>
                    <div>
                      <dt class="text-muted-foreground text-xs">Evidence</dt>
                      <dd>
                        Messages {run().evidence_start_seq}–
                        {run().evidence_end_seq}
                      </dd>
                    </div>
                    <div>
                      <dt class="text-muted-foreground text-xs">Attempts</dt>
                      <dd>{run().attempts}</dd>
                    </div>
                    <div>
                      <dt class="text-muted-foreground text-xs">Timing</dt>
                      <dd>{durationLabel(run()) ?? "In progress"}</dd>
                    </div>
                  </dl>
                  <Show when={run().error}>
                    {(error) => (
                      <div
                        class="border-destructive/40 bg-destructive/10 text-destructive mt-3 rounded-md border px-3 py-2 text-sm"
                        role="alert"
                      >
                        {error()}
                      </div>
                    )}
                  </Show>
                  <Show when={detailQuery.isError}>
                    <p class="text-destructive mt-3 text-sm" role="alert">
                      Could not load this Dream run's details. Try again
                      shortly.
                    </p>
                  </Show>
                  <Show when={!detailQuery.isError}>
                    <div>
                      <h4 class="text-muted-foreground mt-4 text-xs font-semibold uppercase tracking-wider">
                        What changed
                      </h4>
                      <Show
                        fallback={
                          <p class="text-muted-foreground mt-2 text-sm">
                            {run().status === "no_op"
                              ? "No Memory changes were needed."
                              : "No Memory mutations were recorded."}
                          </p>
                        }
                        when={(detailQuery.data?.mutations.length ?? 0) > 0}
                      >
                        <ul class="mt-2 space-y-2">
                          <For each={detailQuery.data?.mutations ?? []}>
                            {(mutation) =>
                              mutationRow(mutation, props.onOpenEvidence)
                            }
                          </For>
                        </ul>
                      </Show>
                    </div>
                  </Show>
                </section>
              )}
            </Show>
          </div>
        </div>
      </Show>
    </section>
  );
}
