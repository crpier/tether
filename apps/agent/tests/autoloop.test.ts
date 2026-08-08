import { describe, expect, test } from "vitest";

import {
  buildExplorerPrompt,
  extractExplorerFinding,
  formatFindingIssueBody,
  renderPiJsonEvent,
  type ExplorerFinding,
} from "../src/autoloop.js";

const finding: ExplorerFinding = {
  actual: "Clicking Save leaves the dialog open with no feedback.",
  evidence: "Console is clean; network POST returns 200.",
  expected: "The dialog should close or show saved feedback.",
  kind: "ux",
  repro: ["Log in", "Open Settings", "Click Save"],
  suggestedLabels: ["ux", "autoloop"],
  title: "Settings save has no visible feedback",
};

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
  test("instructs the explorer to use browser tools, persist notes, and emit marked JSON", () => {
    const prompt = buildExplorerPrompt({
      doNotTry: "- destructive account deletion",
      maxActions: 8,
      notes: "- settings flow looked healthy",
      notesPath: ".tether/autoloop/explorer-notes.md",
      productionUrl: "https://tether.example",
    });

    expect(prompt).toContain("https://tether.example");
    expect(prompt).toContain("browser_open");
    expect(prompt).toContain(".tether/autoloop/explorer-notes.md");
    expect(prompt).toContain("AUTORESEARCH_RESULT_START");
    expect(prompt).toContain("- destructive account deletion");
    expect(prompt).toContain("At most 8 browser actions");
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
});
