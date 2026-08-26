import { describe, expect, test } from "vitest";

import { httpStatusMessage } from "./http-errors";

describe("httpStatusMessage", () => {
  test("maps common statuses to friendly text", () => {
    expect(httpStatusMessage(401)).toBe("Please sign in to continue.");
    expect(httpStatusMessage(403)).toBe(
      "You don't have permission to do that.",
    );
    expect(httpStatusMessage(404)).toBe("We couldn't find what you asked for.");
    expect(httpStatusMessage(409)).toBe(
      "That changed elsewhere. Refresh and try again.",
    );
    expect(httpStatusMessage(429)).toBe(
      "Too many requests. Please wait a moment and try again.",
    );
    expect(httpStatusMessage(500)).toBe(
      "Something went wrong on the server. Please try again.",
    );
    expect(httpStatusMessage(503)).toBe(
      "The service is temporarily unavailable. Please try again.",
    );
  });

  test("never surfaces the raw status code in the default message", () => {
    const message = httpStatusMessage(418);
    expect(message).not.toContain("418");
    expect(message).toBe("The request could not be completed.");
  });

  test("applies per-call overrides ahead of the defaults", () => {
    expect(httpStatusMessage(401, { 401: "Incorrect password." })).toBe(
      "Incorrect password.",
    );
    // Statuses without an override still fall back to the defaults.
    expect(httpStatusMessage(500, { 401: "Incorrect password." })).toBe(
      "Something went wrong on the server. Please try again.",
    );
  });
});
