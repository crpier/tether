import type { Locator, Page } from "@playwright/test";

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
const TABLET_BELOW_DESKTOP = { width: 1023, height: 844 };
const DESKTOP = { width: 1280, height: 860 };

const LONG_CONVERSATION_ID = "018f0000-0000-7000-8000-000000000307";

interface ChatFixtureMessage {
  content: string;
  conversation_id: string;
  created_at: string;
  id: string;
  pi_message_id: null;
  role: "assistant" | "scheduled" | "tool" | "user";
  seq: number;
  tool_args: unknown;
  tool_name: string | null;
  tool_result: unknown;
  turn?: Record<string, unknown>;
}

function longMessage(
  seq: number,
  role: "assistant" | "user",
): ChatFixtureMessage {
  return {
    content: `Long mobile transcript message ${seq.toString()}: ${"Tether should keep the composer reachable while history scrolls independently. ".repeat(8)}`,
    conversation_id: LONG_CONVERSATION_ID,
    created_at: "2026-08-08T09:18:36Z",
    id: `018f0000-0000-7000-8000-${seq.toString().padStart(12, "0")}`,
    pi_message_id: null,
    role,
    seq,
    tool_args: null,
    tool_name: null,
    tool_result: null,
  };
}

function decidedProposal(index: number) {
  const padded = index.toString().padStart(2, "0");
  return {
    actions: [
      {
        display: `Archive batch ${padded}`,
        disposition: "pending",
        id: `018f0000-0000-7000-8001-${index.toString().padStart(12, "0")}`,
        kind: "archive_email",
        params: { batch: padded },
        scope: `mailbox-${padded}`,
      },
    ],
    consumer: "gmail-purge",
    created_at: `2026-01-${padded}T00:00:00Z`,
    decided_at: `2026-01-${padded}T00:00:00Z`,
    id: `018f0000-0000-7000-8002-${index.toString().padStart(12, "0")}`,
    producing_run_id: null,
    rejection_reason: null,
    state: index % 2 === 0 ? "approved" : "rejected",
    summary: `Historical proposal ${padded}`,
    title: `Decision ${padded}`,
    updated_at: `2026-01-${padded}T00:00:00Z`,
    version: 1,
  };
}

function grantSuggestion(index: number) {
  const padded = index.toString().padStart(2, "0");
  return {
    approved: index,
    edited: 0,
    kind: `operation-${padded}`,
    last_rejection: null,
    rejected: 0,
    scope: `scope-${padded}`,
    seen: index + 1,
  };
}

async function serveLargeProposalSurfaces(page: Page) {
  const decided = Array.from({ length: 40 }, (_, index) =>
    decidedProposal(index + 1),
  );
  const suggestions = Array.from({ length: 35 }, (_, index) =>
    grantSuggestion(index + 1),
  );
  await page.route("**/api/proposals**", (route) => {
    const url = new URL(route.request().url());
    return route.fulfill({
      contentType: "application/json",
      json: url.searchParams.get("state") === "pending" ? [] : decided,
    });
  });
  await page.route("**/api/grants", (route) =>
    route.fulfill({ contentType: "application/json", json: [] }),
  );
  await page.route("**/api/grants/suggestions", (route) =>
    route.fulfill({ contentType: "application/json", json: suggestions }),
  );
}

