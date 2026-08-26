import { describe, expect, test } from "vitest";

import type { Page } from "@playwright/test";

import {
  attachListeners,
  createErrorCollector,
  summarizeFailures,
} from "./console-guard";

describe("createErrorCollector", () => {
  test("records console errors but ignores non-error console output", () => {
    const collector = createErrorCollector();
    collector.onConsole("error", "boom");
    collector.onConsole("warning", "careful");
    collector.onConsole("log", "hello");
    expect(collector.failures).toEqual([
      { kind: "console.error", detail: "boom" },
    ]);
  });

  test("records uncaught page errors", () => {
    const collector = createErrorCollector();
    collector.onPageError("ReferenceError: x is not defined");
    expect(collector.failures).toEqual([
      { kind: "pageerror", detail: "ReferenceError: x is not defined" },
    ]);
  });

  test("records 5xx responses but ignores 2xx/4xx", () => {
    const collector = createErrorCollector();
    collector.onResponse(200, "/api/ok");
    collector.onResponse(404, "/api/missing");
    collector.onResponse(500, "/api/down");
    collector.onResponse(503, "/api/unavailable");
    expect(collector.failures).toEqual([
      { kind: "http 500", detail: "/api/down" },
      { kind: "http 503", detail: "/api/unavailable" },
    ]);
  });

  test("records genuine request failures but ignores ERR_ABORTED", () => {
    const collector = createErrorCollector();
    collector.onRequestFailed("/api/login", "net::ERR_ABORTED");
    collector.onRequestFailed("/api/x", "net::ERR_CONNECTION_REFUSED");
    expect(collector.failures).toEqual([
      {
        kind: "requestfailed",
        detail: "/api/x (net::ERR_CONNECTION_REFUSED)",
      },
    ]);
  });
});

describe("summarizeFailures", () => {
  test("reports success when there are no failures", () => {
    const { ok, report } = summarizeFailures([]);
    expect(ok).toBe(true);
    expect(report).toContain("No console errors");
  });

  test("reports each failure when there are some", () => {
    const { ok, report } = summarizeFailures([
      { kind: "console.error", detail: "boom" },
      { kind: "http 500", detail: "/api/down" },
    ]);
    expect(ok).toBe(false);
    expect(report).toContain("2 failure(s)");
    expect(report).toContain("[console.error] boom");
    expect(report).toContain("[http 500] /api/down");
  });
});

interface FakeHandlers {
  console: (message: { type: () => string; text: () => string }) => void;
  pageerror: (error: { message: string }) => void;
  response: (response: { status: () => number; url: () => string }) => void;
  requestfailed: (request: {
    url: () => string;
    failure: () => { errorText: string } | null;
  }) => void;
}

function fakePage(): { page: Page; handlers: FakeHandlers } {
  const handlers: Partial<FakeHandlers> = {};
  const page = {
    on(event: keyof FakeHandlers, handler: unknown) {
      handlers[event] = handler as never;
    },
  } as unknown as Page;
  return { page, handlers: handlers as FakeHandlers };
}

describe("attachListeners", () => {
  test("wires Playwright page events into the collector", () => {
    const collector = createErrorCollector();
    const { page, handlers } = fakePage();
    attachListeners(page, collector);

    handlers.console({ type: () => "error", text: () => "boom" });
    handlers.pageerror({ message: "kaboom" });
    handlers.response({ status: () => 500, url: () => "/api/down" });
    handlers.requestfailed({
      url: () => "/api/x",
      failure: () => ({ errorText: "net::ERR_CONNECTION_REFUSED" }),
    });

    expect(collector.failures).toEqual([
      { kind: "console.error", detail: "boom" },
      { kind: "pageerror", detail: "kaboom" },
      { kind: "http 500", detail: "/api/down" },
      {
        kind: "requestfailed",
        detail: "/api/x (net::ERR_CONNECTION_REFUSED)",
      },
    ]);
  });
});
