import { expect, test } from "./fixtures";

test("captures a memory, tethers it from the review queue, and finds it in the corpus", async ({
  page,
  login,
}) => {
  await login();

  const memories = page.locator('section[aria-label="Memories"]');
  await expect(memories).toBeVisible();

  // Capture two unique memories so the search assertion below can check the
  // relevance ranking between them, not just the presence of a row that the
  // corpus browse already rendered.
  const stamp = String(Date.now());
  const content = `e2e aisle seats ${stamp}`;
  const decoy = `e2e peanut allergy ${stamp}`;
  for (const text of [content, decoy]) {
    await memories.locator('input[name="capture"]').fill(text);
    await memories.getByRole("button", { name: "Capture memory" }).click();
    // It lands in the loose review queue.
    await expect(
      page.locator(`li[aria-label="Memory: ${text}"]`),
    ).toBeVisible();
  }
  const row = page.locator(`li[aria-label="Memory: ${content}"]`);
  const decoyRow = page.locator(`li[aria-label="Memory: ${decoy}"]`);

  // Tether both: the rows leave the queue and appear in the corpus view.
  for (const queued of [row, decoyRow]) {
    await queued.getByRole("button", { name: "Tether" }).click();
    await expect(queued).toHaveCount(0);
  }
  await memories.getByRole("button", { name: "Corpus" }).click();
  await expect(row).toBeVisible();
  await expect(decoyRow).toBeVisible();

  // Keyword search over the tethered corpus. The host's search is ranked
  // recall over every tethered memory (the vector arm has no score cutoff),
  // so the decoy never drops out; what discriminates is the request itself —
  // it only exists once the debounce fired — plus the relevance order it
  // returns: the exact-content match ranks first, ahead of the decoy.
  const searchDone = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/memories/search",
    { timeout: 5000 },
  );
  await memories.locator('input[name="search"]').fill(content);
  await searchDone;
  await expect(row).toBeVisible();
  await expect(memories.locator("ul > li").first()).toHaveAttribute(
    "aria-label",
    `Memory: ${content}`,
  );
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
