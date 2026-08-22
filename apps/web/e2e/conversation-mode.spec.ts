import { expect, test } from "./fixtures";

/**
 * Voice conversation (#576): one composer control owns start and end. Provider
 * requests and playback stay deterministic in host and web unit tests.
 */
test("one voice control starts and ends conversation", async ({
  page,
  login,
}) => {
  await login();

  const start = page.getByRole("button", { name: "Start voice conversation" });
  await expect(start).toBeVisible();
  await expect(start).toHaveAttribute("aria-pressed", "false");

  await start.click();
  const end = page.getByRole("button", { name: "End voice conversation" });
  await expect(end).toHaveAttribute("aria-pressed", "true");

  await end.click();
  await expect(start).toHaveAttribute("aria-pressed", "false");
});
