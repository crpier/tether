import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

/**
 * Artifact linking (#188, ADR 0011): a settled assistant message with an
 * `artifact` fence should render an artifact-card in chat; opening it should
 * raise the in-app overlay and mount the artifact's HTML in a sandboxed
 * iframe with `sandbox="allow-scripts"` and no `allow-same-origin`.
 *
 * As with `chat-widgets.spec.ts`, there is no route to seed a fixture message
 * into a conversation without a live model turn — this intercepts the
 * browser's outgoing REST calls for the conversation list, its messages, and
 * the artifact GET, and serves deterministic fixtures. No LLM cost, no
 * flakiness, and the suite's standard console guard (see fixtures.ts) still
 * applies to whatever the real DOM/iframe machinery does in the page.
 */

const CONVERSATION_ID = "01930000-0000-7000-8000-000000000003";
const MESSAGE_ID = "01930000-0000-7000-8000-000000000004";
const ARTIFACT_ID = "01930000-0000-7000-8000-000000000005";

const ARTIFACT_MESSAGE_TEXT = [
  "Here's a small quiz artifact.",
  "",
  "```artifact",
  JSON.stringify({ id: ARTIFACT_ID, title: "Async IO Quiz" }),
  "```",
].join("\n");

const ARTIFACT_HTML =
  "<!doctype html><html><head><title>Quiz</title></head>" +
  "<body><p>What multiplexes async IO?</p></body></html>";

async function mockArtifactConversation(page: Page): Promise<void> {
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
          title: "Artifact fixture",
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
            content: ARTIFACT_MESSAGE_TEXT,
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

  await page.route(`**/api/artifacts/${ARTIFACT_ID}`, async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    await route.fulfill({
      json: {
        id: ARTIFACT_ID,
        title: "Async IO Quiz",
        html: ARTIFACT_HTML,
        version: 1,
        created_at: new Date().toISOString(),
      },
    });
  });
}

test("an artifact fence renders a card, opens an overlay, and mounts a sandboxed iframe", async ({
  page,
  login,
}) => {
  await mockArtifactConversation(page);
  await login();

  const message = page.getByLabel("Tether message");
  await expect(message).toBeVisible();

  const card = message.locator("[data-widget='artifact']");
  await expect(card).toBeVisible();
  await expect(card).toContainText("Async IO Quiz");
  await expect(
    message.locator("pre code.language-artifact"),
  ).not.toBeAttached();

  await card.getByRole("button", { name: "Open" }).click();

  const overlay = page.getByLabel("Artifact viewer");
  await expect(overlay).toBeVisible();
  await expect(overlay).toContainText("Async IO Quiz");
  await expect(overlay).toContainText("Version 1");

  const iframe = overlay.locator("iframe");
  await expect(iframe).toBeVisible();
  await expect(iframe).toHaveAttribute("sandbox", "allow-scripts");
  expect(await iframe.getAttribute("allow-same-origin")).toBeNull();
  await expect(
    iframe.contentFrame().getByText("What multiplexes async IO?"),
  ).toBeVisible();

  await overlay.getByRole("button", { name: "Close artifact" }).click();
  await expect(overlay).not.toBeAttached();
});
