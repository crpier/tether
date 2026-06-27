import { expect, test } from "./fixtures";

test("logs in and reaches the chat view with a clean console", async ({
  page,
  login,
}) => {
  await login();

  // Core widgets of the authenticated app must render. The automatic console
  // guard (see fixtures.ts) additionally asserts the page booted — including
  // the /ws upgrade — without any runtime error.
  await expect(page.locator("#chat-title")).toBeVisible();
  await expect(page.locator('section[aria-label="Reminders"]')).toBeVisible();
  await expect(page.locator('section[aria-label="Recall"]')).toBeVisible();
});