async function serveLongChat(
  page: Page,
  messages = Array.from({ length: 30 }, (_, index) =>
    longMessage(index + 1, index % 2 === 0 ? "user" : "assistant"),
  ),
) {
  await page.route("**/api/conversations", (route) =>
    route.fulfill({
      contentType: "application/json",
      json: [
        {
          archived_at: null,
          created_at: "2026-08-08T09:18:36Z",
          display_name: null,
          has_unread: false,
          id: LONG_CONVERSATION_ID,
          kind: "main",
          last_read_seq: 30,
          latest_activity: "2026-08-08T09:18:36Z",
          latest_message_seq: 30,
          pending_turn_count: 0,
          pi_session_id: LONG_CONVERSATION_ID,
          running_turn_id: null,
          scope_brief: null,
          scope_revision: 1,
          selected_model: "gpt-5.6-luna",
          session_gap_seconds: 300,
          status: "active",
          title: null,
        },
      ],
    }),
  );
  await page.route("**/api/conversations/*/messages**", (route) =>
    route.fulfill({ contentType: "application/json", json: messages }),
  );
  await page.route("**/api/models", (route) =>
    route.fulfill({
      contentType: "application/json",
      json: {
        default_model: "gpt-5.6-luna",
        models: [
          {
            display_name: "GPT-5.6 Luna · no thinking",
            id: "gpt-5.6-luna",
            model_id: "gpt-5.6-luna",
            provider: "openai-codex",
            thinking_level: "off",
          },
          {
            display_name: "GPT-5.6 Luna · low thinking",
            id: "gpt-5.6-luna-low",
            model_id: "gpt-5.6-luna",
            provider: "openai-codex",
            thinking_level: "low",
          },
          {
            display_name: "GPT-5.6 Terra · low thinking",
            id: "gpt-5.6-terra-low",
            model_id: "gpt-5.6-terra",
            provider: "openai-codex",
            thinking_level: "low",
          },
          {
            display_name: "GPT-5.6 Terra · medium thinking",
            id: "gpt-5.6-terra",
            model_id: "gpt-5.6-terra",
            provider: "openai-codex",
            thinking_level: "medium",
          },
          {
            display_name: "GPT-5.6 Sol · medium thinking",
            id: "gpt-5.6-sol",
            model_id: "gpt-5.6-sol",
            provider: "openai-codex",
            thinking_level: "medium",
          },
        ],
      },
    }),
  );
}

test("phone width: bottom tab bar, chat is full-width, sidebar hidden", async ({
  page,
  login,
}) => {
  await page.setViewportSize(PHONE);
  await login();

  const transcript = page.locator('[role="log"][aria-label="Chat transcript"]');
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

  // Chat is a full-width column. Model controls sit after the transcript, by
  // the composer, rather than above a long mobile conversation.
  const chat = await boundingBox(transcript);
  expect(chat.width).toBeGreaterThan(PHONE.width * 0.8);
  const modelSelector = await boundingBox(
    page.getByRole("group", { name: "Model" }),
  );
  expect(modelSelector.width).toBeLessThanOrEqual(256);
  expect(modelSelector.y).toBeGreaterThan(chat.y);
  expect(modelSelector.y).toBeLessThan(PHONE.height);
});

test("phone chat contains wide code and tables without page overflow", async ({
  page,
  login,
}) => {
  await page.setViewportSize(PHONE);
  await serveLongChat(page, [
    {
      ...longMessage(1, "assistant"),
      content: `\`\`\`text\n${"wide-code-".repeat(40)}\n\`\`\`\n\n| Value |\n| --- |\n| ${"wide-table-".repeat(40)} |`,
    },
  ]);
  await login();

  await expect(page.locator("pre")).toBeVisible();
  await expect(page.locator("table")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    ),
  ).toBeLessThanOrEqual(1);
});

test("phone Conversation picker is modal and Escape restores its trigger", async ({
  page,
  login,
}) => {
  await page.setViewportSize(PHONE);
  await login();

  const trigger = page.getByRole("button", { name: "Choose conversation" });
  await trigger.focus();
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: "Choose conversation" });
  await expect(dialog).toHaveAttribute("aria-modal", "true");

  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(trigger).toBeFocused();
});

for (const viewport of [PHONE, DESKTOP]) {
  test(`chat composer is one compact row at ${viewport.width.toString()}px`, async ({
    page,
    login,
  }) => {
    await page.setViewportSize(viewport);
    await serveLongChat(page);
    await login();

    const context = await boundingBox(
      page.getByRole("group", { name: "Composer context" }),
    );
    const modelSlider = await boundingBox(
      page.getByRole("slider", { name: "Model profile" }),
    );
    const sessionStatus = await boundingBox(
      page.getByText("Next message starts a fresh working session"),
    );
    expect(
      Math.abs(
        modelSlider.y +
          modelSlider.height / 2 -
          (sessionStatus.y + sessionStatus.height / 2),
      ),
    ).toBeLessThanOrEqual(4);
    expect(modelSlider.x + modelSlider.width).toBeLessThanOrEqual(
      sessionStatus.x,
    );

    const composer = page.getByRole("group", { name: "Message composer" });
    const composerBox = await boundingBox(composer);
    expect(composerBox.height).toBeLessThanOrEqual(56);
    expect(context.y + context.height).toBeLessThanOrEqual(composerBox.y);

    const controls = [
      page.getByRole("textbox", { name: "Message" }),
      page.getByRole("button", { name: "Start voice conversation" }),
      page.getByRole("button", { exact: true, name: "Send" }),
    ];
    const composerCenter = composerBox.y + composerBox.height / 2;
    for (const control of controls) {
      const box = await boundingBox(control);
      expect(
        Math.abs(box.y + box.height / 2 - composerCenter),
      ).toBeLessThanOrEqual(4);
    }
  });
}

