// @vitest-environment node

import { mkdtemp, readdir, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, test } from "vitest";
import { build } from "vite";

const webRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);

let outputDir: string | undefined;

afterEach(async () => {
  if (outputDir === undefined) {
    return;
  }
  await rm(outputDir, { recursive: true, force: true });
  outputDir = undefined;
});

async function readJavaScriptFiles(directory: string): Promise<string[]> {
  const entries = await readdir(directory, { withFileTypes: true });
  const contents: string[] = [];
  for (const entry of entries) {
    const entryPath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      contents.push(...(await readJavaScriptFiles(entryPath)));
    } else if (entry.name.endsWith(".js")) {
      contents.push(await readFile(entryPath, "utf8"));
    }
  }
  return contents;
}

describe("production build", () => {
  test("does not ship Solid Devtools runtime or warnings", async () => {
    outputDir = await mkdtemp(path.join(os.tmpdir(), "tether-web-build-"));

    await build({
      root: webRoot,
      logLevel: "silent",
      build: {
        outDir: outputDir,
        emptyOutDir: true,
        minify: false,
      },
    });

    const bundle = (await readJavaScriptFiles(outputDir)).join("\n");
    expect(bundle).not.toContain("SolidDevtools$$");
    expect(bundle).not.toContain("entry point for the vite plugin");
  }, 30_000);
});
