import { cleanup, screen, within } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import type { HealthOverview } from "../host";
import { FakeHost, navigateTo, renderApp } from "../testing/harness";

afterEach(cleanup);

const overview: HealthOverview = {
  after: "2026-08-20T08:00:00Z",
  before: "2026-08-27T08:00:00Z",
  days: 7,
  latest_observation_at: "2026-08-27T07:30:00Z",
  moments: [
    {
      evidence_uri: "tether://health-connect/sleep/sleep-1@v4",
      id: "019f0000-0000-7000-8000-000000000001",
      kind: "primary_sleep",
      observed_at: "2026-08-27T07:30:00Z",
      status: "succeeded",
      turn_id: "019f0000-0000-7000-8000-000000000002",
    },
  ],
  planned_exercise: [
    {
      grace_ended_at: "2026-08-24T18:00:00Z",
      local_date: "2026-08-24",
      matched_evidence_uri: null,
      plan_id: "019f0000-0000-7000-8000-000000000003",
      plan_version: 1,
      source_record_uid:
        "019f0000-0000-7000-8000-000000000003:2026-08-24:0:18:00:20:00",
      status: "missed",
      timezone: "Europe/Athens",
      title: "Home strength",
      window_ended_at: "2026-08-24T17:00:00Z",
      window_started_at: "2026-08-24T15:00:00Z",
    },
  ],
  plans: [
    {
      created_at: "2026-08-20T08:00:00Z",
      effective_at: "2026-08-20T08:00:00Z",
      exercise_types: ["strength_training", "weightlifting"],
      grace_minutes: 60,
      id: "019f0000-0000-7000-8000-000000000003",
      source_evidence_uri:
        "tether://message/019f0000-0000-7000-8000-000000000004",
      status: "active",
      timezone: "Europe/Athens",
      title: "Home strength",
      updated_at: "2026-08-20T08:00:00Z",
      version: 1,
      windows: [
        {
          end_local_time: "20:00:00",
          start_local_time: "18:00:00",
          weekday: 0,
        },
        {
          end_local_time: "20:00:00",
          start_local_time: "18:00:00",
          weekday: 2,
        },
        {
          end_local_time: "20:00:00",
          start_local_time: "18:00:00",
          weekday: 4,
        },
      ],
    },
  ],
  primary_sleep: {
    baseline: null,
    focus: "sleep_episode",
    interpretation_limits:
      "Deterministic personal observations only; not a medical diagnosis.",
    requested_days: 7,
    selected_episode: {
      classification: "primary_sleep",
      evidence_uri: "tether://health-connect/sleep/sleep-1@v4",
      local_end: "2026-08-27T07:30:00+03:00",
      local_start: "2026-08-26T23:30:00+03:00",
      record_id: "sleep-1",
      sleep_efficiency_percent: 93.75,
      sleeping_heart_rate: {
        average_bpm: 58,
        by_stage: {},
        maximum_bpm: 64,
        minimum_bpm: 51,
        sample_count: 120,
      },
      source_version: 4,
      stage_coverage_percent: 100,
      stage_interval_count: 8,
      stage_minutes: { deep: 90, light: 240, rem: 120 },
      stage_percent_of_time_asleep: { deep: 20, light: 53.33, rem: 26.67 },
      stages_complete: true,
      time_asleep_minutes: 450,
      time_in_bed_minutes: 480,
    },
    sleep_day: null,
    status: "available",
  },
  summary: {
    after: "2026-08-20T08:00:00Z",
    before: "2026-08-27T08:00:00Z",
    exercise: {
      exercise_type_code_counts: { "70": 2 },
      exercise_type_counts: { strength_training: 2 },
      record_count: 2,
      total_duration_minutes: 85,
    },
    heart_rate: {
      average_bpm: 67,
      maximum_bpm: 142,
      minimum_bpm: 48,
      record_count: 12,
      sample_count: 420,
    },
    other_record_types: [],
    sleep: {
      average_duration_minutes: 470,
      record_count: 7,
      stage_code_duration_minutes: {},
      stage_duration_minutes: {},
      total_duration_minutes: 3290,
    },
    steps: {
      daily: [
        {
          date: "2026-08-27",
          total_count: 6432,
        },
      ],
      record_count: 50,
      total_count: 41200,
    },
  },
};

describe("Health page", () => {
  test("presents measured observations with the linked proactive briefing", async () => {
    const host = new FakeHost({
      authenticated: true,
      healthOverview: overview,
    });
    renderApp(host);

    await navigateTo("Health");

    expect(
      await screen.findByRole("heading", { name: "Health" }),
    ).toBeInTheDocument();
    expect(
      within(
        await screen.findByRole("region", { name: "Last sleep" }),
      ).getByText("7h 30m"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "Exercise" })).getByText(
        "2 workouts",
      ),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "Steps" })).getByText(
        "6,432 latest day",
      ),
    ).toBeInTheDocument();
    const plans = screen.getByRole("region", { name: "Exercise plans" });
    expect(within(plans).getByText("Home strength")).toBeVisible();
    expect(within(plans).getByText("Mon 18:00 to 20:00")).toBeVisible();
    const adherence = screen.getByRole("region", { name: "Plan adherence" });
    expect(within(adherence).getByText("No matching workout")).toBeVisible();
    expect(screen.getByRole("link", { name: "Open briefing" })).toHaveAttribute(
      "href",
      "/chat?turn=019f0000-0000-7000-8000-000000000002",
    );
  });

  test("names a missed-window briefing as a check-in", async () => {
    const missed = structuredClone(overview);
    missed.moments[0].kind = "missed_exercise";
    const host = new FakeHost({ authenticated: true, healthOverview: missed });
    renderApp(host);

    await navigateTo("Health");

    expect(
      within(
        await screen.findByRole("region", { name: "Proactive briefings" }),
      ).getByText("Planned exercise check-in"),
    ).toBeVisible();
  });

  test("presents absent measurements as missing rather than zero", async () => {
    const missing = structuredClone(overview);
    missing.primary_sleep.selected_episode = null;
    missing.primary_sleep.status = "no_matching_episode";
    missing.summary.exercise.record_count = 0;
    missing.summary.exercise.total_duration_minutes = 0;
    missing.summary.heart_rate.average_bpm = null;
    missing.summary.heart_rate.sample_count = 0;
    missing.summary.steps.daily = [];
    missing.summary.steps.record_count = 0;
    missing.summary.steps.total_count = 0;
    const host = new FakeHost({ authenticated: true, healthOverview: missing });
    renderApp(host);

    await navigateTo("Health");

    expect(
      within(await screen.findByRole("region", { name: "Exercise" })).getByText(
        "No data",
      ),
    ).toBeVisible();
    expect(
      within(screen.getByRole("region", { name: "Steps" })).getByText(
        "No data",
      ),
    ).toBeVisible();
    expect(
      within(screen.getByRole("region", { name: "Heart rate" })).getByText(
        "No data",
      ),
    ).toBeVisible();
  });
});