// Issue #521: the composer should reveal longer prompts without consuming the
// whole chat viewport. Real browser geometry is the public seam because jsdom
// does not calculate textarea layout or wrapped-line scroll height.
for (const viewport of [PHONE, DESKTOP]) {
  test(`chat input grows from one through ten rows at ${viewport.width.toString()}px`, async ({
    page,
    login,
  }) => {
    await page.setViewportSize(viewport);
    await login();

    const input = page.getByRole("textbox", { name: "Message" });
    await expect(input).toHaveAttribute("rows", "1");
    const initialHeight = (await boundingBox(input)).height;

    const heightAfterLines = async (lineCount: number) => {
      await input.fill(
        Array.from(
          { length: lineCount },
          (_, index) => `Prompt line ${(index + 1).toString()}`,
        ).join("\n"),
      );
      return (await boundingBox(input)).height;
    };

    const fiveRowHeight = await heightAfterLines(5);
    const nineRowHeight = await heightAfterLines(9);
    const tenRowHeight = await heightAfterLines(10);
    const fifteenRowHeight = await heightAfterLines(15);

    expect(fiveRowHeight).toBeGreaterThan(initialHeight);
    expect(nineRowHeight).toBeGreaterThan(fiveRowHeight);
    expect(tenRowHeight).toBeGreaterThan(nineRowHeight);
    expect(fifteenRowHeight).toBeCloseTo(tenRowHeight, 0);
    expect(
      await input.evaluate(
        (element) => element.scrollHeight > element.clientHeight,
      ),
    ).toBe(true);

    const composerBox = await boundingBox(
      page.getByRole("group", { name: "Message composer" }),
    );
    const sendBox = await boundingBox(
      page.getByRole("button", { exact: true, name: "Send" }),
    );
    expect(
      composerBox.y + composerBox.height - (sendBox.y + sendBox.height),
    ).toBeLessThanOrEqual(12);
  });
}

test("desktop chat keeps the model selector reasonably narrow", async ({
  page,
  login,
}) => {
  await page.setViewportSize(DESKTOP);
  await login();

  const modelSelector = page.getByRole("group", { name: "Model" });
  await modelSelector.waitFor({ state: "visible" });
  const selectorBox = await boundingBox(modelSelector);
  const composerBox = await boundingBox(
    page.getByRole("textbox", { name: "Message" }),
  );

  expect(selectorBox.width).toBeLessThanOrEqual(384);
  expect(selectorBox.width).toBeLessThan(composerBox.width * 0.6);
});

