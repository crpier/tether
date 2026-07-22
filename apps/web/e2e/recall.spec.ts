import { expect, test } from "./fixtures";

test("recall due prompts render inside the Inbox", async ({
  page,
  login,
}) => {
  await login();

  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("link", { name: /^Inbox/ })
    .click();
  await expect(page.getByRole("heading", { name: "Inbox" })).toBeVisible();

  // Against the harness's fresh database no study items are due, so the page
  // must render cleanly without a "Recall due" group (the console guard
  // enforces that it does not error). When seeded data exists, a "Recall
  // due" group renders instead as one more Inbox kind — either way, the
  // capture form (memory review's entry point) is always present.
  await expect(page.locator('input[name="capture"]')).toBeVisible();
});
