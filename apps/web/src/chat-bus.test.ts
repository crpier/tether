import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { createBrowserChatBus } from "./chat-bus";
import type { ConnectionStatus } from "./chat-bus";

type Listener = (event: unknown) => void;

class FakeSocket {
  static instances: FakeSocket[] = [];
  static readonly OPEN = 1;
  readyState = 0;
  sent: string[] = [];
  private listeners: Record<string, Listener[]> = {};

  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }

  addEventListener(type: string, callback: Listener): void {
    (this.listeners[type] ??= []).push(callback);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.emit("close");
  }

  open(): void {
    this.readyState = FakeSocket.OPEN;
    this.emit("open");
  }

  emit(type: string, event?: unknown): void {
    for (const callback of this.listeners[type] ?? []) {
      callback(event);
    }
  }
}

function noop(): void {
  /* test stub */
}

const originalWebSocket = globalThis.WebSocket;

beforeEach(() => {
  vi.useFakeTimers();
  FakeSocket.instances = [];
  (globalThis as { WebSocket: unknown }).WebSocket = FakeSocket;
});

afterEach(() => {
  vi.useRealTimers();
  (globalThis as { WebSocket: unknown }).WebSocket = originalWebSocket;
});

describe("createBrowserChatBus reconnection", () => {
  test("reports status transitions across a reconnect", () => {
    const statuses: ConnectionStatus[] = [];
    createBrowserChatBus({
      onDisconnect: noop,
      onFrame: noop,
      onStatus(status) {
        statuses.push(status);
      },
    });

    expect(statuses).toEqual(["connecting"]);
    FakeSocket.instances[0].open();
    expect(statuses).toEqual(["connecting", "open"]);
    FakeSocket.instances[0].close();
    expect(statuses).toEqual(["connecting", "open", "closed"]);

    vi.advanceTimersByTime(500);
    expect(statuses).toEqual(["connecting", "open", "closed", "connecting"]);
  });

  test("backs off exponentially while the server stays down", () => {
    createBrowserChatBus({ onDisconnect: noop, onFrame: noop });

    // First close retries after 500ms.
    FakeSocket.instances[0].close();
    vi.advanceTimersByTime(499);
    expect(FakeSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.instances).toHaveLength(2);

    // Second close (still never opened) doubles the delay to 1000ms.
    FakeSocket.instances[1].close();
    vi.advanceTimersByTime(500);
    expect(FakeSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(500);
    expect(FakeSocket.instances).toHaveLength(3);
  });

  test("a clean open resets the backoff", () => {
    createBrowserChatBus({ onDisconnect: noop, onFrame: noop });

    FakeSocket.instances[0].close();
    vi.advanceTimersByTime(500);
    FakeSocket.instances[1].open();
    FakeSocket.instances[1].close();

    // Reset means the next retry is the initial 500ms again, not 1000ms.
    vi.advanceTimersByTime(500);
    expect(FakeSocket.instances).toHaveLength(3);
  });

  test("queued prompts flush once the socket opens", () => {
    const bus = createBrowserChatBus({ onDisconnect: noop, onFrame: noop });
    bus.sendPrompt("c1", "hello");

    const socket = FakeSocket.instances[0];
    expect(socket.sent).toEqual([]);
    socket.open();
    expect(socket.sent).toHaveLength(1);
    expect(JSON.parse(socket.sent[0])).toMatchObject({
      conversation_id: "c1",
      content: "hello",
      type: "prompt",
    });
  });

  test("close stops further reconnection", () => {
    const bus = createBrowserChatBus({ onDisconnect: noop, onFrame: noop });
    bus.close();
    FakeSocket.instances[0].close();
    vi.advanceTimersByTime(5000);
    expect(FakeSocket.instances).toHaveLength(1);
  });
});
