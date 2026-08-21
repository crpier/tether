import { expect, test } from "./fixtures";

/**
 * Conversation mode (#542): the composer exposes an accessible toggle that
 * defaults to off (ordinary text chat) and flips its pressed state on click.
 * Playback itself needs real audio output, so the automated suite pins only
 * the control contract here; capture/playback behavior is covered by the
 * jsdom unit tests around `live-chat-turn` and `speech-player`.
 */
test("conversation mode toggle defaults to text and toggles cleanly", async ({
  page,
  login,
}) => {
  await login();

  const toggle = page.getByRole("button", { name: "Conversation mode" });
  await expect(toggle).toBeVisible();
  await expect(toggle).toHaveAttribute("aria-pressed", "false");

  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-pressed", "true");

  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-pressed", "false");
});
