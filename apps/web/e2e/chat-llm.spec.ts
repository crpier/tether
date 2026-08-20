import { expect, test } from "./fixtures";

/**
 * Live chat happy path: type a message, send it, and wait for an assistant
 * reply to stream back.
 *
 * This is gated by `TETHER_E2E_LLM=1` and skipped by default. It needs the host
 * to be able to spawn the `apps/agent` `pi` runtime (so `pnpm -C apps/agent
 * install` must have run) and to have a default model + provider credentials in
 * its environment (e.g. `TETHER_DEFAULT_MODEL_PROVIDER`, `TETHER_DEFAULT_MODEL_ID`,
 * and the provider API key). Because it calls a real model it is non-deterministic
 * and spends tokens, so it is kept out of the deterministic gate run.
 *
 *   TETHER_E2E_LLM=1 just validate-web-smoke
 */
const LLM_ENABLED = process.env.TETHER_E2E_LLM === "1";

test.describe("chat (live LLM)", () => {
  test.skip(
    !LLM_ENABLED,
    "set TETHER_E2E_LLM=1 with a configured default model + provider key and an installed apps/agent runtime",
  );

  test("a scheduled prompt enters chat and receives an assistant reply", async ({
    page,
    login,
  }) => {
    await login();

    const prompt = `Scheduled e2e ${String(Date.now())}: reply with pong.`;
    const assistantMessages = page.getByLabel("Tether message");
    const assistantCount = await assistantMessages.count();
    const response = await page.request.post("/api/triggers", {
      data: {
        action_kind: "prompt",
        fire_at: new Date(Date.now() + 1_000).toISOString(),
        payload: prompt,
        recurrence: "once",
      },
    });
    expect(response.status()).toBe(201);

    await expect(
      page.getByLabel("You message").filter({ hasText: prompt }),
    ).toBeVisible({ timeout: 60_000 });
    await expect(assistantMessages).toHaveCount(assistantCount + 1, {
      timeout: 60_000,
    });
  });

  test("sends a message and receives an assistant reply", async ({
    page,
    login,
  }) => {
    await login();

    const composer = page.getByRole("textbox", { name: "Message" });
    await composer.fill("Reply with the single word: pong.");
    await page.getByRole("button", { name: "Send", exact: true }).click();

    // The user's message echoes into the transcript immediately.
    await expect(page.locator('[aria-label="Chat transcript"]')).toContainText(
      "pong",
    );

    // An assistant bubble must appear once the model responds. Allow a generous
    // timeout for model latency.
    await expect(page.getByLabel("Tether message").first()).toBeVisible({
      timeout: 60_000,
    });
  });
});