for (const viewport of [PHONE, DESKTOP]) {
  test(`chat bubbles and tool traces stay visually coherent at ${viewport.width.toString()}px`, async ({
    page,
    login,
  }) => {
    await page.setViewportSize(viewport);
    await serveLongChat(page, [
      {
        ...longMessage(1, "user"),
        content: "Keep this dark bubble coherent",
      },
      {
        ...longMessage(2, "user"),
        content: "Review recent email",
        role: "scheduled",
        turn: {
          failure_code: null,
          failure_summary: null,
          intended_fire_at: "2026-08-26T03:00:00Z",
          occurrence_id: "018f0000-0000-7000-8000-000000000399",
          origin: "scheduled",
          status: "succeeded",
          trigger_id: "018f0000-0000-7000-8000-000000000398",
        },
      },
      {
        ...longMessage(3, "assistant"),
        content: "",
        role: "tool",
        tool_args: { query: "newer_than:1d" },
        tool_name: "search_gmail",
        tool_result: { details: { result: { messages: [{}, {}] } } },
      },
      {
        ...longMessage(4, "assistant"),
        content: "",
        role: "tool",
        tool_args: { message_id: "message-1" },
        tool_name: "read_gmail_message",
        tool_result: { ok: true },
      },
    ]);
    await login();

    const userRow = page.getByRole("article", { name: "You message" });
    const userText = page.getByText("Keep this dark bubble coherent");
    const scheduledText = page.getByText("Review recent email");
    await expect(userText).toBeVisible();
    await expect(scheduledText).toBeVisible();
    for (const content of [userText, scheduledText]) {
      expect(
        await content.evaluate(
          (element) => getComputedStyle(element).backgroundColor,
        ),
      ).toBe("rgba(0, 0, 0, 0)");
    }

    const activity = page.getByRole("article", { name: "Tool activity" });
    const summaries = activity.locator("button[aria-expanded]");
    await expect(summaries).toHaveCount(2);
    for (let index = 0; index < 2; index += 1) {
      expect(
        (await boundingBox(summaries.nth(index))).height,
      ).toBeLessThanOrEqual(30);
    }
    expect(
      await activity
        .getByText("Searched Gmail · 2 results")
        .evaluate((element) => getComputedStyle(element).fontSize),
    ).toBe("12px");
    const completedStyle = await activity
      .getByText("Completed")
      .first()
      .evaluate((element) => ({
        background: getComputedStyle(element).backgroundColor,
        fontWeight: getComputedStyle(element).fontWeight,
      }));
    expect(completedStyle).toEqual({
      background: "rgba(0, 0, 0, 0)",
      fontWeight: "400",
    });
    expect((await boundingBox(activity)).height).toBeLessThanOrEqual(66);
    await expect(
      page.getByRole("button", { name: "Quote message" }),
    ).toHaveCount(0);

    const copy = userRow.getByRole("button", { name: "Copy message" });
    const feedback = userRow.getByRole("button", {
      name: "Record product feedback",
    });
    await expect(copy.locator("svg")).toBeVisible();
    await expect(feedback.locator("svg")).toBeVisible();
    await expect(copy).toHaveText("");
    await expect(feedback).toHaveText("");
    const userBox = await boundingBox(userRow);
    for (const action of [copy, feedback]) {
      const actionBox = await boundingBox(action);
      expect(actionBox.y - userBox.y).toBeLessThanOrEqual(8);
      expect(actionBox.x).toBeGreaterThan(userBox.x + userBox.width * 0.75);
    }
  });
}

for (const viewport of [PHONE, TABLET_BELOW_DESKTOP]) {
  test(`chat stays anchored and composeable at ${viewport.width.toString()}px`, async ({
    page,
    login,
  }) => {
    await page.setViewportSize(viewport);
    await serveLongChat(page);
    await login();

    const transcript = page.locator(
      '[role="log"][aria-label="Chat transcript"]',
    );
    await transcript.waitFor({ state: "visible" });
    const composer = page.getByRole("textbox", { name: "Message" });
    await expect(composer).toBeVisible();

    const navTop = (
      await boundingBox(
        page.getByRole("navigation", { name: "Main navigation (compact)" }),
      )
    ).y;
    const sendBox = await boundingBox(
      page.getByRole("button", { exact: true, name: "Send" }),
    );
    expect(sendBox.y + sendBox.height).toBeLessThanOrEqual(navTop);

    await expect
      .poll(async () =>
        transcript.evaluate(
          (element) =>
            element.scrollHeight - element.clientHeight - element.scrollTop,
        ),
      )
      .toBeLessThanOrEqual(2);
    const scroll = await transcript.evaluate((element) => ({
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      scrollTop: element.scrollTop,
    }));
    expect(scroll.scrollHeight).toBeGreaterThan(scroll.clientHeight);
    expect(
      scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop,
    ).toBeLessThanOrEqual(2);

    const transcriptBox = await boundingBox(transcript);
    const userBubble = await boundingBox(
      transcript.getByRole("article", { name: "You message" }).last(),
    );
    expect(userBubble.width).toBeGreaterThan(transcriptBox.width * 0.85);

    expect(
      await page.locator("main").evaluate((element) => element.scrollHeight),
    ).toBeLessThanOrEqual(
      await page.locator("main").evaluate((element) => element.clientHeight),
    );
  });
}

