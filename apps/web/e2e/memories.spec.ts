import { expect, test } from "./fixtures";

test("captures a memory, tethers it from the Inbox, and finds it in the Browse corpus", async ({
  page,
  login,
}) => {
  await login();

  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("link", { name: /^Inbox/ })
    .click();
  await expect(page.getByRole("heading", { name: "Inbox" })).toBeVisible();

  // Capture two unique memories so the search assertion below can check the
  // relevance ranking between them, not just the presence of a row that the
  // corpus browse already rendered.
  const stamp = String(Date.now());
  const content = `e2e aisle seats ${stamp}`;
  const decoy = `e2e peanut allergy ${stamp}`;
  for (const text of [content, decoy]) {
    await page.locator('input[name="capture"]').fill(text);
    await page.getByRole("button", { name: "Capture memory" }).click();
    // It lands in the loose review queue, listed in the master list.
    await expect(
      page.getByRole("button", { name: text, exact: true }),
    ).toBeVisible();
  }

  // Select and tether both from the detail pane: each row leaves the queue.
  for (const text of [content, decoy]) {
    await page.getByRole("button", { name: text, exact: true }).click();
    await page
      .locator('[aria-label^="Inbox item: "]')
      .first()
      .getByRole("button", { name: "Tether" })
      .click();
    await expect(
      page.getByRole("button", { name: text, exact: true }),
    ).toHaveCount(0);
  }

  // Both now show up in Browse's tethered corpus.
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("link", { name: /^Browse/ })
    .click();
  await expect(page.getByRole("heading", { name: "Browse" })).toBeVisible();
  const row = page.locator(`li[aria-label="Memory: ${content}"]`);
  const decoyRow = page.locator(`li[aria-label="Memory: ${decoy}"]`);
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
  await page.locator('input[name="search"]').fill(content);
  await searchDone;
  await expect(row).toBeVisible();
  await expect(
    page.locator('section[aria-label="Memories"] ul > li').first(),
  ).toHaveAttribute("aria-label", `Memory: ${content}`);
});
