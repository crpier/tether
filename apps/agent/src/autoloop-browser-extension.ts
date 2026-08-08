import type { Browser, BrowserContext, ConsoleMessage, Page } from "playwright";
import { chromium } from "playwright";
import { Type } from "typebox";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

interface BrowserState {
  browser: Browser | undefined;
  consoleMessages: string[];
  context: BrowserContext | undefined;
  networkMessages: string[];
  page: Page | undefined;
}

const maxLogEntries = 100;

function trimText(text: string, limit: number): string {
  return text.length <= limit ? text : `${text.slice(0, limit)}\n… truncated …`;
}

function pushBounded(target: string[], value: string): void {
  target.push(value);
  if (target.length > maxLogEntries)
    target.splice(0, target.length - maxLogEntries);
}

async function ensurePage(state: BrowserState): Promise<Page> {
  if (state.page !== undefined) return state.page;

  state.browser = await chromium.launch({
    headless: process.env.TETHER_AUTOLOOP_HEADED !== "1",
  });
  state.context = await state.browser.newContext({
    viewport: { height: 900, width: 1440 },
  });
  state.page = await state.context.newPage();

  state.page.on("console", (message: ConsoleMessage) => {
    pushBounded(state.consoleMessages, `${message.type()}: ${message.text()}`);
  });
  state.page.on("requestfailed", (request) => {
    pushBounded(
      state.networkMessages,
      `failed ${request.method()} ${request.url()}: ${request.failure()?.errorText ?? "unknown"}`,
    );
  });
  state.page.on("response", (response) => {
    if (response.status() >= 400) {
      pushBounded(
        state.networkMessages,
        `${String(response.status())} ${response.request().method()} ${response.url()}`,
      );
    }
  });

  return state.page;
}

async function fillField(
  page: Page,
  field: string,
  value: string,
): Promise<string> {
  const candidates = [
    page.getByLabel(field).first(),
    page.getByPlaceholder(field).first(),
    page.locator(`[name="${field.replaceAll('"', '\\"')}"]`).first(),
  ];
  let lastError: unknown;
  for (const locator of candidates) {
    try {
      await locator.fill(value, { timeout: 2_000 });
      return await snapshot(page);
    } catch (error: unknown) {
      lastError = error;
    }
  }
  throw lastError instanceof Error
    ? lastError
    : new Error(`could not fill ${field}`);
}

async function snapshot(page: Page): Promise<string> {
  const [title, url, bodyText, controls] = await Promise.all([
    page.title(),
    Promise.resolve(page.url()),
    page
      .locator("body")
      .innerText({ timeout: 2_000 })
      .catch(() => ""),
    page
      .locator("a,button,input,textarea,select,[role='button'],[role='link']")
      .evaluateAll((elements) =>
        elements.slice(0, 80).map((element, index) => {
          const htmlElement = element as HTMLElement;
          const input = element as HTMLInputElement;
          return {
            index,
            ariaLabel: htmlElement.getAttribute("aria-label"),
            name: input.name || undefined,
            placeholder: input.placeholder || undefined,
            role: htmlElement.getAttribute("role"),
            tag: htmlElement.tagName.toLowerCase(),
            text: (htmlElement.innerText || input.value || "")
              .trim()
              .slice(0, 120),
            type: input.type || undefined,
          };
        }),
      )
      .catch(() => []),
  ]);

  return JSON.stringify(
    {
      bodyText: trimText(bodyText, 5_000),
      controls,
      title,
      url,
    },
    null,
    2,
  );
}

