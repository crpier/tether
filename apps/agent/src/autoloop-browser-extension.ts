import { mkdir } from "node:fs/promises";
import { dirname } from "node:path";

import type { Browser, BrowserContext, ConsoleMessage, Page } from "playwright";
import { chromium } from "playwright";
import { Type } from "typebox";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

interface BrowserState {
  actionCount: number;
  browser: Browser | undefined;
  consoleMessages: string[];
  context: BrowserContext | undefined;
  networkMessages: string[];
  page: Page | undefined;
}

const maxLogEntries = 100;

export function browserControlAccessibleName(control: {
  ariaLabel: string;
  innerText: string;
  labelledByText: string;
  labelText: string;
  placeholder: string;
}): string {
  return (
    control.ariaLabel ||
    control.labelledByText ||
    control.labelText ||
    control.placeholder ||
    control.innerText
  )
    .trim()
    .slice(0, 120);
}

export function browserControlText(control: {
  innerText: string;
  type: string;
  value: string;
}): string {
  if (control.type === "password" && control.value.length > 0) {
    return "[redacted]";
  }
  return (control.innerText || control.value).trim().slice(0, 120);
}

export function isAllowedBrowserUrl(
  candidate: string,
  target: string,
): boolean {
  try {
    return new URL(candidate).origin === new URL(target).origin;
  } catch {
    return false;
  }
}

function trimText(text: string, limit: number): string {
  return text.length <= limit ? text : `${text.slice(0, limit)}\n… truncated …`;
}

function consumeBrowserAction(state: BrowserState): void {
  const limit = Number.parseInt(
    process.env.TETHER_AUTOLOOP_MAX_ACTIONS ?? "12",
    10,
  );
  if (!Number.isFinite(limit) || limit < 1) {
    throw new Error("TETHER_AUTOLOOP_MAX_ACTIONS must be a positive integer");
  }
  if (state.actionCount >= limit) {
    throw new Error(`browser action limit of ${String(limit)} reached`);
  }
  state.actionCount += 1;
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
  const productionUrl = process.env.TETHER_AUTOLOOP_PRODUCTION_URL;
  if (productionUrl === undefined) {
    throw new Error("TETHER_AUTOLOOP_PRODUCTION_URL is required");
  }
  await state.context.route("**/*", async (route) => {
    if (isAllowedBrowserUrl(route.request().url(), productionUrl)) {
      await route.continue();
    } else {
      await route.abort("blockedbyclient");
    }
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
): Promise<void> {
  const candidates = [
    page.getByLabel(field).first(),
    page.getByPlaceholder(field).first(),
    page.locator(`[name="${field.replaceAll('"', '\\"')}"]`).first(),
  ];
  let lastError: unknown;
  for (const locator of candidates) {
    try {
      await locator.fill(value, { timeout: 2_000 });
      return;
    } catch (error: unknown) {
      lastError = error;
    }
  }
  throw lastError instanceof Error
    ? lastError
    : new Error(`could not fill ${field}`);
}

async function snapshot(page: Page): Promise<string> {
  const [title, url, bodyText, rawControls] = await Promise.all([
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
          const labelledByText = (
            htmlElement.getAttribute("aria-labelledby") ?? ""
          )
            .split(/\s+/)
            .filter(Boolean)
            .map((id) => document.getElementById(id)?.textContent ?? "")
            .join(" ");
          const labels =
            element instanceof HTMLInputElement ||
            element instanceof HTMLTextAreaElement ||
            element instanceof HTMLSelectElement ||
            element instanceof HTMLButtonElement
              ? element.labels
              : null;
          const labelElements = labels === null ? [] : Array.from(labels);
          const labelText = labelElements
            .map((label) => label.textContent)
            .join(" ");
          return {
            index,
            ariaLabel: htmlElement.getAttribute("aria-label") ?? "",
            innerText: htmlElement.innerText || "",
            labelledByText,
            labelText,
            name: input.name || undefined,
            placeholder: input.placeholder || "",
            role: htmlElement.getAttribute("role"),
            tag: htmlElement.tagName.toLowerCase(),
            type: input.type || "",
            value: input.value || "",
          };
        }),
      )
      .catch(() => []),
  ]);

  const controls = rawControls.map((control) => ({
    accessibleName: browserControlAccessibleName(control),
    index: control.index,
    name: control.name,
    role: control.role,
    tag: control.tag,
    text: browserControlText(control),
    type: control.type,
  }));

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
    actionCount: 0,
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
      consumeBrowserAction(state);
      const productionUrl = process.env.TETHER_AUTOLOOP_PRODUCTION_URL ?? "";
      if (!isAllowedBrowserUrl(params.url, productionUrl)) {
        throw new Error(
          "browser_open only permits the configured production origin",
        );
      }
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
      consumeBrowserAction(state);
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
      consumeBrowserAction(state);
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
          await page.waitForTimeout(200);
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
      consumeBrowserAction(state);
      const page = await ensurePage(state);
      await fillField(page, params.field, params.value);
      await page.waitForTimeout(300);
      return {
        content: [{ type: "text", text: await snapshot(page) }],
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
      field: Type.String({
        description: "Label, placeholder, name, or nearby text",
      }),
    }),
    async execute(_toolCallId, params) {
      consumeBrowserAction(state);
      const value = process.env.TETHER_AUTOLOOP_APP_PASSWORD;
      if (value === undefined || value.length === 0) {
        throw new Error("missing TETHER_AUTOLOOP_APP_PASSWORD");
      }
      const page = await ensurePage(state);
      await fillField(page, params.field, value);
      return {
        content: [{ type: "text", text: "Secret filled." }],
        details: {},
      };
    },
  });

  pi.registerTool({
    name: "browser_console",
    label: "Browser Console",
    description: "Return recent browser console messages.",
    parameters: Type.Object({}),
    execute() {
      consumeBrowserAction(state);
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
      consumeBrowserAction(state);
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
    parameters: Type.Object({}),
    async execute() {
      consumeBrowserAction(state);
      const page = await ensurePage(state);
      const path = `.tether/autoloop/screenshots/${new Date().toISOString()}.png`;
      await mkdir(dirname(path), { recursive: true });
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
