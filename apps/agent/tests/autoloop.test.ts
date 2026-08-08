import { describe, expect, test } from "vitest";

import {
  browserControlText,
  isAllowedBrowserUrl,
} from "../src/autoloop-browser-extension.js";
import {
  abortableDelay,
  autoloopConfigFromEnv,
  buildExplorerPrompt,
  extractExplorerFinding,
  ensurePlaywrightBrowser,
  explorerPiArgs,
  extractExplorerReport,
  findOpenIssueByFingerprint,
  formatFindingIssueBody,
  mergeMarkdownEntries,
  renderPiJsonEvent,
  type ExplorerFinding,
} from "../src/autoloop.js";

const finding: ExplorerFinding = {
  actual: "Clicking Save leaves the dialog open with no feedback.",
  evidence: "Console is clean; network POST returns 200.",
  expected: "The dialog should close or show saved feedback.",
  fingerprint: "settings-save-feedback",
  kind: "ux",
  repro: ["Log in", "Open Settings", "Click Save"],
  suggestedLabels: ["ux", "autoloop"],
  title: "Settings save has no visible feedback",
};

describe("abortableDelay", () => {
  test("returns immediately when manual stop is requested", async () => {
    const controller = new AbortController();
    controller.abort();

    await expect(
      abortableDelay(60_000, controller.signal),
    ).resolves.toBeUndefined();
  });
});

describe("browser tool safety", () => {
  test("redacts password control values", () => {
    expect(
      browserControlText({ innerText: "", type: "password", value: "secret" }),
    ).toBe("[redacted]");
  });

  test("allows navigation only within the production origin", () => {
    expect(
      isAllowedBrowserUrl(
        "https://tether.tail2da0b1.ts.net/settings",
        "https://tether.tail2da0b1.ts.net",
      ),
    ).toBe(true);
    expect(
      isAllowedBrowserUrl(
        "https://example.com/collect",
        "https://tether.tail2da0b1.ts.net",
      ),
    ).toBe(false);
  });
});

describe("autoloopConfigFromEnv", () => {
  test("targets the production TLS hostname by default", () => {
    expect(
      autoloopConfigFromEnv({ TETHER_AUTOLOOP_CWD: "/repo" }).productionUrl,
    ).toBe("https://tether.tail2da0b1.ts.net");
  });
});

describe("ensurePlaywrightBrowser", () => {
  test("fails with the exact setup command when Chromium is missing", async () => {
    await expect(
      ensurePlaywrightBrowser("/definitely/missing/playwright-chromium"),
    ).rejects.toThrow("pnpm -C apps/agent exec playwright install chromium");
  });
});

describe("explorerPiArgs", () => {
  test("disables built-in tools, discovered extensions, and skills", () => {
    expect(explorerPiArgs("/repo/browser.ts")).toEqual([
      "--no-builtin-tools",
      "--no-extensions",
      "--no-skills",
      "--extension",
      "/repo/browser.ts",
    ]);
  });
});

describe("extractExplorerReport", () => {
  test("returns the finding and concise persistence entries", () => {
    const text = `AUTORESEARCH_RESULT_START\n${JSON.stringify({
      doNotTry: ["Do not retest Settings until issue is fixed"],
      finding,
      notes: ["Settings save flow tested"],
    })}\nAUTORESEARCH_RESULT_END`;

    expect(extractExplorerReport(text)).toEqual({
      doNotTry: ["Do not retest Settings until issue is fixed"],
      finding,
      notes: ["Settings save flow tested"],
    });
  });

  test("returns persistence entries without a finding", () => {
    const text = `AUTORESEARCH_RESULT_START\n${JSON.stringify({
      doNotTry: [],
      notes: ["Login flow healthy"],
    })}\nAUTORESEARCH_RESULT_END`;

    expect(extractExplorerReport(text)).toEqual({
      doNotTry: [],
      finding: undefined,
      notes: ["Login flow healthy"],
    });
  });
});

