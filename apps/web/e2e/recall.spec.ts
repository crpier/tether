import { expect, test } from "./fixtures";

test("renders the recall panel", async ({ page, login }) => {
  await login();

  const recall = page.locator('section[aria-label="Recall"]');
  await expect(recall).toBeVisible();
  await expect(recall.getByRole("heading", { name: "Recall" })).toBeVisible();

  // Against the harness's fresh database no study items are due, so the panel
  // must render its empty state cleanly. (When seeded data exists, due prompts
  // render instead; either way the panel must not error — the console guard
  // enforces that.)
  await expect(recall.getByText("No recall prompts due")).toBeVisible();
});
