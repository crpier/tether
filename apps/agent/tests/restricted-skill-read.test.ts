import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { afterEach, describe, expect, test } from "vitest";

import { createRestrictedSkillReadExtension } from "../src/restricted-skill-read.js";

interface RegisteredReadTool {
  execute(
    toolCallId: string,
    params: { limit?: number; offset?: number; path: string },
  ): Promise<{
    content: { text: string; type: string }[];
  }>;
  name: string;
}

const temporaryDirectories: string[] = [];

async function skillRoot(): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), "tether-skill-read-"));
  temporaryDirectories.push(root);
  return root;
}

function registeredReadTool(roots: readonly string[]): RegisteredReadTool {
  let registered: RegisteredReadTool | undefined;
  const pi = {
    registerTool(tool: RegisteredReadTool): void {
      registered = tool;
    },
  } as unknown as ExtensionAPI;
  createRestrictedSkillReadExtension(roots)(pi);
  if (registered === undefined) {
    throw new Error("read tool was not registered");
  }
  return registered;
}

afterEach(async () => {
  await Promise.all(
    temporaryDirectories
      .splice(0)
      .map((directory) => rm(directory, { force: true, recursive: true })),
  );
});

describe("bundled skill read tool", () => {
  test("reads a skill instruction file inside an allowlisted root", async () => {
    const root = await skillRoot();
    const skillPath = join(root, "SKILL.md");
    await writeFile(skillPath, "# Bundled instructions\n", "utf8");
    const read = registeredReadTool([root]);

    const result = await read.execute("call-1", { path: skillPath });

    expect(result.content).toEqual([
      { text: "# Bundled instructions\n", type: "text" },
    ]);
  });

  test("reads a bounded range from a bundled Markdown reference", async () => {
    const root = await skillRoot();
    const referencePath = join(root, "GLOSSARY.md");
    await writeFile(referencePath, "heading\nfirst\nsecond\nlast\n", "utf8");
    const read = registeredReadTool([root]);

    const result = await read.execute("call-2", {
      limit: 2,
      offset: 2,
      path: referencePath,
    });

    expect(result.content).toEqual([{ text: "first\nsecond", type: "text" }]);
  });

  test("rejects traversal to a file outside the bundled skill", async () => {
    const root = await skillRoot();
    const unrelatedPath = join(root, "..", "private.md");
    await writeFile(unrelatedPath, "not skill content", "utf8");
    const read = registeredReadTool([root]);

    await expect(
      read.execute("call-3", { path: join(root, "..", "private.md") }),
    ).rejects.toThrow("outside bundled skills");
  });

  test("rejects a symlink that escapes an allowlisted skill root", async () => {
    const root = await skillRoot();
    const unrelatedRoot = await skillRoot();
    const unrelatedPath = join(unrelatedRoot, "private.md");
    await writeFile(unrelatedPath, "not skill content", "utf8");
    const linkedPath = join(root, "REFERENCE.md");
    await symlink(unrelatedPath, linkedPath);
    const read = registeredReadTool([root]);

    await expect(read.execute("call-4", { path: linkedPath })).rejects.toThrow(
      "outside bundled skills",
    );
  });

  test("rejects an unrelated file not beneath any allowlisted root", async () => {
    const root = await skillRoot();
    const unrelatedRoot = await skillRoot();
    const unrelatedPath = join(unrelatedRoot, "private.md");
    await writeFile(unrelatedPath, "not skill content", "utf8");
    const read = registeredReadTool([root]);

    await expect(
      read.execute("call-5", { path: unrelatedPath }),
    ).rejects.toThrow("outside bundled skills");
  });

  test("rejects unsupported files even when they are inside a skill", async () => {
    const root = await skillRoot();
    const unsupportedPath = join(root, "metadata.json");
    await writeFile(unsupportedPath, "{}", "utf8");
    const read = registeredReadTool([root]);

    await expect(
      read.execute("call-6", { path: unsupportedPath }),
    ).rejects.toThrow(/only Markdown skill files are readable/i);
  });

  test("rejects non-text content disguised as a Markdown file", async () => {
    const root = await skillRoot();
    const binaryPath = join(root, "REFERENCE.md");
    await writeFile(binaryPath, Buffer.from([0xc3, 0x28]));
    const read = registeredReadTool([root]);

    await expect(read.execute("call-7", { path: binaryPath })).rejects.toThrow(
      "valid UTF-8 text",
    );
  });

  test("truncates oversized reference output", async () => {
    const root = await skillRoot();
    const referencePath = join(root, "REFERENCE.md");
    await writeFile(referencePath, "line content\n".repeat(5_000), "utf8");
    const read = registeredReadTool([root]);

    const result = await read.execute("call-8", { path: referencePath });

    expect(result.content[0]?.text).toContain("Output truncated");
    expect(
      Buffer.byteLength(result.content[0]?.text ?? "", "utf8"),
    ).toBeLessThan(52 * 1_024);
  });
});
