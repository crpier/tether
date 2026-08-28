import { expect, test } from "./fixtures";

const ONE_PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);

test("chat stages and removes an image attachment", async ({ page, login }) => {
  await login();

  await page.getByLabel("Attach files").setInputFiles({
    buffer: ONE_PIXEL_PNG,
    mimeType: "image/png",
    name: "pixel.png",
  });

  await expect(page.getByText("pixel.png")).toBeVisible();
  await expect(page.getByRole("img", { name: "pixel.png" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Send" })).toBeEnabled();

  await page.getByRole("button", { name: "Remove pixel.png" }).click();

  await expect(page.getByText("pixel.png")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Send" })).toBeDisabled();
});
