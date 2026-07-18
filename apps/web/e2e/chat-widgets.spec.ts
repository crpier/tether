import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

/**
 * Widget vocabulary v1 (#187, ADR 0011): a settled assistant message with a
 * `mermaid` fence, a `vega-lite` fence, and a GFM table should mount real
 * rendered widgets in a real browser — the class of bug static/jsdom checks
 * can't catch (mismatched library versions, real SVG failing to mount,
 * console errors from the actual rendering libraries).
 *
 * There is no route to seed a fixture message into a conversation without a
 * live model turn, and adding one is out of scope for this ticket. Instead
 * this intercepts the browser's outgoing REST calls for the conversation list
 * and its messages and serves a deterministic fixture — no LLM cost, no
 * flakiness, and the suite's standard console guard (see fixtures.ts) still
 * applies to whatever the rendering libraries do in the page.
 */

const CONVERSATION_ID = "01930000-0000-7000-8000-000000000001";
const MESSAGE_ID = "01930000-0000-7000-8000-000000000002";

const WIDGET_MESSAGE_TEXT = [
  "Here's the widget vocabulary in one message.",
  "",
  "| Widget | Renderer |",
  "| --- | --- |",
  "| Table | native |",
  "| Diagram | mermaid |",
  "",
  "```mermaid",
  "graph TD;",
  "  A[Spec] --> B[Widget];",
  "```",
  "",
  "```vega-lite",
  JSON.stringify({
    $schema: "https://vega.github.io/schema/vega-lite/v5.json",
    data: {
      values: [
        { a: "A", b: 10 },
        { a: "B", b: 20 },
      ],
    },
    mark: "bar",
    encoding: {
      x: { field: "a", type: "nominal" },
      y: { field: "b", type: "quantitative" },
    },
  }),
  "```",
].join("\n");

async function mockWidgetConversation(page: Page): Promise<void> {
  await page.route("**/api/conversations", async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    await route.fulfill({
      json: [
        {
          id: CONVERSATION_ID,
          pi_session_id: CONVERSATION_ID,
          title: "Widget fixture",
          selected_model: null,
          session_gap_seconds: 3600,
          latest_activity: new Date().toISOString(),
          created_at: new Date().toISOString(),
        },
      ],
    });
  });

  await page.route(
    `**/api/conversations/${CONVERSATION_ID}/messages*`,
    async (route) => {
      if (route.request().method() !== "GET") {
        await route.fallback();
        return;
      }
      await route.fulfill({
        json: [
          {
            id: MESSAGE_ID,
            conversation_id: CONVERSATION_ID,
            role: "assistant",
            content: WIDGET_MESSAGE_TEXT,
            tool_name: null,
            tool_args: null,
            tool_result: null,
            pi_message_id: null,
            seq: 1,
            created_at: new Date().toISOString(),
          },
        ],
      });
    },
  );
}

test("a settled assistant message renders table, mermaid, and vega-lite widgets", async ({
  page,
  login,
}) => {
  await mockWidgetConversation(page);
  await login();

  const message = page.getByLabel("Tether message");
  await expect(message).toBeVisible();

  // GFM table: native marked/DOMPurify output, no dispatch needed.
  await expect(message.locator("table")).toBeVisible();
  await expect(message.locator("th")).toHaveCount(2);

  // Mermaid: strict-mode SVG mounted in place of the fenced code block.
  await expect(message.locator("[data-widget='mermaid'] svg")).toBeVisible();
  await expect(message.locator("pre code.language-mermaid")).not.toBeAttached();

  // Vega-Lite: vega-embed's SVG render root, actions menu disabled.
  await expect(message.locator("[data-widget='vega-lite'] svg")).toBeVisible();
  await expect(message.locator(".vega-actions")).toHaveCount(0);
});
