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

test("Browse deep-links to Ledgers", async ({ page, login }) => {
  await login();
  await page.goto("/browse/ledgers", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Browse" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Ledgers" })).toBeVisible();
  await expect(page.getByText("No approved Ledgers")).toBeVisible();
});

test("Browse deep-links to Feedback", async ({ page, login }) => {
  await login();
  await page.goto("/browse/feedback", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Browse" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Feedback" })).toBeVisible();
  await expect(page.getByText("No open product observations")).toBeVisible();
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
});

test("Browse tab clicks update the shareable URL", async ({ page, login }) => {
  await login();
  await page.goto("/browse", { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Reminders" }).click();
  await expect(page).toHaveURL(/\/browse\/reminders$/);
  await page.getByRole("tab", { name: "Todos" }).click();
  await expect(page).toHaveURL(/\/browse\/todos$/);
});

test("direct Bucket browse mounts no inactive Memory controls", async ({
  page,
  login,
}) => {
  await login();
  await page.goto("/browse/bucket", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("region", { name: "Bucket" })).toBeVisible();
  await expect(page.getByLabel("Search Memory")).toHaveCount(0);
});

test("selecting Bucket unmounts Memory Topics", async ({ page, login }) => {
  await login();
  await page.goto("/browse", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("region", { name: "Memory Topics" }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Bucket" }).click();
  await expect(page.getByRole("region", { name: "Bucket" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Memory Topics" })).toHaveCount(
    0,
  );
});

test("Panels empty state mounts no inactive Memory controls", async ({
  page,
  login,
}) => {
  await login();
  await page.goto("/browse", { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Panels" }).click();
  await expect(page.getByRole("region", { name: "Panels" })).toBeVisible();
  await expect(page.getByLabel("Search Memory")).toHaveCount(0);
});
