import { cleanup, fireEvent, screen, waitFor } from "@solidjs/testing-library";
import { afterEach, describe, expect, test, vi } from "vitest";

import { FakeApi, navigateTo, renderApp } from "../testing/harness";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function installPushBrowser(subscription?: PushSubscription) {
  const subscribe = vi.fn<
    (options: PushSubscriptionOptions) => Promise<PushSubscription>
  >(() => Promise.resolve(subscription ?? fakeSubscription()));
  const register = vi.fn(() =>
    Promise.resolve({
      pushManager: {
        getSubscription: () => Promise.resolve(subscription ?? null),
        subscribe,
      },
    }),
  );
  vi.stubGlobal("Notification", {
    permission: "granted",
    requestPermission: () => Promise.resolve("granted"),
  });
  vi.stubGlobal("PushManager", { prototype: {} });
  Object.defineProperty(window.navigator, "serviceWorker", {
    configurable: true,
    value: { register },
  });
  return { register, subscribe };
}

function fakeSubscription(): PushSubscription {
  return {
    endpoint: "https://push.example/real",
    getKey(name: PushEncryptionKeyName) {
      if (name === "p256dh") {
        return Uint8Array.from([1, 2, 3]).buffer;
      }
      return Uint8Array.from([4, 5, 6]).buffer;
    },
    unsubscribe: () => Promise.resolve(true),
  } as PushSubscription;
}

describe("Push panel", () => {
  test("enabling push registers a real browser subscription", async () => {
    const browser = installPushBrowser();
    const api = new FakeApi({ authenticated: true });
    renderApp(api);
    await navigateTo("Settings");

    fireEvent.click(
      await screen.findByRole("button", { name: "Enable notifications" }),
    );

    await waitFor(() => {
      expect(api.subscribeCalls).toEqual([
        {
          auth: "BAUG",
          endpoint: "https://push.example/real",
          p256dh: "AQID",
        },
      ]);
    });
    expect(browser.register).toHaveBeenCalledWith("/sw.js");
    expect(browser.subscribe).toHaveBeenCalledOnce();
    expect(
      browser.subscribe.mock.calls[0]?.[0].applicationServerKey,
    ).toBeInstanceOf(Uint8Array);
    expect(browser.subscribe.mock.calls[0]?.[0].userVisibleOnly).toBe(true);
    expect(
      await screen.findByRole("button", { name: "Disable notifications" }),
    ).toBeInTheDocument();
  });
});
