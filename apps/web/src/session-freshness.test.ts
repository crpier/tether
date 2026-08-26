import { describe, expect, test } from "vitest";

import { willStartFreshSession } from "./session-freshness";

const GAP_SECONDS = 300;

describe("willStartFreshSession", () => {
  test("no prior activity is treated as fresh", () => {
    expect(willStartFreshSession(null, GAP_SECONDS, Date.now())).toBe(true);
  });

  test("activity inside the gap is warm", () => {
    const now = Date.parse("2026-01-01T00:04:00Z");
    const latest = "2026-01-01T00:00:00Z";
    expect(willStartFreshSession(latest, GAP_SECONDS, now)).toBe(false);
  });

  test("activity exactly at the gap boundary is still warm", () => {
    const now = Date.parse("2026-01-01T00:05:00Z");
    const latest = "2026-01-01T00:00:00Z";
    expect(willStartFreshSession(latest, GAP_SECONDS, now)).toBe(false);
  });

  test("activity past the gap is fresh", () => {
    const now = Date.parse("2026-01-01T00:05:01Z");
    const latest = "2026-01-01T00:00:00Z";
    expect(willStartFreshSession(latest, GAP_SECONDS, now)).toBe(true);
  });

  test("an unparseable timestamp is treated as fresh", () => {
    expect(willStartFreshSession("not-a-date", GAP_SECONDS, Date.now())).toBe(
      true,
    );
  });
});
