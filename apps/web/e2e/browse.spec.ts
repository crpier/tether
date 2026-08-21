import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

test("Browse tab query deep-links to Reminders", async ({ page, login }) => {
  await login();

  await page.goto("/browse?tab=reminders", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Browse" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Reminders" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Reminders" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
});

test("Browse deep-links to inspectable Dream history", async ({
  page,
  login,
}) => {
  await login();

  await page.goto("/browse/dreaming", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Browse" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Dreaming" })).toBeVisible();
  await expect(page.getByText("No Dream runs yet")).toBeVisible();
  await expect(page.getByRole("tab", { name: "Dreaming" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
});

test("Browse tab clicks update the shareable URL", async ({ page, login }) => {
  await login();

  await page.goto("/browse", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Browse" })).toBeVisible();

  await page.getByRole("tab", { name: "Reminders" }).click();
  await expect(page.getByRole("region", { name: "Reminders" })).toBeVisible();
  await expect(page).toHaveURL(/\/browse\/reminders$/);

  await page.getByRole("tab", { name: "Todos" }).click();
  await expect(page.getByRole("region", { name: "Todos" })).toBeVisible();
  await expect(page).toHaveURL(/\/browse\/todos$/);
});

test("direct Bucket browse exposes no inactive Memories controls", async ({
  page,
  login,
}) => {
  await login();

  const content = `e2e bucket-only a11y ${String(Date.now())}`;
  await acceptMemory(page, content);

  await page.goto("/browse/bucket", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Browse" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Bucket" })).toBeVisible();

  const snapshot = await page.locator("body").ariaSnapshot({ mode: "ai" });
  expect(snapshot).not.toContain("Search memories");
  expect(snapshot).not.toContain(`Edit Memory: ${content}`);
  expect(snapshot).not.toContain(`Reject Memory: ${content}`);
});

test("selecting Bucket Active exposes no inactive Memories controls", async ({
  page,
  login,
}) => {
  await login();

  const content = `e2e bucket-active a11y ${String(Date.now())}`;
  await acceptMemory(page, content);

  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("link", { name: /^Browse/ })
    .click();
  await expect(page.getByRole("heading", { name: "Browse" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Memories" })).toBeVisible();

  await page.getByRole("tab", { name: "Bucket" }).click();
  await expect(page.getByRole("region", { name: "Bucket" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Active" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByText("Nothing in the bucket yet")).toBeVisible();

  const snapshot = await page.locator("body").ariaSnapshot({ mode: "ai" });
  expect(snapshot).not.toContain("Search memories");
  expect(snapshot).not.toContain(`Edit Memory: ${content}`);
  expect(snapshot).not.toContain(`Reject Memory: ${content}`);
});

async function acceptMemory(page: Page, content: string) {
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
}

test("Panels empty state exposes no inactive Memories controls", async ({
  page,
  login,
}) => {
  await login();

  const content = `e2e panels-only a11y ${String(Date.now())}`;
  await acceptMemory(page, content);

  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("link", { name: /^Browse/ })
    .click();
  await expect(page.getByRole("heading", { name: "Browse" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Memories" })).toBeVisible();

  await page.getByRole("tab", { name: "Panels" }).click();
  await expect(page.getByRole("region", { name: "Panels" })).toBeVisible();
  await expect(
    page.getByText("Panels are saved views over your memories"),
  ).toBeVisible();

  const snapshot = await page.locator("body").ariaSnapshot({ mode: "ai" });
  expect(snapshot).not.toContain("Search memories");
  expect(snapshot).not.toContain(`Edit Memory: ${content}`);
  expect(snapshot).not.toContain(`Reject Memory: ${content}`);
});
