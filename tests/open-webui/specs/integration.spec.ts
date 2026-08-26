import { expect, test } from "@playwright/test";
import type {
  APIRequestContext,
  APIResponse,
  Page,
  Request,
  Response,
} from "@playwright/test";
import { execFileSync } from "node:child_process";

const TODO_ACTION = "Standalone Open WebUI smoke Todo";
const admin = {
  email: "smoke-admin@example.com",
  name: "Smoke Admin",
  password: "smoke-admin-password",
  profile_image_url: "",
};

function requiredEnvironment(name: string): string {
  const value = process.env[name];
  if (value === undefined || value === "") {
    throw new Error(
      `${name} is required; run scripts/validate-open-webui-smoke.sh`,
    );
  }
  return value;
}

const webuiUrl = requiredEnvironment("TETHER_SMOKE_WEBUI_URL");
const hostUrl = requiredEnvironment("TETHER_SMOKE_HOST_URL");
const providerUrl = requiredEnvironment("TETHER_SMOKE_PROVIDER_URL");
const toolToken = requiredEnvironment("TETHER_SMOKE_TOOL_TOKEN");
const captureToken = requiredEnvironment("TETHER_SMOKE_CAPTURE_TOKEN");
const providerToken = requiredEnvironment("TETHER_SMOKE_PROVIDER_TOKEN");
const composeProject = requiredEnvironment("TETHER_SMOKE_COMPOSE_PROJECT");
const composeFile = requiredEnvironment("TETHER_SMOKE_COMPOSE_FILE");

interface JsonObject {
  [key: string]: unknown;
}

async function readJson(response: APIResponse): Promise<unknown> {
  const body: unknown = await response.json();
  return body;
}

function objectValue(value: unknown, message: string): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(message);
  }
  return value as JsonObject;
}

async function signIn(request: APIRequestContext): Promise<string> {
  const response = await request.post(`${webuiUrl}/api/v1/auths/signin`, {
    data: { email: admin.email, password: admin.password },
  });
  const session = objectValue(
    await readJson(response),
    "Open WebUI sign-in returned a non-object response",
  );
  expect(response.status()).toBe(200);
  expect(typeof session.token).toBe("string");
  return session.token as string;
}

async function hostTodos(request: APIRequestContext): Promise<JsonObject> {
  const response = await request.post(`${hostUrl}/tools/list_todos`, {
    headers: { Authorization: `Bearer ${toolToken}` },
    data: {},
  });
  expect(response.status()).toBe(200);
  const envelope = objectValue(
    await readJson(response),
    "Host returned a non-object envelope",
  );
  expect(envelope.success).toBe(true);
  return objectValue(envelope.result, "Host returned a non-object Todo result");
}

async function providerEvents(request: APIRequestContext): Promise<unknown[]> {
  const response = await request.get(`${providerUrl}/events`, {
    headers: { Authorization: `Bearer ${providerToken}` },
  });
  expect(response.status()).toBe(200);
  const events = await readJson(response);
  if (!Array.isArray(events))
    throw new Error("Fake provider returned a non-array journal");
  return events as unknown[];
}

