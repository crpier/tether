import { expect, test } from "./fixtures";

test("logs in and lands on pure chat, with the nav present", async ({
  page,
  login,
}) => {
  await login();

  // Chat is the home page and the whole page: transcript + composer, nothing
  // else (#250). The console guard (fixtures.ts) additionally asserts the
  // page booted — including the /ws upgrade — without any runtime error.
  await expect(page.locator("#chat-title")).toBeVisible();
  await expect(
    page.locator('section[aria-label="Chat transcript"]'),
  ).toBeVisible();

  // The nav's five destinations are reachable from chat (desktop sidebar by
  // default in the Playwright viewport).
  const nav = page.getByRole("navigation", { name: "Main navigation" });
  await expect(nav).toBeVisible();
  for (const label of ["Chat", "Proposals", "Inbox", "Browse", "Settings"]) {
    await expect(nav.getByRole("link", { name: new RegExp(`^${label}`) })).toBeVisible();
  }
});
