/**
 * Pure collection + evaluation of page failures observed during the web smoke
 * run. Deliberately free of any Playwright import so the decision logic (which
 * console levels and HTTP statuses count as failures) can be unit-tested
 * without launching a browser. `attachListeners` is the only Playwright-aware
 * seam, and it is thin enough to drive with a fake page in tests.
 */

const SERVER_ERROR_THRESHOLD = 500;

/**
 * @typedef {{ kind: string, detail: string }} Failure
 */

export function createErrorCollector() {
  /** @type {Failure[]} */
  const failures = [];
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
export function attachListeners(page, collector) {
  page.on("console", (message) => {
    collector.onConsole(message.type(), message.text());
  });
  page.on("pageerror", (error) => {
    collector.onPageError(error.message ?? String(error));
  });
  page.on("response", (response) => {
    collector.onResponse(response.status(), response.url());
  });
  page.on("requestfailed", (request) => {
    const failure = request.failure();
    collector.onRequestFailed(request.url(), failure?.errorText ?? "unknown");
  });
}

/**
 * @param {Failure[]} failures
 * @returns {{ ok: boolean, report: string }}
 */
export function summarizeFailures(failures) {
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
