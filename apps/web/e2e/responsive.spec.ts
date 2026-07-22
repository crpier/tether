import type { Locator } from "@playwright/test";

import { expect, test } from "./fixtures";

// Layout is CSS, so the meaningful guard is real geometry in a real browser —
// jsdom can't compute it. The shell (#250) swaps a collapsible left sidebar
// for a bottom tab bar at the `lg` breakpoint; both render the same five nav
// destinations.

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

test("phone width: bottom tab bar, chat is full-width, sidebar hidden", async ({
  page,
  login,
}) => {
  await page.setViewportSize(PHONE);
  await login();

  const transcript = page.locator('section[aria-label="Chat transcript"]');
  await transcript.waitFor({ state: "visible" });

  // The desktop sidebar is not shown at this width…
  await expect(page.locator("aside")).toBeHidden();
  // …while the bottom tab bar is, reachable within the viewport.
  const bottomNav = page.getByRole("navigation", {
    name: "Main navigation (compact)",
  });
  const tabsBox = await boundingBox(bottomNav);
  expect(tabsBox.x).toBeGreaterThanOrEqual(0);
  expect(tabsBox.x + tabsBox.width).toBeLessThanOrEqual(PHONE.width + 1);
  await expect(
    bottomNav.getByRole("link", { name: /^Proposals/ }),
  ).toBeVisible();

  // Chat is a full-width column.
  const chat = await boundingBox(transcript);
  expect(chat.width).toBeGreaterThan(PHONE.width * 0.8);
});

test("desktop width: left sidebar visible, bottom tabs hidden", async ({
  page,
  login,
}) => {
  await page.setViewportSize(DESKTOP);
  await login();

  const transcript = page.locator('section[aria-label="Chat transcript"]');
  await transcript.waitFor({ state: "visible" });

  const sidebar = page.getByRole("navigation", { name: "Main navigation" });
  await expect(sidebar).toBeVisible();
  await expect(
    page.getByRole("navigation", { name: "Main navigation (compact)" }),
  ).toBeHidden();

  const chat = await boundingBox(transcript);
  const sidebarBox = await boundingBox(sidebar);

  // The sidebar sits to the left of chat, sharing a row.
  expect(sidebarBox.x + sidebarBox.width).toBeLessThanOrEqual(chat.x + 1);
  expect(Math.abs(chat.y - sidebarBox.y)).toBeLessThan(40);
});

test("the sidebar collapses to icons and expands back", async ({
  page,
  login,
}) => {
  await page.setViewportSize(DESKTOP);
  await login();

  const sidebar = page.getByRole("navigation", { name: "Main navigation" });
  await sidebar.waitFor({ state: "visible" });
  const expandedBox = await boundingBox(sidebar.locator(".."));

  await page.getByRole("button", { name: "Collapse sidebar" }).click();
  const collapsed = page.getByRole("button", { name: "Expand sidebar" });
  await expect(collapsed).toBeVisible();
  // The width change animates (`transition-[width] duration-150`); wait past
  // it so the bounding box reflects the settled, collapsed width.
  await expect
    .poll(async () => (await boundingBox(sidebar.locator(".."))).width)
    .toBeLessThan(expandedBox.width);

  await collapsed.click();
  await expect(
    page.getByRole("button", { name: "Collapse sidebar" }),
  ).toBeVisible();
});
