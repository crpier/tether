import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { FakeApi, input, renderApp, trigger } from "../testing/harness";

afterEach(cleanup);

describe("Triggers panel", () => {
  test("lists existing reminders", async () => {
    const api = new FakeApi({
      authenticated: true,
      triggers: [trigger({ payload: "water the plants" })],
    });
    renderApp(api);

    expect(
      await screen.findByLabelText("Reminder: water the plants"),
    ).toBeInTheDocument();
  });

  test("creating a one-off reminder posts the right body", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    fireEvent.input(input(await screen.findByLabelText("Reminder")), {
      target: { value: "stretch" },
    });
    fireEvent.input(input(screen.getByLabelText("Date and time")), {
      target: { value: "2099-01-01T15:00" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add reminder" }));

    await waitFor(() => {
      expect(api.createTriggerCalls).toHaveLength(1);
    });
    const body = api.createTriggerCalls[0];
    expect(body.payload).toBe("stretch");
    expect(body.recurrence).toBe("once");
    expect(body.action_kind).toBe("message");
    expect(body.fire_at).not.toBeNull();
    expect(body.time_of_day).toBeNull();
    expect(
      await screen.findByLabelText("Reminder: stretch"),
    ).toBeInTheDocument();
  });

  test("does not create a one-off reminder in the past", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    fireEvent.input(input(await screen.findByLabelText("Reminder")), {
      target: { value: "too late" },
    });
    fireEvent.input(input(screen.getByLabelText("Date and time")), {
      target: { value: "2020-01-01T15:00" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add reminder" }));

    // The `min` guard blocks submission natively; the JS check is a backstop.
    // Either way, no past reminder is ever posted.
    await Promise.resolve();
    expect(api.createTriggerCalls).toHaveLength(0);
    expect(
      screen.queryByLabelText("Reminder: too late"),
    ).not.toBeInTheDocument();
  });

  test("the reminder time input forbids past instants via min", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    const field = input(await screen.findByLabelText("Date and time"));
    const min = field.getAttribute("min");
    expect(min).toBeTruthy();
    // `min` is a local `YYYY-MM-DDTHH:MM` stamp of roughly now.
    expect(new Date(min ?? "").getTime()).toBeLessThanOrEqual(
      Date.now() + 1000,
    );
  });

  test("deleting a reminder calls the API with its version", async () => {
    const api = new FakeApi({
      authenticated: true,
      triggers: [
        trigger({ id: "trig-1", payload: "renew passport", version: 3 }),
      ],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Reminder: renew passport");
    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(api.deleteTriggerCalls).toEqual([
        { triggerId: "trig-1", version: 3 },
      ]);
    });
  });

  test("deleting a fired reminder recovers from a stale-version 409", async () => {
    // The row on screen still holds the pre-fire version; the server bumped it
    // when the trigger fired. Delete must not dead-end on a bare 409 — it should
    // refetch the current version and retry so the reminder actually goes away.
    const api = new FakeApi({
      authenticated: true,
      triggers: [
        trigger({ id: "trig-1", payload: "renew passport", version: 1 }),
      ],
    });
    api.serverTriggerVersions = { "trig-1": 2 };
    renderApp(api);

    const row = await screen.findByLabelText("Reminder: renew passport");
    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(api.deleteTriggerCalls).toEqual([
        { triggerId: "trig-1", version: 1 },
        { triggerId: "trig-1", version: 2 },
      ]);
    });
    expect(
      screen.queryByLabelText("Reminder: renew passport"),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  test("the reminder action help text distinguishes the two kinds", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    await screen.findByRole("heading", { name: "Tether chat" });
    expect(
      screen.getByText(
        "Your text is delivered verbatim as a notification when it fires.",
      ),
    ).toBeInTheDocument();

    const actionSelect = screen.getByDisplayValue("Notify me with this text");
    fireEvent.change(actionSelect, { target: { value: "prompt" } });

    expect(
      screen.getByText(
        "The agent runs your text when it fires; its answer arrives as a notification.",
      ),
    ).toBeInTheDocument();
  });
});
