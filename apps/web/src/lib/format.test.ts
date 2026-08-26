import { describe, expect, it } from "vitest";

import { formatDate, formatDateTime, formatSyncTimestamp } from "./format";

describe("formatDate", () => {
  it("renders day-first with slashes", () => {
    expect(formatDate(new Date(2026, 0, 5))).toBe("05/01/2026");
  });

  it("pads single-digit day and month", () => {
    expect(formatDate(new Date(2026, 8, 1))).toBe("01/09/2026");
  });

  it("does not pad single-digit day and month past two digits", () => {
    expect(formatDate(new Date(2026, 11, 25))).toBe("25/12/2026");
  });
});

describe("formatDateTime", () => {
  it("joins the DD/MM/YYYY date with the locale time-of-day string", () => {
    const date = new Date(2026, 0, 5, 13, 5, 0);
    expect(formatDateTime(date)).toBe(
      `05/01/2026, ${date.toLocaleTimeString()}`,
    );
  });
});

describe("formatSyncTimestamp", () => {
  it("returns the raw value when unparsable", () => {
    expect(formatSyncTimestamp("not-a-date")).toBe("not-a-date");
  });

  it("falls back to DD/MM/YYYY for timestamps more than a day old", () => {
    const twoDaysAgo = new Date(Date.now() - 2 * 24 * 60 * 60 * 1000);
    expect(formatSyncTimestamp(twoDaysAgo.toISOString())).toBe(
      formatDate(twoDaysAgo),
    );
  });
});