for (const viewport of [PHONE, DESKTOP]) {
  test(`proposals history and grants stay bounded at ${viewport.width.toString()}px`, async ({
    page,
    login,
  }) => {
    await page.setViewportSize(viewport);
    await serveLargeProposalSurfaces(page);
    await login();

    const navName =
      viewport.width < 1024 ? "Main navigation (compact)" : "Main navigation";
    await page
      .getByRole("navigation", { name: navName })
      .getByRole("link", { name: /^Proposals/ })
      .click();

    await page.getByRole("tab", { name: /Decided/ }).click();
    await expect(
      page.getByRole("heading", { name: "Decided proposals (40)" }),
    ).toBeVisible();
    const historyPanel = page.getByRole("tabpanel", { name: /Decided/ });
    await expect
      .poll(() => historyPanel.locator('li[aria-label^="Proposal:"]').count())
      .toBe(25);
    await historyPanel
      .getByRole("searchbox", { name: "Search decided proposals" })
      .fill("Decision 36");
    await expect(historyPanel.getByText("Decision 36")).toBeVisible();
    await expect(historyPanel.getByText("Decision 35")).toBeHidden();

    await page.getByRole("tab", { name: /Grants/ }).click();
    const grantsPanel = page.getByRole("tabpanel", { name: /Grants/ });
    await expect(
      grantsPanel.getByRole("heading", { name: "Suggestions (35)" }),
    ).toBeVisible();
    await expect
      .poll(() =>
        grantsPanel.getByRole("button", { name: /^Grant operation-/ }).count(),
      )
      .toBe(25);
    await grantsPanel
      .getByRole("searchbox", { name: "Search grant suggestions" })
      .fill("scope-30");
    await expect(
      grantsPanel.getByRole("button", {
        name: "Grant operation-30 for scope-30",
      }),
    ).toBeVisible();
  });
}

test("phone width: every Browse tab stays fully readable", async ({
  page,
  login,
}) => {
  await page.setViewportSize(PHONE);
  await login();
  await page
    .getByRole("navigation", { name: "Main navigation (compact)" })
    .getByRole("link", { name: /^Browse/ })
    .click();

  const browseViews = page.getByRole("tablist", { name: "Browse view" });
  await browseViews.waitFor({ state: "visible" });
  for (const label of ["Memories", "Bucket", "Todos", "Reminders", "Panels"]) {
    const tabBox = await boundingBox(
      browseViews.getByRole("tab", { name: label }),
    );
    expect(tabBox.x).toBeGreaterThanOrEqual(0);
    expect(tabBox.x + tabBox.width).toBeLessThanOrEqual(PHONE.width + 1);
  }
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(PHONE.width);
});

test("phone width: primary navigation and tabs are 44px touch targets", async ({
  page,
  login,
}) => {
  await page.setViewportSize(PHONE);
  await login();

  const bottomNav = page.getByRole("navigation", {
    name: "Main navigation (compact)",
  });
  for (const label of ["Chat", "Proposals", "Inbox", "Browse", "Settings"]) {
    expect(
      (
        await boundingBox(
          bottomNav.getByRole("link", { name: new RegExp(`^${label}`) }),
        )
      ).height,
    ).toBeGreaterThanOrEqual(44);
  }

  await bottomNav.getByRole("link", { name: /^Browse/ }).click();
  const browseViews = page.getByRole("tablist", { name: "Browse view" });
  for (const label of ["Memories", "Bucket", "Todos", "Reminders", "Panels"]) {
    expect(
      (await boundingBox(browseViews.getByRole("tab", { name: label }))).height,
    ).toBeGreaterThanOrEqual(44);
  }
});

test("desktop width: left sidebar visible, bottom tabs hidden", async ({
  page,
  login,
}) => {
  await page.setViewportSize(DESKTOP);
  await login();

  const transcript = page.locator('[role="log"][aria-label="Chat transcript"]');
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
