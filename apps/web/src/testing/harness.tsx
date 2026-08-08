import { fireEvent, render, screen, within } from "@solidjs/testing-library";

import { App } from "../app";
import type {
  ChatBus,
  ChatBusHandlers,
  ChatFrame,
  CreateChatBus,
} from "../chat-bus";
import { FakeHost } from "./fake-host";

export { FakeHost } from "./fake-host";
export * from "./fixtures";

export function input(element: HTMLElement): HTMLInputElement {
  if (!(element instanceof HTMLInputElement)) {
    throw new Error("expected input");
  }
  return element;
}

export function textarea(element: HTMLElement): HTMLTextAreaElement {
  if (!(element instanceof HTMLTextAreaElement)) {
    throw new Error("expected textarea");
  }
  return element;
}

export function createBusHarness(): {
  createChatBus: CreateChatBus;
  emit(frame: ChatFrame): void;
  sent: {
    content?: string;
    conversationId: string;
    type: "abort" | "prompt";
  }[];
} {
  let closed = false;
  let handlers: ChatBusHandlers | undefined;
  const sent: {
    content?: string;
    conversationId: string;
    type: "abort" | "prompt";
  }[] = [];
  const bus: ChatBus = {
    abort(conversationId) {
      sent.push({ conversationId, type: "abort" });
    },
    close() {
      closed = true;
    },
    sendPrompt(conversationId, content) {
      sent.push({ content, conversationId, type: "prompt" });
    },
  };
  return {
    createChatBus(nextHandlers) {
      handlers = nextHandlers;
      return bus;
    },
    emit(frame) {
      if (!closed) {
        handlers?.onFrame(frame);
      }
    },
    sent,
  };
}

// The app is a routed 5-page shell (#250): tests running with vitest's
// `isolate: false` share one jsdom `window.history` across every test file in
// the worker, so a route left over from a previous test would otherwise leak
// into the next render. Reset to the root path (pure chat, the home page)
// before every render unless the caller wants to land somewhere else.
// The shell hides the inactive responsive nav from accessibility; jsdom has no
// layout, so it falls back to the desktop sidebar unless tests mock
// matchMedia.
export async function navigateTo(
  label: "Chat" | "Proposals" | "Inbox" | "Browse" | "Settings",
): Promise<void> {
  const nav = await screen.findByRole("navigation", {
    name: "Main navigation",
  });
  // `getByRole`'s `name` matcher has no `exact` option (a plain string is
  // always exact-equality) — a regex is the only way to substring-match past
  // a badge count appended to the accessible name (e.g. "Inbox" -> "Inbox3").
  fireEvent.click(
    within(nav).getByRole("link", { name: new RegExp(`^${label}`) }),
  );
}

export function renderApp(
  host: FakeHost,
  bus = createBusHarness(),
  options: { path?: string } = {},
) {
  window.history.pushState({}, "", options.path ?? "/");
  render(() => <App createChatBus={bus.createChatBus} host={host} />);
  return bus;
}
