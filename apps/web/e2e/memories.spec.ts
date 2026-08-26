import { expect, test } from "./fixtures";

test("Memory is a read-only Dreaming-maintained Topic surface", async ({
  page,
  login,
}) => {
  await login();

  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("link", { name: /^Browse/ })
    .click();
  await expect(page.getByRole("heading", { name: "Browse" })).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Memory Topics" }),
  ).toBeVisible();
  await expect(page.getByLabel("Search Memory")).toBeVisible();
  await expect(
    page.getByText(/Corrections happen in chat, not through direct editing/),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /edit memory/i })).toHaveCount(
    0,
  );

  const topics = await page.request.get("/api/memory-topics");
  expect(topics.status()).toBe(200);
  const removedCrud = await page.request.get("/api/memories?state=loose");
  expect(removedCrud.status()).toBe(404);
});
