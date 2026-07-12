import type { Locator, Page } from "@playwright/test";

import { expect, test } from "./fixtures";

// datetime-local wants a *local* "YYYY-MM-DDTHH:MM" value; build it from local
// components (not toISOString(), which is UTC) so the instant is genuinely in
// the future in every timezone — the input's `min` and the server both reject
// past times.
function localStampFromNow(offsetMs: number): string {
  const future = new Date(Date.now() + offsetMs);
  const pad = (value: number) => String(value).padStart(2, "0");
  return (
    `${String(future.getFullYear())}-${pad(future.getMonth() + 1)}-${pad(future.getDate())}` +
    `T${pad(future.getHours())}:${pad(future.getMinutes())}`
  );
}

async function createOneOffReminder(
  page: Page,
  reminders: Locator,
  label: string,
): Promise<void> {
  await reminders.locator('input[name="payload"]').fill(label);

  // Recurrence defaults to "once"; select it explicitly so the test does not
  // depend on the default and the datetime-local field is present.
  await reminders.locator('select[name="recurrence"]').selectOption("once");
  await reminders
    .locator('input[name="fire_at"]')
    .fill(localStampFromNow(60 * 60 * 1000));

  await reminders.getByRole("button", { name: "Add reminder" }).click();

  // The new reminder is rendered as a list item labelled with its payload.
  await expect(
    page.locator(`li[aria-label="Reminder: ${label}"]`),
  ).toBeVisible();
}

test("creates a one-off reminder and shows it in the list", async ({
  page,
  login,
}) => {
  await login();

  const reminders = page.locator('section[aria-label="Reminders"]');
  const label = `e2e reminder ${String(Date.now())}`;
  await createOneOffReminder(page, reminders, label);
});

test("edits a reminder and shows the updated row", async ({ page, login }) => {
  await login();

  const reminders = page.locator('section[aria-label="Reminders"]');
  const label = `e2e edit ${String(Date.now())}`;
  await createOneOffReminder(page, reminders, label);

  const row = page.locator(`li[aria-label="Reminder: ${label}"]`);
  await row.getByRole("button", { name: "Edit" }).click();

  // The form is pre-filled with the reminder; change its message and push the
  // fire time out another hour, then save.
  const updated = `${label} updated`;
  await reminders.locator('input[name="payload"]').fill(updated);
  await reminders
    .locator('input[name="fire_at"]')
    .fill(localStampFromNow(2 * 60 * 60 * 1000));
  await reminders.getByRole("button", { name: "Save reminder" }).click();

  // The row now carries the new message; the old one is gone, not duplicated.
  await expect(
    page.locator(`li[aria-label="Reminder: ${updated}"]`),
  ).toBeVisible();
  await expect(row).toHaveCount(0);
});
