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

  test("clicking Edit pre-fills the form with the reminder's values", async () => {
    const api = new FakeApi({
      authenticated: true,
      triggers: [
        trigger({
          action_kind: "prompt",
          id: "trig-1",
          payload: "summarise inbox",
          recurrence: "weekly",
          timezone: "Europe/Bucharest",
          version: 2,
          wall_time: "08:30",
          weekday: 4,
        }),
      ],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Reminder: summarise inbox");
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));

    expect(input(screen.getByLabelText("Reminder")).value).toBe(
      "summarise inbox",
    );
    expect(screen.getByDisplayValue("Weekly")).toBeInTheDocument();
    expect(
      screen.getByDisplayValue("Run this as an agent prompt"),
    ).toBeInTheDocument();
    expect(input(screen.getByLabelText("Time of day")).value).toBe("08:30");
    expect(input(screen.getByLabelText("Time zone")).value).toBe(
      "Europe/Bucharest",
    );
    expect(screen.getByDisplayValue("Friday")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Save reminder" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  });

  test("editing a one-off reminder pre-fills its fire time in local form", async () => {
    const api = new FakeApi({
      authenticated: true,
      triggers: [
        trigger({
          id: "trig-1",
          next_fire_at: "2099-01-01T15:00:00Z",
          payload: "stretch",
        }),
      ],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Reminder: stretch");
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));

    const field = input(screen.getByLabelText("Date and time"));
    // The datetime-local stamp is in local time; it must denote the same instant.
    expect(new Date(field.value).getTime()).toBe(
      new Date("2099-01-01T15:00:00Z").getTime(),
    );
  });

  test("saving an edit PUTs the new definition with the observed version", async () => {
    const api = new FakeApi({
      authenticated: true,
      triggers: [
        trigger({
          id: "trig-1",
          payload: "water the plants",
          recurrence: "daily",
          timezone: "UTC",
          version: 3,
          wall_time: "09:00",
        }),
      ],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Reminder: water the plants");
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    fireEvent.input(input(screen.getByLabelText("Reminder")), {
      target: { value: "water the garden" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save reminder" }));

    await waitFor(() => {
      expect(api.updateTriggerCalls).toHaveLength(1);
    });
    const call = api.updateTriggerCalls[0];
    expect(call.triggerId).toBe("trig-1");
    expect(call.body.version).toBe(3);
    expect(call.body.payload).toBe("water the garden");
    expect(call.body.recurrence).toBe("daily");
    expect(call.body.time_of_day).toBe("09:00");
    expect(call.body.timezone).toBe("UTC");
    expect(api.createTriggerCalls).toHaveLength(0);
    // The form leaves edit mode and resets once the save lands.
    expect(
      await screen.findByRole("button", { name: "Add reminder" }),
    ).toBeInTheDocument();
    expect(input(screen.getByLabelText("Reminder")).value).toBe("");
    expect(
      await screen.findByLabelText("Reminder: water the garden"),
    ).toBeInTheDocument();
  });

  test("saving an edit recovers from a stale-version 409", async () => {
    // Same race as delete: the row on screen holds the pre-fire version. The
    // save must refetch the current version and retry once instead of
    // dead-ending on a bare 409.
    const api = new FakeApi({
      authenticated: true,
      triggers: [
        trigger({
          id: "trig-1",
          payload: "water the plants",
          recurrence: "daily",
          version: 1,
          wall_time: "09:00",
        }),
      ],
    });
    api.serverTriggerVersions = { "trig-1": 2 };
    renderApp(api);

    const row = await screen.findByLabelText("Reminder: water the plants");
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    fireEvent.input(input(screen.getByLabelText("Reminder")), {
      target: { value: "water the garden" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save reminder" }));

    await waitFor(() => {
      expect(api.updateTriggerCalls).toHaveLength(2);
    });
    expect(api.updateTriggerCalls[0].body.version).toBe(1);
    expect(api.updateTriggerCalls[1].body.version).toBe(2);
    expect(
      await screen.findByLabelText("Reminder: water the garden"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  test("cancelling an edit resets the form without saving", async () => {
    const api = new FakeApi({
      authenticated: true,
      triggers: [trigger({ id: "trig-1", payload: "water the plants" })],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Reminder: water the plants");
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    fireEvent.input(input(screen.getByLabelText("Reminder")), {
      target: { value: "changed my mind" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(
      screen.getByRole("button", { name: "Add reminder" }),
    ).toBeInTheDocument();
    expect(input(screen.getByLabelText("Reminder")).value).toBe("");
    expect(
      screen.queryByRole("button", { name: "Cancel" }),
    ).not.toBeInTheDocument();
    expect(api.updateTriggerCalls).toHaveLength(0);
    expect(api.createTriggerCalls).toHaveLength(0);
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
