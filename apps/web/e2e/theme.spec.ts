import { expect, test } from "./fixtures";

test("uses dark mode across the document and core surfaces", async ({
  page,
}) => {
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await expect(page.locator("html")).toHaveAttribute("data-kb-theme", "dark");
  await expect
    .poll(() =>
      page.evaluate(() => {
        const styles = getComputedStyle(document.documentElement);
        return {
          background: styles.getPropertyValue("--background").trim(),
          card: styles.getPropertyValue("--card").trim(),
          colorScheme: styles.colorScheme,
        };
      }),
    )
    .toEqual({
      background: "oklch(0.145 0 0)",
      card: "oklch(0.205 0 0)",
      colorScheme: "dark",
    });
});
