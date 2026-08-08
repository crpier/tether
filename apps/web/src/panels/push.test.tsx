import { cleanup, fireEvent, screen, waitFor } from "@solidjs/testing-library";
import { afterEach, describe, expect, test, vi } from "vitest";

import { FakeApi, navigateTo, renderApp } from "../testing/harness";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(window.navigator, "serviceWorker");
});

interface PushBrowserOptions {
  permission?: NotificationPermission;
  requestPermission?: NotificationPermission;
  subscribeError?: Error;
  subscription?: PushSubscription;
}

function installPushBrowser(options: PushBrowserOptions = {}) {
  const subscribe = vi.fn<
    (options: PushSubscriptionOptions) => Promise<PushSubscription>
  >(() => {
    if (options.subscribeError !== undefined) {
      return Promise.reject(options.subscribeError);
    }
    return Promise.resolve(options.subscription ?? fakeSubscription());
  });
  const register = vi.fn(() =>
    Promise.resolve({
      pushManager: {
        getSubscription: () => Promise.resolve(options.subscription ?? null),
        subscribe,
      },
    }),
  );
  vi.stubGlobal("Notification", {
    permission: options.permission ?? "granted",
    requestPermission: () =>
      Promise.resolve(
        options.requestPermission ?? options.permission ?? "granted",
      ),
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
  test("default permission can be prompted and subscribed", async () => {
    const browser = installPushBrowser({
      permission: "default",
      requestPermission: "granted",
    });
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
    expect(browser.subscribe).toHaveBeenCalledOnce();
  });

  test("granted permission registers a real browser subscription", async () => {
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

  test("denied permission shows browser-settings recovery guidance", async () => {
    installPushBrowser({ permission: "denied" });
    const api = new FakeApi({ authenticated: true });
    renderApp(api);
    await navigateTo("Settings");

    expect(
      await screen.findByText("Notifications are blocked for this site."),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Use your browser's site settings/u),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Enable notifications" }),
    ).not.toBeInTheDocument();
  });

  test("unsupported browsers show a dedicated state", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);
    await navigateTo("Settings");

    expect(
      await screen.findByText("Push notifications are not supported here."),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Enable notifications" }),
    ).not.toBeInTheDocument();
  });

  test("subscription failures are shown inline", async () => {
    installPushBrowser({ subscribeError: new Error("subscription failed") });
    const api = new FakeApi({ authenticated: true });
    renderApp(api);
    await navigateTo("Settings");

    fireEvent.click(
      await screen.findByRole("button", { name: "Enable notifications" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "subscription failed",
    );
  });
});
