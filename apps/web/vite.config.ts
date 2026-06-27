/// <reference types="vitest" />
/// <reference types="vite/client" />

import devtools from "solid-devtools/vite";
import { defineConfig } from "vitest/config";
import solidPlugin from "vite-plugin-solid";

export default defineConfig({
  plugins: [devtools(), solidPlugin()],
  server: {
    port: 3000,
  },
  test: {
    environment: "jsdom",
    globals: false,
    setupFiles: ["node_modules/@testing-library/jest-dom/vitest"],
    // if you have few tests, try commenting this
    // out to improve performance:
    isolate: false,
  },
  build: {
    target: "esnext",
  },
  resolve: {
    conditions: ["development", "browser"],
  },
});
