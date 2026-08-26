import { describe, expect, test } from "vitest";

import {
  isPinned,
  restoredScrollTop,
  PINNED_THRESHOLD_PX,
} from "./chat-scroll";

describe("isPinned", () => {
  test("true when scrolled exactly to the bottom", () => {
    expect(isPinned(100, 200, 100)).toBe(true);
  });

  test("true when within the threshold of the bottom", () => {
    expect(isPinned(100 - PINNED_THRESHOLD_PX, 200, 100)).toBe(true);
  });

  test("false when scrolled up past the threshold", () => {
    expect(isPinned(0, 200, 100)).toBe(false);
  });

  test("respects a custom threshold", () => {
    expect(isPinned(80, 200, 100, 10)).toBe(false);
    expect(isPinned(91, 200, 100, 10)).toBe(true);
  });

  test("true for a viewport that doesn't scroll (content fits)", () => {
    expect(isPinned(0, 100, 100)).toBe(true);
  });
});

describe("restoredScrollTop", () => {
  test("shifts scrollTop by however much content grew above the fold", () => {
    // 500px of new content was prepended above the previously-visible rows.
    expect(restoredScrollTop(50, 1000, 1500)).toBe(550);
  });

  test("is a no-op when the scroll height did not change", () => {
    expect(restoredScrollTop(50, 1000, 1000)).toBe(50);
  });
});