describe("extractExplorerFinding", () => {
  test("reads a marked JSON finding from assistant text", () => {
    const text = `notes\nAUTORESEARCH_RESULT_START\n${JSON.stringify({ found: true, ...finding })}\nAUTORESEARCH_RESULT_END`;

    expect(extractExplorerFinding(text)).toEqual(finding);
  });

  test("returns undefined when the explorer reports no finding", () => {
    const text = `AUTORESEARCH_RESULT_START\n${JSON.stringify({ found: false })}\nAUTORESEARCH_RESULT_END`;

    expect(extractExplorerFinding(text)).toBeUndefined();
  });
});

describe("mergeMarkdownEntries", () => {
  test("appends only new entries and bounds retained history", () => {
    const existing = [
      "# Explorer notes",
      ...Array.from(
        { length: 100 },
        (_, index) => `- coverage ${String(index + 1)}`,
      ),
      "",
    ].join("\n");

    const merged = mergeMarkdownEntries(existing, "# Explorer notes", [
      "coverage 100",
      "latest coverage",
    ]);

    expect(merged).not.toContain("- coverage 1\n");
    expect(merged).toContain("- coverage 100\n");
    expect(merged.match(/- coverage 100/g)).toHaveLength(1);
    expect(merged).toContain("- latest coverage\n");
    expect(
      merged.split("\n").filter((line) => line.startsWith("- ")),
    ).toHaveLength(100);
  });
});

describe("findOpenIssueByFingerprint", () => {
  test("finds an existing issue independent of title wording", () => {
    expect(
      findOpenIssueByFingerprint(
        [
          {
            body: "Autoloop fingerprint: settings-save-feedback\n",
            url: "https://github.example/issues/7",
          },
        ],
        "settings-save-feedback",
      ),
    ).toBe("https://github.example/issues/7");
  });
});

describe("formatFindingIssueBody", () => {
  test("creates a GitHub issue body with repro and production target", () => {
    expect(
      formatFindingIssueBody(finding, {
        productionUrl: "https://tether.example",
        runId: "run-123",
      }),
    ).toMatchInlineSnapshot(`
      "Found by the Pi autoloop explorer against https://tether.example.

      Run: run-123
      Autoloop fingerprint: settings-save-feedback

      ## Kind
      ux

      ## Evidence
      Console is clean; network POST returns 200.

      ## Repro
      1. Log in
      2. Open Settings
      3. Click Save

      ## Expected
      The dialog should close or show saved feedback.

      ## Actual
      Clicking Save leaves the dialog open with no feedback.
      "
    `);
  });
});

describe("buildExplorerPrompt", () => {
  test("instructs the explorer to return persistence entries without editing files", () => {
    const prompt = buildExplorerPrompt({
      doNotTry: "- destructive account deletion",
      maxActions: 8,
      notes: "- settings flow looked healthy",
      productionUrl: "https://tether.example",
    });

    expect(prompt).toContain("https://tether.example");
    expect(prompt).toContain("browser_open");
    expect(prompt).not.toContain("Read it, then update it");
    expect(prompt).toContain('"notes":["concise new coverage note"]');
    expect(prompt).toContain('"fingerprint":"stable-kebab-case-id"');
    expect(prompt).toContain("AUTORESEARCH_RESULT_START");
    expect(prompt).toContain("- destructive account deletion");
    expect(prompt).toContain("At most 8 browser actions");
    expect(prompt).toContain("confirm it with a fresh browser_snapshot");
  });
});

describe("renderPiJsonEvent", () => {
  test("renders assistant text deltas and tool starts for streaming output", () => {
    expect(
      renderPiJsonEvent({
        assistantMessageEvent: { delta: "hello", type: "text_delta" },
        type: "message_update",
      }),
    ).toBe("hello");
    expect(
      renderPiJsonEvent({
        args: { url: "https://tether.example" },
        toolName: "browser_open",
        type: "tool_execution_start",
      }),
    ).toBe('\n[tool] browser_open {"url":"https://tether.example"}\n');
  });

  test("includes useful tool error details", () => {
    expect(
      renderPiJsonEvent({
        isError: true,
        result: {
          content: [
            { type: "text", text: "page.goto: ERR_SSL_PROTOCOL_ERROR" },
          ],
        },
        toolName: "browser_open",
        type: "tool_execution_end",
      }),
    ).toBe("\n[tool:error] browser_open: page.goto: ERR_SSL_PROTOCOL_ERROR\n");
  });
});
