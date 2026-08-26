import { chromium, expect, test } from "@playwright/test";
import type { APIRequestContext, APIResponse } from "@playwright/test";
import { execFileSync } from "node:child_process";
import path from "node:path";

const webuiUrl = "http://127.0.0.1:18080";
const composeFile = path.resolve(
  process.cwd(),
  "../../spikes/open-webui-v0.11.1/compose.yaml",
);

async function readJson(response: APIResponse): Promise<unknown> {
  const body: unknown = await response.json();
  return body;
}

async function signIn(request: APIRequestContext): Promise<string> {
  const response = await request.post(`${webuiUrl}/api/v1/auths/signin`, {
    data: { email: "spike@example.com", password: "spike-password" },
  });
  const session = await readJson(response);
  if (
    !response.ok() ||
    typeof session !== "object" ||
    session === null ||
    !("token" in session) ||
    typeof session.token !== "string"
  ) {
    throw new Error("Open WebUI spike sign-in did not return a token");
  }
  return session.token;
}

async function fetchFirstChatId(
  request: APIRequestContext,
  token: string,
): Promise<string> {
  const response = await request.get(`${webuiUrl}/api/v1/chats/?page=1`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const chats = await readJson(response);
  const firstChat: unknown = Array.isArray(chats) ? chats[0] : undefined;
  if (
    typeof firstChat !== "object" ||
    firstChat === null ||
    !("id" in firstChat) ||
    typeof firstChat.id !== "string"
  ) {
    throw new Error("Open WebUI spike did not preserve a conversation");
  }
  return firstChat.id;
}

test.describe.configure({ mode: "serial" });
test.skip(
  process.env.TETHER_OPEN_WEBUI_SPIKE !== "1",
  "Run only through the throwaway Open WebUI spike harness.",
);

test("prototype tool server rejects unauthenticated discovery and invocation", async ({
  request,
}) => {
  const schemaResponse = await request.get(
    "http://127.0.0.1:18081/tools/openapi.json",
  );
  const invocationResponse = await request.post(
    "http://127.0.0.1:18081/tools/spike_echo",
    { data: { message: "must not run" } },
  );

  expect(schemaResponse.status()).toBe(401);
  expect(invocationResponse.status()).toBe(401);
});

test("prototype first account becomes admin while signup is disabled", async ({
  request,
}) => {
  const response = await request.post(`${webuiUrl}/api/v1/auths/signup`, {
    data: {
      email: "spike@example.com",
      name: "Spike Admin",
      password: "spike-password",
      profile_image_url: "",
    },
  });

  expect(response.status()).toBe(200);
  expect(await readJson(response)).toMatchObject({ role: "admin" });
});

test("prototype rejects a second account after first-admin creation", async ({
  request,
}) => {
  const response = await request.post(`${webuiUrl}/api/v1/auths/signup`, {
    data: {
      email: "second@example.com",
      name: "Second User",
      password: "second-password",
      profile_image_url: "",
    },
  });

  expect(response.status()).toBe(403);
});

test("prototype discovers the global tool server with backend bearer auth", async ({
  request,
}) => {
  const token = await signIn(request);

  const toolsResponse = await request.get(`${webuiUrl}/api/v1/tools/`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const eventsResponse = await request.get("http://127.0.0.1:18081/events");

  expect(toolsResponse.status()).toBe(200);
  expect(await readJson(toolsResponse)).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ id: "server:tether-spike" }),
    ]),
  );
  expect(await readJson(eventsResponse)).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ kind: "schema_fetch", authorized: true }),
    ]),
  );
});

