import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

const CONVERSATION_ID = "01930000-0000-7000-8000-000000000060";

function transcriptMessage(seq: number) {
  return {
    content: `History row ${seq.toString()} with enough text to keep the transcript vertically scrollable.`,
    conversation_id: CONVERSATION_ID,
    created_at: new Date(
      Date.UTC(2026, 0, 1, seq <= 30 ? 0 : 1, seq <= 30 ? seq : seq - 30),
    ).toISOString(),
    id: `01930000-0000-7000-8000-${seq.toString().padStart(12, "0")}`,
    pi_message_id: null,
    role: "user",
    seq,
    tool_args: null,
    tool_name: null,
    tool_result: null,
    turn: null,
    turn_id: null,
    turn_message_seq: null,
  };
}

async function mockLongConversation(page: Page): Promise<void> {
  await page.route("**/api/conversations", async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    await route.fulfill({
      json: [
        {
          archived_at: null,
          created_at: "2026-01-01T00:00:00Z",
          display_name: "Long history",
          has_unread: false,
          id: CONVERSATION_ID,
          kind: "main",
          last_read_seq: 60,
          latest_activity: "2026-01-01T01:00:00Z",
          latest_message_seq: 60,
          pending_turn_count: 0,
          pi_session_id: CONVERSATION_ID,
          running_turn_id: null,
          scope_brief: null,
          scope_revision: 1,
          selected_model: null,
          session_gap_seconds: 300,
          status: "active",
          title: null,
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
      const before = new URL(route.request().url()).searchParams.get(
        "before_seq",
      );
      const rows = before === "31" ? [1, 30] : [31, 60];
      await route.fulfill({
        json: Array.from({ length: rows[1] - rows[0] + 1 }, (_, index) =>
          transcriptMessage(rows[0] + index),
        ),
      });
    },
  );
}

test("prepending older history keeps the visible transcript position", async ({
  page,
  login,
}) => {
  await page.setViewportSize({ width: 390, height: 780 });
  await mockLongConversation(page);
  await login();

  const transcript = page.getByRole("log", { name: "Chat transcript" });
  const anchor = page.getByLabel("You message").filter({
    hasText: "History row 31 ",
  });
  await expect(anchor).toBeAttached();
  await transcript.evaluate((element) => {
    element.scrollTop = 50;
    element.dispatchEvent(new Event("scroll"));
  });
  const before = await anchor.boundingBox();
  expect(before).not.toBeNull();

  await expect(
    page.getByText(
      "History row 1 with enough text to keep the transcript vertically scrollable.",
      { exact: true },
    ),
  ).toBeAttached();
  await expect(
    page.getByLabel("Historical Pi session boundary"),
  ).toBeAttached();
  const after = await anchor.boundingBox();
  expect(after).not.toBeNull();
  expect(Math.abs((after?.y ?? 0) - (before?.y ?? 0))).toBeLessThanOrEqual(2);
});
