import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { FakeApi, notification, renderApp } from "../testing/harness";

afterEach(cleanup);

describe("Notifications panel", () => {
  test("persisted notifications load into the panel on mount", async () => {
    const api = new FakeApi({ authenticated: true });
    api.storedNotifications = [notification({ body: "call the dentist" })];
    renderApp(api);

    await screen.findByRole("heading", { name: "Tether chat" });
    const row = await screen.findByLabelText("Notification: call the dentist");
    expect(within(row).getByText("call the dentist")).toBeInTheDocument();
    // The message action reads as a "Reminder" so it is distinguishable from an
    // agent result.
    expect(within(row).getByText("Reminder")).toBeInTheDocument();
  });

  test("an agent-result notification is labelled and shows its prompt", async () => {
    const api = new FakeApi({ authenticated: true });
    api.storedNotifications = [
      notification({
        action_kind: "prompt",
        body: "It is sunny.",
        source_label: "what is the weather?",
      }),
    ];
    renderApp(api);

    await screen.findByRole("heading", { name: "Tether chat" });
    expect(await screen.findByText("It is sunny.")).toBeInTheDocument();
    expect(screen.getByText("Agent result")).toBeInTheDocument();
    expect(
      screen.getByText("Prompt: what is the weather?"),
    ).toBeInTheDocument();
  });

  test("notify frames refetch the persisted notifications list", async () => {
    const api = new FakeApi({ authenticated: true });
    const bus = renderApp(api);

    await waitFor(() => {
      expect(api.listNotificationsCalls).toBeGreaterThan(0);
    });
    const before = api.listNotificationsCalls;
    // The host persists before the frame arrives; the frame only prompts a
    // refetch of the authoritative list.
    api.storedNotifications = [notification({ body: "drink water" })];
    bus.emit({
      body: "drink water",
      title: null,
      trigger_id: "trig-9",
      type: "notify",
    });

    await waitFor(() => {
      expect(api.listNotificationsCalls).toBeGreaterThan(before);
    });
    expect(await screen.findByText("drink water")).toBeInTheDocument();
  });

  test("dismissing a notification removes it from the panel", async () => {
    const api = new FakeApi({ authenticated: true });
    api.storedNotifications = [notification({ body: "call the dentist" })];
    renderApp(api);

    const row = await screen.findByLabelText("Notification: call the dentist");
    fireEvent.click(
      within(row).getByRole("button", { name: "Dismiss notification" }),
    );

    await waitFor(() => {
      expect(api.dismissNotificationCalls).toHaveLength(1);
    });
    await waitFor(() => {
      expect(screen.queryByText("call the dentist")).not.toBeInTheDocument();
    });
  });

  test("clearing dismisses every notification", async () => {
    const api = new FakeApi({ authenticated: true });
    api.storedNotifications = [
      notification({ body: "call the dentist", id: "n1" }),
      notification({ body: "drink water", id: "n2" }),
    ];
    renderApp(api);

    await screen.findByText("call the dentist");
    fireEvent.click(screen.getByRole("button", { name: "Clear all" }));

    await waitFor(() => {
      expect(api.clearNotificationsCalls).toBe(1);
    });
    await waitFor(() => {
      expect(screen.queryByText("drink water")).not.toBeInTheDocument();
    });
  });
});
