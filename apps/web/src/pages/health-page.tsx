import { A } from "@solidjs/router";
import { createQuery } from "@tanstack/solid-query";
import { For, Show, createMemo, createSignal } from "solid-js";

import { useAppContext, useHost } from "../app-context";
import { queryKeys } from "../lib/query-keys";

function formatMinutes(minutes: number): string {
  const rounded = Math.round(minutes);
  const hours = Math.floor(rounded / 60);
  const remainder = rounded % 60;
  if (hours === 0) return `${remainder.toString()}m`;
  if (remainder === 0) return `${hours.toString()}h`;
  return `${hours.toString()}h ${remainder.toString()}m`;
}

function formatInstant(value: string | null): string {
  if (value === null) return "No observations yet";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function MetricCard(props: { detail: string; label: string; value: string }) {
  return (
    <section
      aria-label={props.label}
      class="border-border bg-card rounded-xl border p-4"
    >
      <h2 class="text-muted-foreground text-sm font-medium">{props.label}</h2>
      <p class="mt-2 text-2xl font-semibold tracking-tight">{props.value}</p>
      <p class="text-muted-foreground mt-1 text-sm">{props.detail}</p>
    </section>
  );
}

export function HealthPage() {
  const health = useHost("health");
  const app = useAppContext();
  const [days, setDays] = createSignal<7 | 28 | 90>(7);
  const overviewQuery = createQuery(() => ({
    queryFn: () => health.getOverview(days()),
    queryKey: queryKeys.healthOverview(days()),
  }));
  const overview = createMemo(() => overviewQuery.data);
  const sleep = createMemo(
    () => overview()?.primary_sleep.selected_episode ?? null,
  );
  const latestSteps = createMemo(() => overview()?.summary.steps.daily.at(-1));
  const exercise = createMemo(() => overview()?.summary.exercise);
  const heartRate = createMemo(() => overview()?.summary.heart_rate);

  return (
    <div class="mx-auto w-full max-w-6xl p-4 sm:p-6 lg:p-8">
      <header class="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 class="text-2xl font-semibold tracking-tight">Health</h1>
          <p class="text-muted-foreground mt-1 text-sm">
            Measured Health Connect observations. Agent briefings stay linked,
            but never define these values.
          </p>
        </div>
        <div aria-label="Health period" class="bg-muted flex rounded-lg p-1">
          <For each={[7, 28, 90] as const}>
            {(period) => (
              <button
                aria-pressed={days() === period}
                class="aria-pressed:bg-background rounded-md px-3 py-1.5 text-sm aria-pressed:shadow-sm"
                onClick={() => setDays(period)}
                type="button"
              >
                {period}d
              </button>
            )}
          </For>
        </div>
      </header>

      <Show when={overviewQuery.isLoading}>
        <p class="text-muted-foreground mt-8">Loading Health observations…</p>
      </Show>
      <Show when={overviewQuery.isError}>
        <p class="text-destructive mt-8" role="alert">
          Health observations could not be loaded.
        </p>
      </Show>

      <Show when={overview()}>
        {(current) => (
          <>
            <p class="text-muted-foreground mt-5 text-xs">
              Latest observation:{" "}
              {formatInstant(current().latest_observation_at)}
            </p>
            <div class="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <MetricCard
                detail={
                  sleep() === null
                    ? "No primary sleep in this period"
                    : `${sleep()?.sleep_efficiency_percent?.toFixed(1) ?? "Unknown"}% efficiency`
                }
                label="Last sleep"
                value={
                  sleep() === null
                    ? "No data"
                    : formatMinutes(sleep()?.time_asleep_minutes ?? 0)
                }
              />
              <MetricCard
                detail={`${formatMinutes(exercise()?.total_duration_minutes ?? 0)} total`}
                label="Exercise"
                value={`${(exercise()?.record_count ?? 0).toLocaleString()} ${(exercise()?.record_count ?? 0) === 1 ? "workout" : "workouts"}`}
              />
              <MetricCard
                detail={`${current().summary.steps.total_count.toLocaleString()} across ${days().toString()} days`}
                label="Steps"
                value={`${(latestSteps()?.total_count ?? 0).toLocaleString()} latest day`}
              />
              <MetricCard
                detail={`${(heartRate()?.sample_count ?? 0).toLocaleString()} samples`}
                label="Heart rate"
                value={
                  heartRate()?.average_bpm === null
                    ? "No data"
                    : `${heartRate()?.average_bpm?.toFixed(0) ?? "0"} bpm avg`
                }
              />
            </div>

            <section aria-labelledby="health-briefings" class="mt-8">
              <h2 id="health-briefings" class="text-lg font-semibold">
                Proactive briefings
              </h2>
              <Show
                fallback={
                  <p class="text-muted-foreground mt-3 text-sm">
                    No Health moments in this period.
                  </p>
                }
                when={current().moments.length > 0}
              >
                <ul class="mt-3 space-y-2">
                  <For each={current().moments}>
                    {(moment) => (
                      <li class="border-border bg-card flex flex-wrap items-center gap-3 rounded-lg border p-3">
                        <div class="min-w-0 flex-1">
                          <p class="font-medium">
                            {moment.kind === "primary_sleep"
                              ? "Primary sleep"
                              : "Exercise"}
                          </p>
                          <p class="text-muted-foreground text-sm">
                            {formatInstant(moment.observed_at)} ·{" "}
                            {moment.status}
                          </p>
                        </div>
                        <button
                          class="hover:bg-muted rounded-md px-2 py-1.5 text-sm underline underline-offset-4"
                          onClick={() => app.openEvidence(moment.evidence_uri)}
                          type="button"
                        >
                          Inspect evidence
                        </button>
                        <Show when={moment.turn_id}>
                          {(turnId) => (
                            <A
                              class="bg-primary text-primary-foreground rounded-md px-3 py-1.5 text-sm font-medium"
                              href={`/chat?turn=${turnId()}`}
                            >
                              Open briefing
                            </A>
                          )}
                        </Show>
                      </li>
                    )}
                  </For>
                </ul>
              </Show>
            </section>
          </>
        )}
      </Show>
    </div>
  );
}
