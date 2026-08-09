import { expect, test } from "./fixtures";

test("settings reports the server-owned model provider", async ({
  page,
  login,
}) => {
  await login();
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("link", { name: /^Settings/u })
    .click();

  const provider = page.getByRole("region", { name: "Model provider" });
  await expect(provider).toBeVisible();
  await expect(provider).toContainText("OpenAI Codex");
  await expect(provider).toContainText(
    /Connected|Not connected|Status unavailable/u,
  );
  await expect(provider).not.toContainText("ChatGPT");
});

test("unknown routes show not found without redirecting", async ({
  page,
  login,
}) => {
  await login();
  await page.goto("/not-a-real-route-autoresearch", {
    waitUntil: "domcontentloaded",
  });

  await expect(
    page.getByRole("heading", { level: 1, name: "Page not found" }),
  ).toBeVisible();
  await expect(page).toHaveURL(/\/not-a-real-route-autoresearch$/u);
  await expect(page.locator("#chat-title")).toBeHidden();
});

test("direct queue proposals route loads the queue tab", async ({
  page,
  login,
}) => {
  await login();
  await page.goto("/proposals/queue", { waitUntil: "domcontentloaded" });

  await expect(
    page.getByRole("heading", { exact: true, name: "Proposals" }),
  ).toBeVisible();
  await expect(page.getByRole("tab", { name: /Queue/u })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(
    page.getByRole("heading", { level: 1, name: "Page not found" }),
  ).toBeHidden();
});

test("direct decided proposals route loads the decided tab", async ({
  page,
  login,
}) => {
  await login();
  await page.goto("/proposals/decided", { waitUntil: "domcontentloaded" });

  await expect(
    page.getByRole("heading", { exact: true, name: "Proposals" }),
  ).toBeVisible();
  await expect(page.getByRole("tab", { name: /Decided/u })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(
    page.getByRole("heading", { level: 1, name: "Page not found" }),
  ).toBeHidden();
});

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
    await expect(
      nav.getByRole("link", { name: new RegExp(`^${label}`) }),
    ).toBeVisible();
  }
});
