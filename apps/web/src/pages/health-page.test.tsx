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
      by_origin: [],
      daily: [
        {
          by_origin: [],
          date: "2026-08-27",
          duplicate_source_warning: null,
          raw_total_count: 6432,
          record_count: 8,
          total_count: 6432,
        },
      ],
      duplicate_source_warning: null,
      raw_total_count: 41200,
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
    expect(screen.getByRole("link", { name: "Open briefing" })).toHaveAttribute(
      "href",
      "/chat?turn=019f0000-0000-7000-8000-000000000002",
    );
  });
});