export default function autoloopBrowserExtension(pi: ExtensionAPI): void {
  const state: BrowserState = {
    browser: undefined,
    consoleMessages: [],
    context: undefined,
    networkMessages: [],
    page: undefined,
  };

  pi.registerTool({
    name: "browser_open",
    label: "Browser Open",
    description: "Open a URL in the exploratory browser.",
    parameters: Type.Object({
      url: Type.String({ description: "URL to open" }),
    }),
    async execute(_toolCallId, params, signal) {
      const page = await ensurePage(state);
      await page.goto(params.url, {
        waitUntil: "domcontentloaded",
        timeout: 10_000,
      });
      if (signal?.aborted === true) throw new Error("aborted");
      return {
        content: [{ type: "text", text: await snapshot(page) }],
        details: {},
      };
    },
  });

  pi.registerTool({
    name: "browser_snapshot",
    label: "Browser Snapshot",
    description:
      "Return the current page URL, title, visible text, and key controls.",
    parameters: Type.Object({}),
    async execute() {
      const page = await ensurePage(state);
      return {
        content: [{ type: "text", text: await snapshot(page) }],
        details: {},
      };
    },
  });

  pi.registerTool({
    name: "browser_click",
    label: "Browser Click",
    description:
      "Click the first visible element matching text or accessible name.",
    parameters: Type.Object({
      text: Type.String({
        description: "Visible text or accessible name to click",
      }),
    }),
    async execute(_toolCallId, params) {
      const page = await ensurePage(state);
      const candidates = [
        page.getByRole("button", { name: params.text }).first(),
        page.getByRole("link", { name: params.text }).first(),
        page.getByLabel(params.text).first(),
        page.getByText(params.text, { exact: false }).first(),
      ];
      let lastError: unknown;
      for (const locator of candidates) {
        try {
          await locator.click({ timeout: 2_000 });
          await page
            .waitForLoadState("domcontentloaded", { timeout: 2_000 })
            .catch(() => undefined);
          return {
            content: [{ type: "text", text: await snapshot(page) }],
            details: {},
          };
        } catch (error: unknown) {
          lastError = error;
        }
      }
      throw lastError instanceof Error
        ? lastError
        : new Error(`could not click ${params.text}`);
    },
  });

  pi.registerTool({
    name: "browser_fill",
    label: "Browser Fill",
    description: "Fill an input matched by label, placeholder, name, or text.",
    parameters: Type.Object({
      field: Type.String({
        description: "Label, placeholder, name, or nearby text",
      }),
      value: Type.String({ description: "Value to enter" }),
    }),
    async execute(_toolCallId, params) {
      const page = await ensurePage(state);
      return {
        content: [
          {
            type: "text",
            text: await fillField(page, params.field, params.value),
          },
        ],
        details: {},
      };
    },
  });

  pi.registerTool({
    name: "browser_fill_secret",
    label: "Browser Fill Secret",
    description:
      "Fill an input from an environment variable without returning the secret.",
    parameters: Type.Object({
      envName: Type.String({
        description: "Environment variable containing the secret",
      }),
      field: Type.String({
        description: "Label, placeholder, name, or nearby text",
      }),
    }),
    async execute(_toolCallId, params) {
      const value = process.env[params.envName];
      if (value === undefined || value.length === 0) {
        throw new Error(`missing ${params.envName}`);
      }
      const page = await ensurePage(state);
      return {
        content: [
          { type: "text", text: await fillField(page, params.field, value) },
        ],
        details: { envName: params.envName },
      };
    },
  });

  pi.registerTool({
    name: "browser_console",
    label: "Browser Console",
    description: "Return recent browser console messages.",
    parameters: Type.Object({}),
    execute() {
      return Promise.resolve({
        content: [
          {
            type: "text",
            text: state.consoleMessages.join("\n") || "(no console messages)",
          },
        ],
        details: {},
      });
    },
  });

  pi.registerTool({
    name: "browser_network",
    label: "Browser Network",
    description: "Return recent failed requests and HTTP 4xx/5xx responses.",
    parameters: Type.Object({}),
    execute() {
      return Promise.resolve({
        content: [
          {
            type: "text",
            text: state.networkMessages.join("\n") || "(no network errors)",
          },
        ],
        details: {},
      });
    },
  });

  pi.registerTool({
    name: "browser_screenshot",
    label: "Browser Screenshot",
    description: "Save a screenshot and return its path.",
    parameters: Type.Object({
      path: Type.Optional(Type.String({ description: "Screenshot path" })),
    }),
    async execute(_toolCallId, params) {
      const page = await ensurePage(state);
      const path =
        params.path ??
        `.tether/autoloop/screenshots/${new Date().toISOString()}.png`;
      await page.screenshot({ fullPage: true, path });
      return { content: [{ type: "text", text: path }], details: { path } };
    },
  });

  pi.on("session_shutdown", async () => {
    await state.context?.close().catch(() => undefined);
    await state.browser?.close().catch(() => undefined);
    state.context = undefined;
    state.browser = undefined;
    state.page = undefined;
  });
}
