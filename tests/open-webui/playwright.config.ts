import { defineConfig } from "@playwright/test";

const baseURL = process.env.TETHER_SMOKE_WEBUI_URL;
if (baseURL === undefined) {
  throw new Error(
    "TETHER_SMOKE_WEBUI_URL is required; run the standalone validation script",
  );
}

export default defineConfig({
  testDir: "./specs",
  testMatch: "**/*.spec.ts",
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  retries: 0,
  reporter: [["list"]],
  timeout: 30_000,
  expect: { timeout: 5_000 },
  use: {
    baseURL,
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
