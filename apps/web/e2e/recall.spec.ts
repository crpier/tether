import { expect, test } from "./fixtures";

test("recall due prompts render inside the Inbox", async ({ page, login }) => {
  await login();

  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("link", { name: /^Inbox/ })
    .click();
  await expect(page.getByRole("heading", { name: "Inbox" })).toBeVisible();

  // A fresh database has no due Study items. Memory contributes no Review
  // queue or capture form to Inbox.
  await expect(
    page.getByText("Nothing awaiting you — inbox zero."),
  ).toBeVisible();
  await expect(page.getByText(/Memory review/)).toHaveCount(0);
});
