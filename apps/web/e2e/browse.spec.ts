import { expect, test } from "./fixtures";

test("direct Bucket browse exposes no inactive Memories controls", async ({
  page,
  login,
}) => {
  await login();

  const content = `e2e bucket-only a11y ${String(Date.now())}`;
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("link", { name: /^Inbox/ })
    .click();
  await page.locator('input[name="capture"]').fill(content);
  await page.getByRole("button", { name: "Capture memory" }).click();
  await page.getByRole("button", { name: content, exact: true }).click();
  await page
    .locator('[aria-label^="Inbox item: "]')
    .first()
    .getByRole("button", { name: "Accept memory" })
    .click();
  await expect(page.getByRole("button", { name: content })).toHaveCount(0);

  await page.goto("/browse/bucket", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Browse" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Bucket" })).toBeVisible();

  const snapshot = await page.locator("body").ariaSnapshot({ mode: "ai" });
  expect(snapshot).not.toContain("Search memories");
  expect(snapshot).not.toContain(`Edit Memory: ${content}`);
  expect(snapshot).not.toContain(`Reject Memory: ${content}`);
});
