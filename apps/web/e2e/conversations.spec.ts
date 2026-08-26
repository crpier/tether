import { expect, test } from "./fixtures";

test("root redirects to canonical Chat and preserves its query", async ({
  login,
  page,
}) => {
  await login();
  await page.goto("/?prompt=keep%20this");

  await expect(page).toHaveURL(/\/chat\?prompt=keep(?:%20|\+)this$/);
  await expect(page.getByRole("textbox", { name: "Message" })).toHaveValue(
    "keep this",
  );
});

test("new Conversation link immediately opens an untitled chat", async ({
  login,
  page,
}) => {
  await login();
  await page.getByRole("link", { name: "Create Conversation" }).click();

  await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/);
  await expect(
    page.getByRole("heading", { name: "Untitled chat" }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "Main Chat" })).toHaveCount(0);
});

test("creates, selects, archives, and restores a Scoped Conversation", async ({
  login,
  page,
}) => {
  await login();
  const name = `Playwright scope ${Date.now().toString()}`;

  await page.getByRole("link", { name: "Create Conversation" }).click();
  await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/);

  await page.getByRole("button", { name: "Edit conversation" }).click();
  await page.getByLabel("Conversation name").fill(name);
  await page
    .getByLabel("Scope brief")
    .fill("Exercise the Conversation lifecycle.");
  await page.getByRole("button", { name: "Save conversation" }).click();
  await expect(page.getByRole("heading", { name })).toBeVisible();
  await page.getByRole("button", { name: "Archive conversation" }).click();
  await expect(page).toHaveURL(/\/chat$/);

  await page.getByRole("link", { name: "Archived Conversations" }).click();
  const archived = page.getByRole("region", { name: "Archived Conversations" });
  await expect(archived.getByRole("link", { name })).toBeVisible();
  await archived.getByRole("button", { name: `Restore ${name}` }).click();
  await expect(archived.getByRole("link", { name })).toHaveCount(0);
});
