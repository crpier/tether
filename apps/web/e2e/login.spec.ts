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

test.describe("YouTube authorization", () => {
  test.use({ serviceWorkers: "block" });

  test("can be completed from Settings", async ({ page, login }) => {
    await page.route("**/api/youtube-auth**", async (route) => {
      if (new URL(route.request().url()).pathname.endsWith("/callback")) {
        await route.fulfill({ status: 204 });
        return;
      }
      const authorizationUrl = new URL(
        "/api/youtube-auth/callback?state=fake-state&code=fake-code",
        page.url() || "http://127.0.0.1",
      ).href;
      await route.fulfill({
        contentType: "application/json",
        json:
          route.request().method() === "POST"
            ? {
                authorization_url: authorizationUrl,
                error: null,
                state: "authorizing",
              }
            : {
                authorization_url: null,
                error: null,
                state: "disconnected",
              },
      });
    });
    await login();
    await page
      .getByRole("navigation", { name: "Main navigation" })
      .getByRole("link", { name: /^Settings/u })
      .click();

    const youtube = page.getByRole("region", { name: "YouTube sync" });
    await youtube.getByRole("button", { name: "Connect YouTube" }).click();
    await youtube.getByRole("link", { name: "Continue with Google" }).click();
    await expect(page).toHaveURL(/\/api\/youtube-auth\/callback\?/u);
    await page.unroute("**/api/youtube-auth**");
    await page.route("**/api/youtube-auth", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        json: {
          authorization_url: null,
          error: null,
          state: "connected",
        },
      });
    });
    await page.goto("/settings?youtube_auth=connected");

    await expect(page).toHaveURL(/\/settings\?youtube_auth=connected$/u);
    await expect(
      page
        .getByRole("region", { name: "YouTube sync" })
        .getByRole("button", { name: "Reconnect YouTube" }),
    ).toBeVisible();
  });
});

test.describe("Gmail authorization", () => {
  test.use({ serviceWorkers: "block" });

  test("can be completed from Settings", async ({ page, login }) => {
    await page.route("**/api/gmail-auth**", async (route) => {
      if (new URL(route.request().url()).pathname.endsWith("/callback")) {
        await route.fulfill({ status: 204 });
        return;
      }
      const authorizationUrl = new URL(
        "/api/gmail-auth/callback?state=fake-state&code=fake-code",
        page.url() || "http://127.0.0.1",
      ).href;
      await route.fulfill({
        contentType: "application/json",
        json:
          route.request().method() === "POST"
            ? {
                authorization_url: authorizationUrl,
                error: null,
                state: "authorizing",
              }
            : {
                authorization_url: null,
                error: null,
                state: "disconnected",
              },
      });
    });
    await login();
    await page
      .getByRole("navigation", { name: "Main navigation" })
      .getByRole("link", { name: /^Settings/u })
      .click();

    const gmail = page.getByRole("region", { name: "Gmail" });
    await gmail.getByRole("button", { name: "Connect Gmail" }).click();
    await gmail.getByRole("link", { name: "Continue with Google" }).click();
    await expect(page).toHaveURL(/\/api\/gmail-auth\/callback\?/u);
    await page.unroute("**/api/gmail-auth**");
    await page.route("**/api/gmail-auth", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        json: {
          authorization_url: null,
          error: null,
          state: "connected",
        },
      });
    });
    await page.goto("/settings?gmail_auth=connected");

    await expect(page).toHaveURL(/\/settings\?gmail_auth=connected$/u);
    await expect(
      page
        .getByRole("region", { name: "Gmail" })
        .getByRole("button", { name: "Reconnect Gmail" }),
    ).toBeVisible();
  });
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

test("the removed proposals route shows not found", async ({ page, login }) => {
  await login();
  await page.goto("/proposals", { waitUntil: "domcontentloaded" });

  await expect(
    page.getByRole("heading", { level: 1, name: "Page not found" }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: /^Proposals/u })).toBeHidden();
});

test("logs in and lands on pure chat, with the nav present", async ({
  page,
  login,
}) => {
  await login();

  // Chat is the home page and the whole page: transcript + composer, nothing
  // else (#250). The console guard (fixtures.ts) additionally asserts the
  // page booted — including the /ws upgrade — without any runtime error.
  // The semantic page title remains available without spending a visible row
  // on a heading that duplicates the active Chat navigation item.
  const chatTitle = page.locator("#chat-title");
  await expect(chatTitle).toBeAttached();
  expect(await chatTitle.boundingBox()).toMatchObject({ height: 1, width: 1 });
  await expect(
    page.locator('[role="log"][aria-label="Chat transcript"]'),
  ).toBeVisible();

  // The nav's four destinations are reachable from chat (desktop sidebar by
  // default in the Playwright viewport).
  const nav = page.getByRole("navigation", { name: "Main navigation" });
  await expect(nav).toBeVisible();
  for (const label of ["Chat", "Inbox", "Browse", "Settings"]) {
    await expect(
      nav.getByRole("link", { name: new RegExp(`^${label}`) }),
    ).toBeVisible();
  }
});
