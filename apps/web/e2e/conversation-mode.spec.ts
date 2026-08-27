import { expect, test } from "./fixtures";

/**
 * Voice conversation (#576): one composer control owns start and end. Provider
 * requests and playback stay deterministic in host and web unit tests.
 */
test("one voice control starts and ends conversation", async ({
  page,
  login,
}) => {
  await page.addInitScript(() => {
    class FakeMediaRecorder {
      ondataavailable: ((event: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;

      start(): void {
        // The browser smoke observes the active listening presentation.
      }

      stop(): void {
        this.ondataavailable?.({ data: new Blob(["voice"]) });
        this.onstop?.();
      }
    }
    Object.defineProperty(window, "AudioContext", {
      configurable: true,
      value: undefined,
    });
    Object.defineProperty(window, "MediaRecorder", {
      configurable: true,
      value: FakeMediaRecorder,
    });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: () => Promise.resolve(new MediaStream()),
      },
    });
  });
  await login();

  const start = page.getByRole("button", { name: "Start voice conversation" });
  await expect(start).toBeVisible();
  await expect(start).toHaveAttribute("aria-pressed", "false");

  await start.click();
  const end = page.getByRole("button", { name: "End voice conversation" });
  await expect(end).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText("Listening…")).toBeVisible();
  await expect(
    page.getByRole("img", { name: "Microphone is listening" }),
  ).toBeVisible();

  await end.click();
  await expect(start).toHaveAttribute("aria-pressed", "false");
  await expect(page.getByText("Listening…")).toHaveCount(0);
});
