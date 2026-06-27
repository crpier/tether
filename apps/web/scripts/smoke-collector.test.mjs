import { describe, expect, test } from "vitest";

import {
  attachListeners,
  createErrorCollector,
  summarizeFailures,
} from "./smoke-collector.mjs";

/**
 * Minimal stand-in for a Playwright `Page`: records the handler registered for
 * each event so the test can drive the listener wiring without a real browser.
 */
function fakePage() {
  const handlers = new Map();
  return {
    on(event, handler) {
      handlers.set(event, handler);
    },
    emit(event, payload) {
      const handler = handlers.get(event);
      if (handler === undefined) {
        throw new Error(`no handler registered for "${event}"`);
      }
      handler(payload);
    },
  };
}

describe("createErrorCollector", () => {
  test("records console errors but ignores other console levels", () => {
    const collector = createErrorCollector();
    collector.onConsole("error", "boom");
    collector.onConsole("log", "just chatter");
    collector.onConsole("warning", "meh");
    expect(collector.failures).toEqual([
      { kind: "console.error", detail: "boom" },
    ]);
  });

  test("records page errors and genuine request failures", () => {
    const collector = createErrorCollector();
    collector.onPageError("Uncaught TypeError: x is not a function");
    collector.onRequestFailed("/ws", "net::ERR_CONNECTION_REFUSED");
    expect(collector.failures).toEqual([
      {
        kind: "pageerror",
        detail: "Uncaught TypeError: x is not a function",
      },
      {
        kind: "requestfailed",
        detail: "/ws (net::ERR_CONNECTION_REFUSED)",
      },
    ]);
  });

  test("ignores net::ERR_ABORTED, a benign client-side cancellation", () => {
    const collector = createErrorCollector();
    collector.onRequestFailed("/api/auth/login", "net::ERR_ABORTED");
    expect(collector.failures).toEqual([]);
  });

  test("treats 5xx responses as failures but allows 4xx and 2xx", () => {
    const collector = createErrorCollector();
    collector.onResponse(200, "/api/memories");
    collector.onResponse(401, "/api/session");
    collector.onResponse(500, "/api/triggers");
    collector.onResponse(503, "/api/models");
    expect(collector.failures).toEqual([
      { kind: "http 500", detail: "/api/triggers" },
      { kind: "http 503", detail: "/api/models" },
    ]);
  });
});

describe("summarizeFailures", () => {
  test("reports success when there are no failures", () => {
    const result = summarizeFailures([]);
    expect(result.ok).toBe(true);
    expect(result.report).toContain("No console errors");
  });

  test("reports each failure with an index when failures exist", () => {
    const result = summarizeFailures([
      { kind: "console.error", detail: "boom" },
      { kind: "http 500", detail: "/api/triggers" },
    ]);
    expect(result.ok).toBe(false);
    expect(result.report).toContain("2 failure(s)");
    expect(result.report).toContain("1. [console.error] boom");
    expect(result.report).toContain("2. [http 500] /api/triggers");
  });
});

describe("attachListeners", () => {
  test("wires Playwright page events into the collector", () => {
    const page = fakePage();
    const collector = createErrorCollector();
    attachListeners(page, collector);

    page.emit("console", { type: () => "error", text: () => "render crash" });
    page.emit("console", { type: () => "log", text: () => "ignored" });
    page.emit("pageerror", { message: "TypeError: boom" });
    page.emit("response", { status: () => 502, url: () => "/api/models" });
    page.emit("response", { status: () => 200, url: () => "/api/session" });
    page.emit("requestfailed", {
      url: () => "/ws",
      failure: () => ({ errorText: "net::ERR_CONNECTION_REFUSED" }),
    });

    expect(collector.failures).toEqual([
      { kind: "console.error", detail: "render crash" },
      { kind: "pageerror", detail: "TypeError: boom" },
      { kind: "http 502", detail: "/api/models" },
      { kind: "requestfailed", detail: "/ws (net::ERR_CONNECTION_REFUSED)" },
    ]);
  });
});