async function firstChatId(
  request: APIRequestContext,
  token: string,
): Promise<string> {
  const response = await request.get(`${webuiUrl}/api/v1/chats/?page=1`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(response.status()).toBe(200);
  const chats = await readJson(response);
  const firstChat = objectValue(
    Array.isArray(chats) ? chats[0] : undefined,
    "Open WebUI did not persist a chat",
  );
  if (typeof firstChat.id !== "string")
    throw new Error("Persisted chat has no string id");
  return firstChat.id;
}

interface BrowserGuard {
  assertClean(): Promise<void>;
}

function guardBrowser(page: Page): BrowserGuard {
  const consoleErrors: string[] = [];
  const failedRequests: string[] = [];
  const serverErrors: string[] = [];
  const directHostRequests: string[] = [];
  const credentialLeaks: string[] = [];
  const inspections: Promise<void>[] = [];

  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(error.message));
  page.on("requestfailed", (request: Request) => {
    const failure = request.failure();
    failedRequests.push(
      `${request.method()} ${request.url()}: ${failure === null ? "unknown failure" : failure.errorText}`,
    );
  });
  page.on("request", (request: Request) => {
    if (request.url().startsWith(hostUrl))
      directHostRequests.push(request.url());
    inspections.push(
      request
        .allHeaders()
        .then((headers) => {
          const browserPayload = JSON.stringify({
            headers,
            postData: request.postData(),
          });
          if (browserPayload.includes(toolToken))
            credentialLeaks.push(request.url());
        })
        .catch(() => undefined),
    );
  });
  page.on("response", (response: Response) => {
    if (response.status() >= 500) {
      serverErrors.push(`${String(response.status())} ${response.url()}`);
    }
    const resourceType = response.request().resourceType();
    if (["document", "fetch", "xhr"].includes(resourceType)) {
      inspections.push(
        Promise.all([response.allHeaders(), response.text()])
          .then(([headers, body]) => {
            if (
              JSON.stringify(headers).includes(toolToken) ||
              body.includes(toolToken)
            ) {
              credentialLeaks.push(response.url());
            }
          })
          .catch(() => undefined),
      );
    }
  });

  return {
    async assertClean(): Promise<void> {
      await Promise.all(inspections);
      expect(consoleErrors, "browser console/page errors").toEqual([]);
      expect(failedRequests, "unexpected browser request failures").toEqual([]);
      expect(serverErrors, "browser-visible 5xx responses").toEqual([]);
      expect(
        directHostRequests,
        "browser contacted the Tether tool host directly",
      ).toEqual([]);
      expect(
        credentialLeaks,
        "browser sent or received the Open WebUI tool credential",
      ).toEqual([]);
    },
  };
}

async function closeReleaseNotes(page: Page): Promise<void> {
  const button = page.getByRole("button", { name: "Close" });
  await button
    .waitFor({ state: "visible", timeout: 2_000 })
    .then(() => button.click())
    .catch(() => undefined);
}

function composeServiceUrl(service: string, containerPort: number): string {
  const address = execFileSync(
    "docker",
    [
      "compose",
      "-p",
      composeProject,
      "-f",
      composeFile,
      "port",
      service,
      String(containerPort),
    ],
    { encoding: "utf8" },
  ).trim();
  return `http://${address}`;
}

test.describe.configure({ mode: "serial" });

test("real host rejects absent and non-Open-WebUI bearer credentials", async ({
  request,
}) => {
  for (const headers of [
    undefined,
    { Authorization: `Bearer ${captureToken}` },
  ]) {
    const schema = await request.get(`${hostUrl}/tools/openapi.json`, {
      headers,
    });
    const invocation = await request.post(`${hostUrl}/tools/list_todos`, {
      headers,
      data: {},
    });
    expect(schema.status()).toBe(401);
    expect(invocation.status()).toBe(401);
  }

  const schema = await request.get(`${hostUrl}/tools/openapi.json`, {
    headers: { Authorization: `Bearer ${toolToken}` },
  });
  expect(schema.status()).toBe(200);
  const document = objectValue(
    await readJson(schema),
    "Host returned a non-object schema",
  );
  const paths = objectValue(document.paths, "Host schema has no paths object");
  expect(paths).toHaveProperty("/tools/create_todo");
  expect(paths).toHaveProperty("/tools/list_todos");
});

test("first account becomes admin while signup remains disabled", async ({
  request,
}) => {
  const first = await request.post(`${webuiUrl}/api/v1/auths/signup`, {
    data: admin,
  });
  expect(first.status()).toBe(200);
  expect(await readJson(first)).toMatchObject({ role: "admin" });

  const second = await request.post(`${webuiUrl}/api/v1/auths/signup`, {
    data: {
      email: "second-smoke@example.com",
      name: "Second Smoke User",
      password: "second-smoke-password",
      profile_image_url: "",
    },
  });
  expect(second.status()).toBe(403);
});

