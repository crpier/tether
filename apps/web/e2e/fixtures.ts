/**
 * Shared Playwright fixtures for the Tether end-to-end suite.
 *
 * Every test gets an automatic console guard: listeners are attached before the
 * test runs and, on teardown, the test fails if the page emitted any console
 * error, uncaught page error, 5xx response, or genuine (non-aborted) request
 * failure. This is the same runtime-failure net the old bespoke smoke script
 * applied, folded into the test runner so it covers every spec.
 *
 * The `login` fixture performs the password login and waits for the chat view,
 * so specs can start from an authenticated state in one line.
 */

import { test as base, expect } from "@playwright/test";

import {
  attachListeners,
  createErrorCollector,
  summarizeFailures,
} from "./console-guard";

// Time allowed for late, asynchronous errors (queries firing after mount,
// deferred renders) to surface before the guard is sampled on teardown.
const SETTLE_MS = 1000;

const APP_PASSWORD = process.env.TETHER_APP_PASSWORD ?? "dev";

interface TetherFixtures {
  login: () => Promise<void>;
}

export const test = base.extend<TetherFixtures>({
  page: async ({ page }, use) => {
    const collector = createErrorCollector();
    attachListeners(page, collector);

    await use(page);

    await page.waitForTimeout(SETTLE_MS);
    const { ok, report } = summarizeFailures(collector.failures);
    expect(ok, `page emitted runtime failures — ${report}`).toBe(true);
  },
  login: async ({ page }, use) => {
    await use(async () => {
      await page.goto("/", { waitUntil: "domcontentloaded" });
      await page.locator("#login-title").waitFor({ state: "visible" });
      await page.locator('input[name="password"]').fill(APP_PASSWORD);
      await page.getByRole("button", { name: "Log in" }).click();
      await page.locator("#chat-title").waitFor({ state: "visible" });
    });
  },
});

export { expect };
