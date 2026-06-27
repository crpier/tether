/// <reference types="vitest" />
/// <reference types="vite/client" />

import { fileURLToPath } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import devtools from "solid-devtools/vite";
import { defineConfig } from "vitest/config";
import solidPlugin from "vite-plugin-solid";

export default defineConfig({
  plugins: [devtools(), solidPlugin(), tailwindcss()],
  server: {
    port: 3000,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ws": {
        target: "ws://127.0.0.1:8000",
        ws: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: false,
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
