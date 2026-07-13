import { expect, test } from "./fixtures";

test("captures a memory, tethers it from the review queue, and finds it in the corpus", async ({
  page,
  login,
}) => {
  await login();

  const memories = page.locator('section[aria-label="Memories"]');
  await expect(memories).toBeVisible();

  // Capture a unique memory through the manual capture form.
  const content = `e2e memory ${String(Date.now())}`;
  await memories.locator('input[name="capture"]').fill(content);
  await memories.getByRole("button", { name: "Capture memory" }).click();

  // It lands in the loose review queue.
  const row = page.locator(`li[aria-label="Memory: ${content}"]`);
  await expect(row).toBeVisible();

  // Tether it: the row leaves the queue and appears in the corpus view.
  await row.getByRole("button", { name: "Tether" }).click();
  await expect(row).toHaveCount(0);
  await memories.getByRole("button", { name: "Corpus" }).click();
  await expect(row).toBeVisible();

  // Keyword search over the tethered corpus finds it.
  await memories.locator('input[name="search"]').fill(content);
  await expect(row).toBeVisible();
});

test("edits a loose memory and rejects it from the review queue", async ({
  page,
  login,
}) => {
  await login();

  const memories = page.locator('section[aria-label="Memories"]');
  const content = `e2e edit ${String(Date.now())}`;
  await memories.locator('input[name="capture"]').fill(content);
  await memories.getByRole("button", { name: "Capture memory" }).click();

  const row = page.locator(`li[aria-label="Memory: ${content}"]`);
  await expect(row).toBeVisible();

  // Edit the content in place; the row re-renders under its new content.
  await row.getByRole("button", { name: "Edit" }).click();
  const updated = `${content} updated`;
  await memories.locator('textarea[name="content"]').fill(updated);
  await memories.getByRole("button", { name: "Save" }).click();
  const updatedRow = page.locator(`li[aria-label="Memory: ${updated}"]`);
  await expect(updatedRow).toBeVisible();
  await expect(row).toHaveCount(0);

  // Reject it: the memory leaves the queue for good.
  await updatedRow.getByRole("button", { name: "Reject" }).click();
  await expect(updatedRow).toHaveCount(0);
});
