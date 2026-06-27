/**
 * Real-browser smoke check for the Tether web SPA.
 *
 * Drives headless Chromium against an already-running web dev server (which
 * proxies `/api` + `/ws` to an already-running host), exercises the
 * unauthenticated load and the post-login chat view, and fails if the page
 * emits any console error, uncaught page error, 5xx response, or failed
 * request. The bash harness (`scripts/validate-web-smoke.sh`) owns the host +
 * dev-server lifecycle; this script only owns the browser.
 *
 * Required env:
 *   TETHER_SMOKE_WEB_URL   base URL of the running dev server (e.g. http://127.0.0.1:3000)
 *   TETHER_APP_PASSWORD    password to log in with
 * Optional env:
 *   TETHER_SMOKE_SCREENSHOT  path to write a screenshot of the final state
 */

import { chromium } from "playwright";

import {
  attachListeners,
  createErrorCollector,
  summarizeFailures,
} from "./smoke-collector.mjs";

// Time allowed for late, asynchronous errors (queries firing after mount,
// deferred renders) to surface before we sample the collector.
const SETTLE_MS = 1500;

function requireEnv(name) {
  const value = process.env[name];
  if (value === undefined || value === "") {
    throw new Error(`missing required env var ${name}`);
  }
  return value;
}

async function run() {
  const webUrl = requireEnv("TETHER_SMOKE_WEB_URL");
  const password = requireEnv("TETHER_APP_PASSWORD");
  const screenshotPath = process.env.TETHER_SMOKE_SCREENSHOT;

  const browser = await chromium.launch();
  const collector = createErrorCollector();
  try {
    const page = await browser.newPage();
    attachListeners(page, collector);

    // 1. Unauthenticated load: the SPA must boot and render the login screen.
    await page.goto(webUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#login-title").waitFor({ state: "visible" });
    await page.waitForTimeout(SETTLE_MS);

    // 2. Log in and reach the authenticated chat view.
    await page.locator('input[name="password"]').fill(password);
    await page.getByRole("button", { name: "Log in" }).click();
    await page.locator("#chat-title").waitFor({ state: "visible" });
    await page.waitForTimeout(SETTLE_MS);

    if (screenshotPath !== undefined && screenshotPath !== "") {
      await page.screenshot({ path: screenshotPath, fullPage: true });
    }
  } finally {
    await browser.close();
  }

  const { ok, report } = summarizeFailures(collector.failures);
  if (ok) {
    console.log(`web smoke passed: ${report}`);
    return;
  }
  console.error(`web smoke FAILED — ${report}`);
  process.exitCode = 1;
}

run().catch((error) => {
  console.error("web smoke crashed:", error);
  process.exitCode = 1;
});
