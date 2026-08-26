import { expect, test } from "./fixtures";

test("chat session controls and message actions work together", async ({
  page,
  login,
}) => {
  await login();
  const conversations = (await page.evaluate(() =>
    fetch("/api/conversations").then((response) => response.json()),
  )) as { id: string }[];
  const conversationId = conversations[0]?.id;
  expect(conversationId).toBeTruthy();

  const messageId = "018f0000-0000-7000-8000-0000000000a1";
  let feedbackBody: unknown;
  let undoBody: unknown;
  await page.route(
    `**/api/conversations/${conversationId}/messages*`,
    (route) =>
      route.fulfill({
        contentType: "application/json",
        json: [
          {
            content: "The model selector should be clearer.",
            conversation_id: conversationId,
            created_at: "2026-01-01T00:00:00Z",
            id: messageId,
            pi_message_id: null,
            role: "user",
            seq: 1,
            tool_args: null,
            tool_name: null,
            tool_result: null,
          },
          {
            content: "archive_gmail_message",
            conversation_id: conversationId,
            created_at: "2026-01-01T00:00:01Z",
            id: "018f0000-0000-7000-8000-0000000000a2",
            pi_message_id: "archive-call",
            role: "tool",
            seq: 2,
            tool_args: { message_id: "gmail-message-1" },
            tool_name: "archive_gmail_message",
            tool_result: {
              details: {
                result: { message_id: "gmail-message-1", outcome: "done" },
              },
            },
          },
        ],
        status: 200,
      }),
  );
  await page.route(`**/api/conversations/${conversationId}/read`, (route) =>
    route.fulfill({
      contentType: "application/json",
      json: { id: conversationId, last_read_seq: 2 },
      status: 200,
    }),
  );
  await page.route("**/api/product-observations", async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    feedbackBody = route.request().postDataJSON();
    await route.fulfill({
      contentType: "application/json",
      json: {
        conversation_id: conversationId,
        created_at: "2026-01-01T00:00:02Z",
        id: "018f0000-0000-7000-8000-0000000000f1",
        interpretation: "Name the active profile.",
        message_id: messageId,
        resolved_at: null,
        status: "open",
        updated_at: "2026-01-01T00:00:02Z",
        version: 1,
        wording: "The model selector should be clearer.",
      },
      status: 200,
    });
  });
  await page.route("**/api/gmail/actions/undo", async (route) => {
    undoBody = route.request().postDataJSON();
    await route.fulfill({
      contentType: "application/json",
      json: {
        detail: null,
        message_id: "gmail-message-1",
        outcome: "done",
      },
      status: 200,
    });
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByRole("slider", { name: "Model profile" }).waitFor();

  await page.getByRole("button", { name: "Search transcript" }).click();
  await page
    .getByRole("searchbox", { name: "Search transcript" })
    .fill("selector");
  await expect(page.getByText("1 match")).toBeVisible();

  await expect(page.getByRole("button", { name: "Quote message" })).toHaveCount(
    0,
  );
  await expect(
    page.getByRole("button", { name: "Copy message" }).locator("svg"),
  ).toBeVisible();
  const feedback = page.getByRole("button", {
    name: "Record product feedback",
  });
  await expect(feedback.locator("svg")).toBeVisible();
  await feedback.click();
  await page
    .getByRole("textbox", { name: "Expected behavior" })
    .fill("Name the active profile.");
  await page.getByRole("button", { name: "Save feedback" }).click();
  await expect(page.getByText("Feedback recorded.")).toBeVisible();
  expect(feedbackBody).toEqual({
    conversation_id: conversationId,
    interpretation: "Name the active profile.",
    message_id: messageId,
  });

  await page.getByRole("button", { name: "Undo archive" }).click();
  await expect(page.getByText("Restored to Inbox")).toBeVisible();
  expect(undoBody).toEqual({
    action: "archive",
    message_id: "gmail-message-1",
  });
});