test("prototype approval survives reload and continues the native tool turn", async ({
  page,
  request,
}) => {
  const browserErrors: string[] = [];
  const browserToolRequests: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("request", (pendingRequest) => {
    if (pendingRequest.url().startsWith("http://127.0.0.1:18081")) {
      browserToolRequests.push(pendingRequest.url());
    }
  });

  const token = await signIn(request);
  await request.delete("http://127.0.0.1:18081/events");

  const modelResponse = await request.post(`${webuiUrl}/api/v1/models/create`, {
    headers: { Authorization: `Bearer ${token}` },
    data: {
      id: "tether-spike",
      base_model_id: "spike-model",
      name: "Tether Spike",
      meta: { toolIds: ["server:tether-spike"] },
      params: { function_calling: "native" },
      access_grants: [],
      is_active: true,
    },
  });
  expect(modelResponse.status()).toBe(200);

  const settingsResponse = await request.post(
    `${webuiUrl}/api/v1/users/user/settings/update`,
    {
      headers: { Authorization: `Bearer ${token}` },
      data: {
        ui: {
          models: ["tether-spike"],
          params: { tool_approval_mode: "ask" },
        },
      },
    },
  );
  expect(settingsResponse.status()).toBe(200);

  await page.addInitScript((sessionToken) => {
    localStorage.setItem("token", sessionToken);
  }, token);
  await page.goto(webuiUrl);
  const releaseNotesButton = page.getByRole("button", { name: "Close" });
  await releaseNotesButton
    .waitFor({ state: "visible", timeout: 2_000 })
    .then(() => releaseNotesButton.click())
    .catch(() => undefined);
  await page.locator("#chat-input").fill("Use the spike echo tool.");
  await page.locator("#send-message-button").click();

  await expect(page.locator(".tool-call-allow-button")).toBeVisible({
    timeout: 10_000,
  });
  let events = await readJson(
    await request.get("http://127.0.0.1:18081/events"),
  );
  expect(events).not.toEqual(
    expect.arrayContaining([expect.objectContaining({ kind: "tool_call" })]),
  );

  await page.reload();
  await expect(page.locator(".tool-call-allow-button")).toBeVisible({
    timeout: 5_000,
  });
  await page.locator(".tool-call-allow-button").click();
  await expect(
    page.getByText("Tool result received: hello from tool", { exact: true }),
  ).toBeVisible({ timeout: 10_000 });

  events = await readJson(await request.get("http://127.0.0.1:18081/events"));
  expect(events).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        kind: "model_call",
        request: expect.objectContaining({
          tools: expect.arrayContaining([
            expect.objectContaining({
              function: expect.objectContaining({ name: "spike_echo" }),
            }),
          ]),
        }),
      }),
      expect.objectContaining({
        kind: "tool_call",
        authorized: true,
        arguments: { message: "hello from tool" },
      }),
      expect.objectContaining({
        kind: "model_call",
        request: expect.objectContaining({
          messages: expect.arrayContaining([
            expect.objectContaining({ role: "tool" }),
          ]),
        }),
      }),
    ]),
  );
  expect(browserToolRequests).toEqual([]);
  expect(browserErrors).toEqual([]);
});

test("prototype conversation survives an Open WebUI container restart", async ({
  page,
  request,
}) => {
  const token = await signIn(request);
  const chatId = await fetchFirstChatId(request, token);

  execFileSync("docker", [
    "compose",
    "-f",
    composeFile,
    "restart",
    "open-webui",
  ]);
  await expect
    .poll(
      async () => {
        const healthResponse = await request
          .get(`${webuiUrl}/health`)
          .catch(() => null);
        return healthResponse?.ok() ?? false;
      },
      { timeout: 15_000 },
    )
    .toBe(true);

  await page.addInitScript((sessionToken) => {
    localStorage.setItem("token", sessionToken);
  }, token);
  await page.goto(`${webuiUrl}/c/${chatId}`);

  await expect(
    page.getByText("Tool result received: hello from tool", { exact: true }),
  ).toBeVisible({ timeout: 5_000 });
});

test("prototype chat has no horizontal overflow at phone width", async ({
  page,
  request,
}) => {
  const token = await signIn(request);
  const chatId = await fetchFirstChatId(request, token);

  await page.setViewportSize({ width: 375, height: 812 });
  await page.addInitScript((sessionToken) => {
    localStorage.setItem("token", sessionToken);
  }, token);
  await page.goto(`${webuiUrl}/c/${chatId}`);

  await expect(
    page.getByText("Tool result received: hello from tool", { exact: true }),
  ).toBeVisible({ timeout: 5_000 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("prototype voice records and transcribes over localhost", async ({
  request,
}) => {
  const token = await signIn(request);
  await request.delete("http://127.0.0.1:18081/events");

  const browser = await chromium.launch({
    headless: true,
    args: [
      "--use-fake-device-for-media-stream",
      "--use-fake-ui-for-media-stream",
    ],
  });
  try {
    const context = await browser.newContext({
      permissions: ["microphone"],
    });
    await context.addInitScript((sessionToken) => {
      localStorage.setItem("token", sessionToken);
    }, token);
    const page = await context.newPage();
    await page.goto(webuiUrl);

    await page.getByRole("button", { name: "Voice Input" }).click();
    await expect(page.locator("#confirm-recording-button")).toBeVisible({
      timeout: 5_000,
    });
    await page.waitForTimeout(500);
    await page.locator("#confirm-recording-button").click();

    await expect(page.locator("#chat-input")).toContainText(
      "voice spike works",
      { timeout: 10_000 },
    );

    const events = await readJson(
      await request.get("http://127.0.0.1:18081/events"),
    );
    expect(events).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          kind: "transcription",
          authorized: true,
        }),
      ]),
    );
  } finally {
    await browser.close();
  }
});
