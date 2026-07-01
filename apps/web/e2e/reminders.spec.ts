import { expect, test } from "./fixtures";

test("creates a one-off reminder and shows it in the list", async ({
  page,
  login,
}) => {
  await login();

  const reminders = page.locator('section[aria-label="Reminders"]');
  const label = `e2e reminder ${String(Date.now())}`;

  await reminders.locator('input[name="payload"]').fill(label);

  // Recurrence defaults to "once"; select it explicitly so the test does not
  // depend on the default and the datetime-local field is present.
  await reminders.locator('select[name="recurrence"]').selectOption("once");

  // datetime-local wants a *local* "YYYY-MM-DDTHH:MM" value; build it from local
  // components (not toISOString(), which is UTC) so the instant is genuinely in
  // the future in every timezone — the input's `min` and the server both reject
  // past times.
  const future = new Date(Date.now() + 60 * 60 * 1000);
  const pad = (value: number) => String(value).padStart(2, "0");
  const fireAt =
    `${String(future.getFullYear())}-${pad(future.getMonth() + 1)}-${pad(future.getDate())}` +
    `T${pad(future.getHours())}:${pad(future.getMinutes())}`;
  await reminders.locator('input[name="fire_at"]').fill(fireAt);

  await reminders.getByRole("button", { name: "Add reminder" }).click();

  // The new reminder is rendered as a list item labelled with its payload.
  await expect(
    page.locator(`li[aria-label="Reminder: ${label}"]`),
  ).toBeVisible();
});