test("backend discovers the global real-host tool connection", async ({
  request,
}) => {
  const token = await signIn(request);
  const toolsResponse = await request.get(`${webuiUrl}/api/v1/tools/`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(toolsResponse.status()).toBe(200);
  expect(await readJson(toolsResponse)).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ id: "server:tether-smoke" }),
    ]),
  );
});

test("ask approval gates create, continues, lists, and persists through refresh", async ({
  page,
  request,
}) => {
  const browser = guardBrowser(page);
  const token = await signIn(request);

  const modelResponse = await request.post(`${webuiUrl}/api/v1/models/create`, {
    headers: { Authorization: `Bearer ${token}` },
    data: {
      id: "tether-smoke",
      base_model_id: "smoke-model",
      name: "Tether Smoke",
      meta: { toolIds: ["server:tether-smoke"] },
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
          models: ["tether-smoke"],
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
  await closeReleaseNotes(page);
  await page.locator("#chat-input").fill("Create the standalone smoke Todo.");
  await page.locator("#send-message-button").click();

  const allow = page.locator(".tool-call-allow-button");
  await expect(allow).toBeVisible({ timeout: 10_000 });
  expect(await hostTodos(request)).toMatchObject({ ready: [], waiting: [] });

  await allow.click();
  await expect(
    page.getByText(`Todo created: ${TODO_ACTION}`, { exact: true }),
  ).toBeVisible({
    timeout: 10_000,
  });
  expect(await hostTodos(request)).toMatchObject({
    ready: [expect.objectContaining({ action: TODO_ACTION })],
    waiting: [],
  });

  const chatId = await firstChatId(request, token);
  await page.reload();
  await expect(
    page.getByText(`Todo created: ${TODO_ACTION}`, { exact: true }),
  ).toBeVisible();

  await page.locator("#chat-input").fill("List my Todos.");
  await page.locator("#send-message-button").click();
  await expect(allow).toBeVisible({ timeout: 10_000 });
  await allow.click();
  await expect(
    page.getByText(`Todo list confirmed: ${TODO_ACTION}`, { exact: true }),
  ).toBeVisible({
    timeout: 10_000,
  });

  const events = await providerEvents(request);
  expect(events).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        kind: "model_call",
        request: expect.objectContaining({
          tools: expect.arrayContaining([
            expect.objectContaining({
              function: expect.objectContaining({ name: "create_todo" }),
            }),
            expect.objectContaining({
              function: expect.objectContaining({ name: "list_todos" }),
            }),
          ]),
        }),
      }),
      expect.objectContaining({
        kind: "model_call",
        request: expect.objectContaining({
          messages: expect.arrayContaining([
            expect.objectContaining({
              role: "tool",
              content: expect.stringContaining(TODO_ACTION),
            }),
          ]),
        }),
      }),
    ]),
  );
  expect(page.url()).toContain(`/c/${chatId}`);
  await browser.assertClean();
});

test("conversation survives an Open WebUI container restart", async ({
  page,
  request,
}) => {
  test.setTimeout(45_000);
  const browser = guardBrowser(page);
  const token = await signIn(request);
  const chatId = await firstChatId(request, token);

  execFileSync("docker", [
    "compose",
    "-p",
    composeProject,
    "-f",
    composeFile,
    "restart",
    "open-webui",
  ]);
  const restartedWebuiUrl = composeServiceUrl("open-webui", 8080);
  await expect
    .poll(
      async () => {
        const response = await request
          .get(`${restartedWebuiUrl}/health`, { timeout: 1_000 })
          .catch(() => null);
        return response?.ok() ?? false;
      },
      { timeout: 30_000 },
    )
    .toBe(true);

  await page.addInitScript((sessionToken) => {
    localStorage.setItem("token", sessionToken);
  }, token);
  await page.goto(`${restartedWebuiUrl}/c/${chatId}`);
  await expect(
    page.getByText(`Todo created: ${TODO_ACTION}`, { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(`Todo list confirmed: ${TODO_ACTION}`, { exact: true }),
  ).toBeVisible();
  expect(await hostTodos(request)).toMatchObject({
    ready: [expect.objectContaining({ action: TODO_ACTION })],
  });
  await browser.assertClean();
});
