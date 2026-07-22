import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { ApiError } from "../api";
import {
  FakeApi,
  input,
  navigateTo,
  renderApp,
  trigger,
} from "../testing/harness";

afterEach(cleanup);

describe("Triggers panel", () => {
  test("lists existing reminders", async () => {
    const api = new FakeApi({
      authenticated: true,
      triggers: [trigger({ payload: "water the plants" })],
    });
    renderApp(api);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));

    expect(
      await screen.findByLabelText("Reminder: water the plants"),
    ).toBeInTheDocument();
  });

  test("creating a one-off reminder posts the right body", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));

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
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));

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
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));

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
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));

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
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));

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
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));

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
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));

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
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));

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
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));

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

  test("a 409 that reveals a concurrent edit is surfaced, not auto-retried", async () => {
    // The refetched definition differs from the one the edit was based on, so
    // someone (another tab, the agent) genuinely edited it. Auto-resubmitting
    // would silently overwrite that edit (docs/principles.md); the save must
    // stop, show the conflict, and refresh the list instead.
    const api = new FakeApi({
      authenticated: true,
      triggers: [
        trigger({
          id: "trig-1",
          payload: "water the plants",
          recurrence: "daily",
          timezone: "UTC",
          version: 1,
          wall_time: "09:00",
        }),
      ],
    });
    api.serverTriggerVersions = { "trig-1": 2 };
    api.serverTriggerEdits = { "trig-1": { payload: "water the cactus" } };
    renderApp(api);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));

    const row = await screen.findByLabelText("Reminder: water the plants");
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    fireEvent.input(input(screen.getByLabelText("Reminder")), {
      target: { value: "water the garden" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save reminder" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "changed elsewhere",
    );
    expect(api.updateTriggerCalls).toHaveLength(1);
    // The list now shows the concurrent edit; the user's draft stays in the form.
    expect(
      await screen.findByLabelText("Reminder: water the cactus"),
    ).toBeInTheDocument();
    expect(input(screen.getByLabelText("Reminder")).value).toBe(
      "water the garden",
    );

    // Saving again after reviewing is a deliberate overwrite: it carries the
    // fresh version and lands.
    fireEvent.click(screen.getByRole("button", { name: "Save reminder" }));
    await waitFor(() => {
      expect(api.updateTriggerCalls).toHaveLength(2);
    });
    expect(api.updateTriggerCalls[1].body.version).toBe(2);
    expect(
      await screen.findByLabelText("Reminder: water the garden"),
    ).toBeInTheDocument();
  });

  test("a failed edit retry reports its own error, not the original 409", async () => {
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
    api.updateTriggerRejections = [new ApiError(409), new ApiError(422)];
    renderApp(api);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));

    const row = await screen.findByLabelText("Reminder: water the plants");
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    fireEvent.input(input(screen.getByLabelText("Reminder")), {
      target: { value: "water the garden" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save reminder" }));

    await waitFor(() => {
      expect(api.updateTriggerCalls).toHaveLength(2);
    });
    const alert = await screen.findByRole("alert");
    // The 422 from the retry is what actually stopped the save; parroting the
    // already-handled 409 ("refresh and try again") would mislead.
    expect(alert).toHaveTextContent(new ApiError(422).message);
    expect(alert).not.toHaveTextContent(new ApiError(409).message);
  });

  test("a failed delete retry reports its own error, not the original 409", async () => {
    const api = new FakeApi({
      authenticated: true,
      triggers: [
        trigger({ id: "trig-1", payload: "renew passport", version: 1 }),
      ],
    });
    api.deleteTriggerRejections = [new ApiError(409), new ApiError(500)];
    renderApp(api);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));

    const row = await screen.findByLabelText("Reminder: renew passport");
    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(api.deleteTriggerCalls).toHaveLength(2);
    });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(new ApiError(500).message);
    expect(alert).not.toHaveTextContent(new ApiError(409).message);
  });

  test("editing another reminder clears the previous one's inactive-branch fields", async () => {
    const api = new FakeApi({
      authenticated: true,
      triggers: [
        trigger({
          id: "trig-a",
          next_fire_at: "2099-01-01T15:00:00Z",
          payload: "stretch",
          recurrence: "once",
        }),
        trigger({
          id: "trig-b",
          payload: "review week",
          recurrence: "weekly",
          timezone: "Europe/Bucharest",
          wall_time: "08:30",
          weekday: 4,
        }),
      ],
    });
    renderApp(api);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));

    // Open the one-off first so its fire time lands in the form, then switch
    // to editing the weekly one. Flipping Repeat to "once" mid-edit must show
    // an empty date field, not the leftover from the other reminder.
    const onceRow = await screen.findByLabelText("Reminder: stretch");
    fireEvent.click(within(onceRow).getByRole("button", { name: "Edit" }));
    const weeklyRow = screen.getByLabelText("Reminder: review week");
    fireEvent.click(within(weeklyRow).getByRole("button", { name: "Edit" }));

    fireEvent.change(screen.getByDisplayValue("Weekly"), {
      target: { value: "once" },
    });
    expect(input(screen.getByLabelText("Date and time")).value).toBe("");
  });

  test("editing another reminder resets the recurring fields to defaults", async () => {
    const api = new FakeApi({
      authenticated: true,
      triggers: [
        trigger({
          id: "trig-a",
          next_fire_at: "2099-01-01T15:00:00Z",
          payload: "stretch",
          recurrence: "once",
        }),
        trigger({
          id: "trig-b",
          payload: "review week",
          recurrence: "weekly",
          timezone: "Europe/Bucharest",
          wall_time: "08:30",
          weekday: 4,
        }),
      ],
    });
    renderApp(api);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));

    // Mirror image of the previous test: open the weekly one first, then the
    // one-off. Flipping Repeat to "daily" mid-edit must show the defaults, not
    // the weekly reminder's time/zone.
    const weeklyRow = await screen.findByLabelText("Reminder: review week");
    fireEvent.click(within(weeklyRow).getByRole("button", { name: "Edit" }));
    const onceRow = screen.getByLabelText("Reminder: stretch");
    fireEvent.click(within(onceRow).getByRole("button", { name: "Edit" }));

    fireEvent.change(screen.getByDisplayValue("Once"), {
      target: { value: "daily" },
    });
    expect(input(screen.getByLabelText("Time of day")).value).toBe("09:00");
    const defaultTimezone =
      Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    expect(input(screen.getByLabelText("Time zone")).value).toBe(
      defaultTimezone,
    );
  });

  test("saving an untouched one-off edit preserves the seconds of fire_at", async () => {
    // Agent-created triggers carry seconds; the pre-filled datetime-local stamp
    // must not truncate them, or an untouched save shifts the instant.
    const api = new FakeApi({
      authenticated: true,
      triggers: [
        trigger({
          id: "trig-1",
          next_fire_at: "2099-01-01T15:00:42Z",
          payload: "stretch",
        }),
      ],
    });
    renderApp(api);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));

    const row = await screen.findByLabelText("Reminder: stretch");
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Save reminder" }));

    await waitFor(() => {
      expect(api.updateTriggerCalls).toHaveLength(1);
    });
    const fireAt = api.updateTriggerCalls[0].body.fire_at;
    expect(fireAt).not.toBeNull();
    expect(new Date(fireAt ?? "").getTime()).toBe(
      new Date("2099-01-01T15:00:42Z").getTime(),
    );
  });

  test("creating a reminder resets the whole form, like a saved edit", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));

    fireEvent.input(input(await screen.findByLabelText("Reminder")), {
      target: { value: "weekly review" },
    });
    fireEvent.change(screen.getByDisplayValue("Once"), {
      target: { value: "weekly" },
    });
    fireEvent.input(input(screen.getByLabelText("Time of day")), {
      target: { value: "10:15" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add reminder" }));

    await waitFor(() => {
      expect(api.createTriggerCalls).toHaveLength(1);
    });
    expect(input(screen.getByLabelText("Reminder")).value).toBe("");
    expect(screen.getByDisplayValue("Once")).toBeInTheDocument();
  });

  test("cancelling an edit resets the form without saving", async () => {
    const api = new FakeApi({
      authenticated: true,
      triggers: [trigger({ id: "trig-1", payload: "water the plants" })],
    });
    renderApp(api);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));

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
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("button", { name: "Reminders" }));

    await screen.findByLabelText("Reminder");
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
