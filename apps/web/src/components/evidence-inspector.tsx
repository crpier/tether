import { Match, Show, Switch, createEffect, createSignal } from "solid-js";

import type { Evidence, EvidenceHost } from "../host/evidence";
import { formatDateTime } from "../lib/format";

type InspectorState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; evidence: Evidence };

const loadingState: InspectorState = { status: "loading" };

function minutes(value: number): string {
  const hours = Math.floor(value / 60);
  const remainder = Math.round(value % 60);
  if (hours === 0) {
    return `${String(remainder)} min`;
  }
  return remainder === 0
    ? `${String(hours)} hr`
    : `${String(hours)} hr ${String(remainder)} min`;
}

function label(value: string): string {
  const spaced = value.replaceAll("_", " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

function EvidenceDetails(props: { evidence: Evidence }) {
  return (
    <Switch>
      <Match when={props.evidence.kind === "message" && props.evidence}>
        {(evidence) => (
          <div class="space-y-3">
            <p class="text-muted-foreground text-xs">
              {label(evidence().role)} · Message {evidence().seq} ·{" "}
              {formatDateTime(new Date(evidence().occurred_at))}
            </p>
            <p class="bg-muted whitespace-pre-wrap break-words rounded-md border p-3 text-sm">
              {evidence().content}
            </p>
          </div>
        )}
      </Match>
      <Match
        when={props.evidence.kind === "health_connect_sleep" && props.evidence}
      >
        {(evidence) => (
          <div class="space-y-3">
            <dl class="grid grid-cols-2 gap-3 text-sm">
              <div>
                <dt class="text-muted-foreground text-xs">Start</dt>
                <dd>{formatDateTime(new Date(evidence().start_time))}</dd>
              </div>
              <div>
                <dt class="text-muted-foreground text-xs">End</dt>
                <dd>{formatDateTime(new Date(evidence().end_time))}</dd>
              </div>
              <div>
                <dt class="text-muted-foreground text-xs">Duration</dt>
                <dd>{minutes(evidence().duration_minutes)}</dd>
              </div>
              <div>
                <dt class="text-muted-foreground text-xs">Source version</dt>
                <dd>{evidence().version_id}</dd>
              </div>
            </dl>
            <Show when={Object.keys(evidence().stage_minutes).length > 0}>
              <div>
                <h3 class="text-muted-foreground text-xs font-medium">
                  Recorded stages
                </h3>
                <dl class="mt-1 grid grid-cols-2 gap-x-3 gap-y-1 text-sm">
                  {Object.entries(evidence().stage_minutes).map(
                    ([stage, value]) => (
                      <div class="flex justify-between gap-2">
                        <dt>{label(stage)}</dt>
                        <dd>{minutes(value)}</dd>
                      </div>
                    ),
                  )}
                </dl>
              </div>
            </Show>
          </div>
        )}
      </Match>
      <Match
        when={
          props.evidence.kind === "health_connect_exercise" && props.evidence
        }
      >
        {(evidence) => (
          <dl class="grid grid-cols-2 gap-3 text-sm">
            <div>
              <dt class="text-muted-foreground text-xs">Start</dt>
              <dd>{formatDateTime(new Date(evidence().start_time))}</dd>
            </div>
            <div>
              <dt class="text-muted-foreground text-xs">End</dt>
              <dd>{formatDateTime(new Date(evidence().end_time))}</dd>
            </div>
            <div>
              <dt class="text-muted-foreground text-xs">Duration</dt>
              <dd>{minutes(evidence().duration_minutes)}</dd>
            </div>
            <div>
              <dt class="text-muted-foreground text-xs">Exercise</dt>
              <dd>{label(evidence().exercise_type ?? "unknown")}</dd>
            </div>
            <div>
              <dt class="text-muted-foreground text-xs">Segments</dt>
              <dd>{evidence().segment_count}</dd>
            </div>
            <div>
              <dt class="text-muted-foreground text-xs">Laps</dt>
              <dd>{evidence().lap_count}</dd>
            </div>
            <Show when={evidence().total_lap_meters !== null}>
              <div>
                <dt class="text-muted-foreground text-xs">Lap distance</dt>
                <dd>{Math.round(evidence().total_lap_meters ?? 0)} m</dd>
              </div>
            </Show>
            <div>
              <dt class="text-muted-foreground text-xs">Source version</dt>
              <dd>{evidence().version_id}</dd>
            </div>
          </dl>
        )}
      </Match>
    </Switch>
  );
}

function evidenceTitle(evidence: Evidence): string {
  switch (evidence.kind) {
    case "message":
      return "Conversation message";
    case "health_connect_sleep":
      return evidence.title ?? "Sleep episode";
    case "health_connect_exercise":
      return evidence.title ?? "Exercise episode";
  }
}

export function EvidenceInspector(props: {
  api: EvidenceHost;
  onClose: () => void;
  uri: string | null;
}) {
  const [state, setState] = createSignal<InspectorState>(loadingState);

  createEffect(() => {
    const uri = props.uri;
    if (uri === null) {
      setState(loadingState);
      return;
    }
    setState(loadingState);
    void props.api.resolveEvidence(uri).then(
      (evidence) => {
        if (props.uri === uri) {
          setState({ evidence, status: "ready" });
        }
      },
      (caught: unknown) => {
        if (props.uri === uri) {
          setState({
            message:
              caught instanceof Error
                ? caught.message
                : "Evidence is unavailable",
            status: "error",
          });
        }
      },
    );
  });

  return (
    <Show when={props.uri}>
      {(uri) => (
        <div
          aria-label="Evidence inspector"
          aria-modal="true"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
          role="dialog"
        >
          <div class="flex max-h-full w-full max-w-xl flex-col overflow-hidden rounded-lg border bg-card shadow-lg">
            <header class="flex items-start justify-between gap-3 border-b px-4 py-3">
              <div>
                <h2 class="text-sm font-semibold">
                  {state().status === "ready"
                    ? evidenceTitle(
                        (
                          state() as Extract<
                            InspectorState,
                            { status: "ready" }
                          >
                        ).evidence,
                      )
                    : "Evidence"}
                </h2>
                <code class="text-muted-foreground mt-1 block break-all text-[11px]">
                  {uri()}
                </code>
              </div>
              <button
                aria-label="Close Evidence"
                class="shrink-0 rounded-md border px-2 py-1 text-xs font-medium hover:bg-muted"
                onClick={props.onClose}
                type="button"
              >
                Close
              </button>
            </header>
            <div class="overflow-y-auto p-4">
              <Switch>
                <Match when={state().status === "loading"}>
                  <p class="text-muted-foreground text-sm">Loading Evidence…</p>
                </Match>
                <Match
                  when={
                    state().status === "error"
                      ? (state() as Extract<
                          InspectorState,
                          { status: "error" }
                        >)
                      : undefined
                  }
                >
                  {(error) => (
                    <p class="text-destructive text-sm" role="alert">
                      {error().message}
                    </p>
                  )}
                </Match>
                <Match
                  when={
                    state().status === "ready"
                      ? (state() as Extract<
                          InspectorState,
                          { status: "ready" }
                        >)
                      : undefined
                  }
                >
                  {(ready) => <EvidenceDetails evidence={ready().evidence} />}
                </Match>
              </Switch>
            </div>
          </div>
        </div>
      )}
    </Show>
  );
}
