/**
 * Pure collection + evaluation of page failures observed during an end-to-end
 * run, plus a thin Playwright seam to feed it. The classification logic (which
 * console levels and HTTP statuses count as failures) is free of any Playwright
 * runtime import so it can be unit-tested without launching a browser;
 * `attachListeners` is the only Playwright-aware function and is small enough to
 * drive with a fake page.
 */

import type { Page } from "@playwright/test";

const SERVER_ERROR_THRESHOLD = 500;

export interface Failure {
  kind: string;
  detail: string;
}

export interface ErrorCollector {
  failures: Failure[];
  onConsole: (type: string, text: string) => void;
  onPageError: (message: string) => void;
  onResponse: (status: number, url: string) => void;
  onRequestFailed: (url: string, errorText: string) => void;
}

export function createErrorCollector(): ErrorCollector {
  const failures: Failure[] = [];
  return {
    failures,
    onConsole(type, text) {
      if (type === "error") {
        failures.push({ kind: "console.error", detail: text });
      }
    },
    onPageError(message) {
      failures.push({ kind: "pageerror", detail: message });
    },
    onResponse(status, url) {
      if (status >= SERVER_ERROR_THRESHOLD) {
        failures.push({ kind: `http ${String(status)}`, detail: url });
      }
    },
    onRequestFailed(url, errorText) {
      // ERR_ABORTED is a client-side cancellation (navigation, superseded
      // fetch, closed page), not a server or page error — ignore it so the
      // gate stays free of false positives. Genuine failures such as
      // ERR_CONNECTION_REFUSED still count.
      if (errorText.includes("ERR_ABORTED")) {
        return;
      }
      failures.push({ kind: "requestfailed", detail: `${url} (${errorText})` });
    },
  };
}

/**
 * Wire a Playwright `Page`'s diagnostic events into a collector. Kept tiny so
 * the bulk of the logic stays in the pure helpers above.
 */
export function attachListeners(page: Page, collector: ErrorCollector): void {
  page.on("console", (message) => {
    collector.onConsole(message.type(), message.text());
  });
  page.on("pageerror", (error) => {
    collector.onPageError(error.message);
  });
  page.on("response", (response) => {
    collector.onResponse(response.status(), response.url());
  });
  page.on("requestfailed", (request) => {
    const failure = request.failure();
    collector.onRequestFailed(request.url(), failure?.errorText ?? "unknown");
  });
}

export function summarizeFailures(failures: Failure[]): {
  ok: boolean;
  report: string;
} {
  if (failures.length === 0) {
    return {
      ok: true,
      report: "No console errors, page errors, or failed requests.",
    };
  }
  const lines = failures.map(
    (failure, index) =>
      `  ${String(index + 1)}. [${failure.kind}] ${failure.detail}`,
  );
  return {
    ok: false,
    report: `${String(failures.length)} failure(s):\n${lines.join("\n")}`,
  };
}
