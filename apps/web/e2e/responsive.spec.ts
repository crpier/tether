import type { Locator } from "@playwright/test";

import { expect, test } from "./fixtures";

// Layout is CSS, so the meaningful guard is real geometry in a real browser —
// jsdom can't compute it. These specs pin the behaviours issue #112 called out:
// a header that doesn't clip Log out, and a chat that goes full-width in a
// single stacked column on phones while keeping the two-column split on desktop.

async function boundingBox(
  locator: Locator,
): Promise<{ x: number; y: number; width: number; height: number }> {
  const box = await locator.boundingBox();
  expect(box, "expected element to have a bounding box").not.toBeNull();
  if (box === null) {
    throw new Error("unreachable: assertion above fails first");
  }
  return box;
}

const PHONE = { width: 390, height: 780 };
const DESKTOP = { width: 1280, height: 860 };

test("phone width: header keeps Log out reachable and chat is full-width", async ({
  page,
  login,
}) => {
  await page.setViewportSize(PHONE);
  await login();

  const transcript = page.locator('section[aria-label="Chat transcript"]');
  await transcript.waitFor({ state: "visible" });

  // Log out must stay within the viewport rather than clip off the right edge.
  const logout = await boundingBox(
    page.getByRole("button", { name: "Log out" }),
  );
  expect(logout.x).toBeGreaterThanOrEqual(0);
  expect(logout.x + logout.width).toBeLessThanOrEqual(PHONE.width + 1);

  // Chat is a full-width column, not the pre-fix sliver.
  const chat = await boundingBox(transcript);
  expect(chat.width).toBeGreaterThan(PHONE.width * 0.8);

  // Single-column stack: the sidebar sits below the chat, not beside it.
  const sidebar = await boundingBox(page.locator("aside"));
  expect(sidebar.y).toBeGreaterThanOrEqual(chat.y + chat.height - 1);
});

test("desktop width: chat and sidebar sit side by side", async ({
  page,
  login,
}) => {
  await page.setViewportSize(DESKTOP);
  await login();

  const transcript = page.locator('section[aria-label="Chat transcript"]');
  await transcript.waitFor({ state: "visible" });

  const chat = await boundingBox(transcript);
  const sidebar = await boundingBox(page.locator("aside"));

  // Two columns share a row and the sidebar is to the right of the transcript.
  expect(chat.x + chat.width).toBeLessThanOrEqual(sidebar.x + 1);
  expect(Math.abs(chat.y - sidebar.y)).toBeLessThan(8);
});
