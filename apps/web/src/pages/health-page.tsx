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

function formatInstant(value: string | null, timeZone?: string): string {
  if (value === null) return "No observations yet";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone,
  }).format(new Date(value));
}

const weekdayNames = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function formatExerciseType(value: string): string {
  return value.replaceAll("_", " ");
}

function formatMomentKind(
  kind: "exercise" | "missed_exercise" | "primary_sleep",
): string {
  if (kind === "primary_sleep") return "Primary sleep";
  if (kind === "missed_exercise") return "Planned exercise check-in";
  return "Exercise";
}

function formatWindow(window: {
  end_local_time: string;
  start_local_time: string;
  weekday: number;
}): string {
  const weekday = weekdayNames[window.weekday] ?? "Unknown day";
  return `${weekday} ${window.start_local_time.slice(0, 5)} to ${window.end_local_time.slice(0, 5)}`;
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
  const hasExercise = createMemo(() => (exercise()?.record_count ?? 0) > 0);
  const heartRate = createMemo(() => overview()?.summary.heart_rate);
  const hasHeartRate = createMemo(
    () =>
      (heartRate()?.sample_count ?? 0) > 0 && heartRate()?.average_bpm !== null,
  );

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
                detail={
                  hasExercise()
                    ? `${formatMinutes(exercise()?.total_duration_minutes ?? 0)} total`
                    : "No exercise observations in this period"
                }
                label="Exercise"
                value={
                  hasExercise()
                    ? `${(exercise()?.record_count ?? 0).toLocaleString()} ${(exercise()?.record_count ?? 0) === 1 ? "workout" : "workouts"}`
                    : "No data"
                }
              />
              <MetricCard
                detail={
                  latestSteps() === undefined
                    ? "No step observations in this period"
                    : `${current().summary.steps.total_count.toLocaleString()} across ${days().toString()} days`
                }
                label="Steps"
                value={
                  latestSteps() === undefined
                    ? "No data"
                    : `${latestSteps()?.total_count.toLocaleString() ?? ""} latest day`
                }
              />
              <MetricCard
                detail={
                  hasHeartRate()
                    ? `${(heartRate()?.sample_count ?? 0).toLocaleString()} samples`
                    : "No heart-rate observations in this period"
                }
                label="Heart rate"
                value={
                  hasHeartRate()
                    ? `${heartRate()?.average_bpm?.toFixed(0) ?? ""} bpm avg`
                    : "No data"
                }
              />
            </div>

            <section aria-labelledby="health-plans" class="mt-8">
              <h2 id="health-plans" class="text-lg font-semibold">
                Exercise plans
              </h2>
              <p class="text-muted-foreground mt-1 text-sm">
                Explicit intentions from chat. These do not change the measured
                cards above.
              </p>
              <Show
                fallback={
                  <p class="text-muted-foreground mt-3 text-sm">
                    No exercise plans yet. Tell Tether the days and local times
                    you intend to exercise.
                  </p>
                }
                when={current().plans.length > 0}
              >
                <ul class="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
                  <For each={current().plans}>
                    {(plan) => (
                      <li class="border-border bg-card rounded-xl border p-4">
                        <div class="flex flex-wrap items-start gap-2">
                          <div class="min-w-0 flex-1">
                            <h3 class="font-semibold">{plan.title}</h3>
                            <p class="text-muted-foreground mt-1 text-sm capitalize">
                              {plan.exercise_types
                                .map(formatExerciseType)
                                .join(", ")}
                            </p>
                          </div>
                          <span class="bg-muted rounded-full px-2 py-1 text-xs capitalize">
                            {plan.status}
                          </span>
                        </div>
                        <ul class="mt-3 flex flex-wrap gap-2">
                          <For each={plan.windows}>
                            {(window) => (
                              <li class="bg-muted rounded-md px-2 py-1 text-sm">
                                {formatWindow(window)}
                              </li>
                            )}
                          </For>
                        </ul>
                        <div class="text-muted-foreground mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 text-xs">
                          <span>{plan.timezone}</span>
                          <span>
                            {plan.grace_minutes.toString()} min sync grace
                          </span>
                          <button
                            class="hover:text-foreground underline underline-offset-4"
                            onClick={() =>
                              app.openEvidence(plan.source_evidence_uri)
                            }
                            type="button"
                          >
                            Inspect plan evidence
                          </button>
                        </div>
                      </li>
                    )}
                  </For>
                </ul>
              </Show>
            </section>

            <section aria-labelledby="health-adherence" class="mt-8">
              <h2 id="health-adherence" class="text-lg font-semibold">
                Plan adherence
              </h2>
              <p class="text-muted-foreground mt-1 text-sm">
                Settled window matching. A miss can mean rest, changed plans, or
                delayed source data.
              </p>
              <Show
                fallback={
                  <p class="text-muted-foreground mt-3 text-sm">
                    No settled exercise windows in this period.
                  </p>
                }
                when={current().planned_exercise.length > 0}
              >
                <ul class="mt-3 space-y-2">
                  <For each={current().planned_exercise}>
                    {(occurrence) => (
                      <li class="border-border bg-card flex flex-wrap items-center gap-3 rounded-lg border p-3">
                        <div class="min-w-0 flex-1">
                          <p class="font-medium">{occurrence.title}</p>
                          <p class="text-muted-foreground text-sm">
                            {formatInstant(
                              occurrence.window_started_at,
                              occurrence.timezone,
                            )}{" "}
                            to{" "}
                            {formatInstant(
                              occurrence.window_ended_at,
                              occurrence.timezone,
                            )}{" "}
                            · {occurrence.timezone}
                          </p>
                        </div>
                        <span
                          class={
                            occurrence.status === "matched"
                              ? "bg-muted rounded-full px-2.5 py-1 text-sm"
                              : "border-border rounded-full border px-2.5 py-1 text-sm"
                          }
                        >
                          {occurrence.status === "matched"
                            ? "Workout matched"
                            : "No matching workout"}
                        </span>
                        <Show when={occurrence.matched_evidence_uri}>
                          {(evidenceUri) => (
                            <button
                              class="hover:bg-muted rounded-md px-2 py-1.5 text-sm underline underline-offset-4"
                              onClick={() => app.openEvidence(evidenceUri())}
                              type="button"
                            >
                              Inspect workout
                            </button>
                          )}
                        </Show>
                      </li>
                    )}
                  </For>
                </ul>
              </Show>
            </section>

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
                            {formatMomentKind(moment.kind)}
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
