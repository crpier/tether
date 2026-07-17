import { defineConfig } from "@playwright/test";

/**
 * End-to-end suite config.
 *
 * The runner drives an already-running web dev server (which proxies /api + /ws
 * to a running host). The bash harness `scripts/validate-web-smoke.sh` owns the
 * host + dev-server lifecycle on ephemeral ports and points us at it via
 * `TETHER_E2E_BASE_URL`. For interactive dev, run `just host` + `just web` and
 * point at the default `http://127.0.0.1:5000`.
 *
 * Headed vs headless is configurable: set `TETHER_E2E_HEADED=1` (or pass
 * `--headed`) to watch the browser; the default is headless for gate runs.
 *
 * The live-LLM chat spec is gated by `TETHER_E2E_LLM=1` (see chat-llm.spec.ts);
 * it is skipped by default so the gate stays deterministic and token-free.
 */
const baseURL =
  process.env.TETHER_E2E_BASE_URL ??
  process.env.TETHER_SMOKE_WEB_URL ??
  "http://127.0.0.1:5000";

const headed = process.env.TETHER_E2E_HEADED === "1";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  // Tests share one host + one SQLite DB, and the reminder spec writes to it;
  // run serially so specs cannot race each other's state.
  fullyParallel: false,
  workers: 1,
  forbidOnly: process.env.CI !== undefined,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL,
    headless: !headed,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
