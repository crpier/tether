/// <reference types="vitest" />
/// <reference types="vite/client" />

import { fileURLToPath } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import devtools from "solid-devtools/vite";
import { defineConfig } from "vitest/config";
import solidPlugin from "vite-plugin-solid";

// Proxy targets default to the local `just host` port but can be overridden so
// the web smoke harness can point the dev server at a host on an ephemeral
// port without colliding with a running dev stack.
const apiTarget = process.env.TETHER_API_TARGET ?? "http://127.0.0.1:8000";
const wsTarget = process.env.TETHER_WS_TARGET ?? "ws://127.0.0.1:8000";

export default defineConfig({
  plugins: [devtools(), solidPlugin(), tailwindcss()],
  server: {
    // Bind explicitly to IPv4 so the app is reachable at 127.0.0.1:5000, which
    // is what the Playwright e2e/MCP tooling targets. Without this Vite binds
    // to `localhost` -> IPv6 `::1` only, leaving 127.0.0.1 unreachable.
    host: "127.0.0.1",
    port: 5000,
    proxy: {
      "/api": apiTarget,
      "/ws": {
        target: wsTarget,
        ws: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: false,
    // Vitest owns the unit tests under src/ plus the e2e support helpers
    // (`e2e/**/*.test.ts`). The Playwright end-to-end specs (`e2e/**/*.spec.ts`)
    // are driven by the Playwright runner, not vitest, so they are excluded.
    include: ["src/**/*.{test,spec}.{ts,tsx}", "e2e/**/*.test.ts"],
    exclude: ["**/node_modules/**", "**/dist/**", "e2e/**/*.spec.ts"],
    setupFiles: ["node_modules/@testing-library/jest-dom/vitest"],
    // Kobalte ships untranspiled .jsx; inline it so vite-plugin-solid transforms
    // it instead of Node trying to load the raw source.
    server: {
      deps: {
        inline: [/@kobalte\/core/],
      },
    },
    // if you have few tests, try commenting this
    // out to improve performance:
    isolate: false,
  },
  build: {
    target: "esnext",
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
    conditions: ["development", "browser"],
  },
});
