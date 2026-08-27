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
    replyMode?: "spoken" | "text";
    turnId?: string;
    type: "abort" | "prompt";
  }[];
  statusRequests: string[];
} {
  let closed = false;
  let handlers: ChatBusHandlers | undefined;
  const sent: {
    content?: string;
    conversationId: string;
    replyMode?: "spoken" | "text";
    turnId?: string;
    type: "abort" | "prompt";
  }[] = [];
  const statusRequests: string[] = [];
  const bus: ChatBus = {
    abort(conversationId, turnId) {
      sent.push({ conversationId, turnId, type: "abort" });
    },
    close() {
      closed = true;
    },
    requestSessionStatus(conversationId) {
      statusRequests.push(conversationId);
    },
    sendPrompt(conversationId, content, replyMode) {
      sent.push({ content, conversationId, replyMode, type: "prompt" });
    },
  };
  return {
    createChatBus(nextHandlers) {
      handlers = nextHandlers;
      nextHandlers.onStatus?.("open");
      return bus;
    },
    emit(frame) {
      if (!closed) {
        handlers?.onFrame(frame);
      }
    },
    sent,
    statusRequests,
  };
}

// The app uses a shared routed shell (#250): tests running with vitest's
// `isolate: false` share one jsdom `window.history` across every test file in
// the worker, so a route left over from a previous test would otherwise leak
// into the next render. Reset to canonical Chat before every render unless
// the caller wants to land somewhere else.
// The shell hides the inactive responsive nav from accessibility; jsdom has no
// layout, so it falls back to the desktop sidebar unless tests mock
// matchMedia.
export async function navigateTo(
  label: "Chat" | "Health" | "Browse" | "Settings",
): Promise<void> {
  const nav = await screen.findByRole("navigation", {
    name: "Main navigation",
  });
  // Use a regex so callers can match a navigation label consistently.
  fireEvent.click(
    within(nav).getByRole("link", { name: new RegExp(`^${label}`) }),
  );
}

export function renderApp(
  host: FakeHost,
  bus = createBusHarness(),
  options: { path?: string } = {},
) {
  window.history.pushState({}, "", options.path ?? "/chat");
  window.dispatchEvent(new PopStateEvent("popstate"));
  render(() => <App createChatBus={bus.createChatBus} host={host} />);
  return bus;
}
