import { createQuery, useQueryClient } from "@tanstack/solid-query";
import { For, Show, createSignal } from "solid-js";
import type { JSX } from "solid-js";

import { ApiError } from "../api";
import type {
  CreateTrigger,
  TetherApi,
  Trigger,
  TriggerActionKind,
  TriggerRecurrence,
} from "../api";
import { panelClass } from "../lib/panel";
import { queryKeys } from "../lib/query-keys";
import { Button } from "@/components/ui/button";
import {
  TextField,
  TextFieldInput,
  TextFieldLabel,
} from "@/components/ui/text-field";

const selectClass =
  "border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 h-9 rounded-md border px-3 py-1 text-sm shadow-xs transition-[color,box-shadow] outline-none focus-visible:ring-[3px]";
const fieldLabelClass = "text-muted-foreground text-xs font-medium";

function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function formatFireTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

// A `datetime-local` value is a local (not UTC) stamp, so the `min` guard and
// the edit pre-fill have to be built from local components rather than
// `toISOString()`. Seconds are included (with a matching `step` on the input)
// so that saving an untouched edit of an agent-created trigger does not
// silently truncate its fire time to the minute.
function localDateTimeStamp(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  const year = String(date.getFullYear()).padStart(4, "0");
  const month = pad(date.getMonth() + 1);
  const day = pad(date.getDate());
  const hours = pad(date.getHours());
  const minutes = pad(date.getMinutes());
  const seconds = pad(date.getSeconds());
  return `${year}-${month}-${day}T${hours}:${minutes}:${seconds}`;
}

// The definition fields a save could overwrite: everything the form edits.
// `next_fire_at` counts only for one-off triggers — recurring ones advance it
// on every fire without the definition having changed.
function sameDefinition(a: Trigger, b: Trigger): boolean {
  return (
    a.payload === b.payload &&
    a.recurrence === b.recurrence &&
    a.action_kind === b.action_kind &&
    a.wall_time === b.wall_time &&
    a.timezone === b.timezone &&
    a.weekday === b.weekday &&
    (a.recurrence !== "once" || a.next_fire_at === b.next_fire_at)
  );
}

const WEEKDAYS = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
];

export function TriggersPanel(props: { api: TetherApi }) {
  const queryClient = useQueryClient();
  const triggersQuery = createQuery(() => ({
    queryFn: () => props.api.listTriggers(),
    queryKey: queryKeys.triggers,
  }));

  const [recurrence, setRecurrence] = createSignal<TriggerRecurrence>("once");
  const [actionKind, setActionKind] =
    createSignal<TriggerActionKind>("message");
  const [payload, setPayload] = createSignal("");
  const [fireAt, setFireAt] = createSignal("");
  const [timeOfDay, setTimeOfDay] = createSignal("09:00");
  const [timezone, setTimezone] = createSignal(browserTimezone());
  const [weekday, setWeekday] = createSignal(0);
  const [error, setError] = createSignal<string | undefined>();
  // The trigger being edited, as observed at click time: its version is the
  // optimistic-concurrency token and its definition is the basis a 409 retry
  // is judged against. Undefined when the form is in create mode.
  const [editing, setEditing] = createSignal<Trigger | undefined>();

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.triggers });
    void queryClient.refetchQueries({ queryKey: queryKeys.triggers });
  };

  const resetForm = () => {
    setEditing(undefined);
    setPayload("");
    setFireAt("");
    setRecurrence("once");
    setActionKind("message");
    setTimeOfDay("09:00");
    setTimezone(browserTimezone());
    setWeekday(0);
  };

  const startEdit = (trigger: Trigger) => {
    // Reset first so fields on the branch this trigger does not use (e.g. the
    // one-off date after editing a recurring reminder) never carry leftovers
    // from a previous edit that resurface on a mid-edit Repeat flip.
    resetForm();
    setEditing(trigger);
    setPayload(trigger.payload);
    setRecurrence(trigger.recurrence);
    setActionKind(trigger.action_kind);
    if (trigger.recurrence === "once") {
      setFireAt(localDateTimeStamp(new Date(trigger.next_fire_at)));
    } else {
      setTimeOfDay(trigger.wall_time ?? "09:00");
      setTimezone(trigger.timezone);
      setWeekday(trigger.weekday ?? 0);
    }
    setError(undefined);
  };

  const submit = () => {
    const rec = recurrence();
    if (payload().trim().length === 0) {
      setError("Add a reminder message");
      return;
    }
    let fireAtIso: string | null = null;
    if (rec === "once") {
      const parsed = new Date(fireAt());
      if (Number.isNaN(parsed.getTime())) {
        setError("Pick a date and time");
        return;
      }
      if (parsed.getTime() < Date.now()) {
        setError("Pick a time in the future");
        return;
      }
      fireAtIso = parsed.toISOString();
    }
    const body: CreateTrigger = {
      action_kind: actionKind(),
      fire_at: fireAtIso,
      payload: payload().trim(),
      recurrence: rec,
      time_of_day: rec === "once" ? null : timeOfDay(),
      timezone: rec === "once" ? null : timezone(),
      weekday: rec === "weekly" ? weekday() : null,
    };
    const target = editing();
    void (async () => {
      setError(undefined);
      try {
        if (target === undefined) {
          await props.api.createTrigger(body);
        } else {
          await props.api.updateTrigger(target.id, {
            ...body,
            version: target.version,
          });
        }
        resetForm();
        refresh();
      } catch (caught) {
        // Same stale-version race as delete: the trigger fired after we loaded
        // the row, so the server's version moved on. Refetch and retry once
        // with the fresh version instead of dead-ending on a bare 409 — unless
        // the refetch reveals a genuine concurrent edit.
        if (
          target !== undefined &&
          caught instanceof ApiError &&
          caught.status === 409
        ) {
          setError(await retryUpdateWithFreshVersion(target, body));
          return;
        }
        setError(
          caught instanceof Error
            ? caught.message
            : target === undefined
              ? "Could not create reminder"
              : "Could not update reminder",
        );
      }
    })();
  };

  // Refetch triggers, then retry the update with the fresh version — but only
  // when the fresh definition still matches the basis the edit was formulated
  // against (i.e. only the version moved, e.g. because the trigger fired). A
  // changed definition is a genuine concurrent edit; auto-resubmitting would
  // silently overwrite it (docs/principles.md, "operations that overwrite
  // distinct prior state"), so the conflict is surfaced instead. Returns
  // undefined when the save landed, or the error message to show.
  const retryUpdateWithFreshVersion = async (
    basis: Trigger,
    body: CreateTrigger,
  ): Promise<string | undefined> => {
    await queryClient.refetchQueries({ queryKey: queryKeys.triggers });
    const fresh = (
      queryClient.getQueryData<Trigger[]>(queryKeys.triggers) ?? []
    ).find((candidate) => candidate.id === basis.id);
    if (fresh === undefined) {
      return "This reminder no longer exists, so the edit cannot be saved.";
    }
    if (!sameDefinition(basis, fresh)) {
      // Re-arm the form against the fresh row (keeping the user's draft) so a
      // second Save, after reviewing the refreshed list, deliberately wins.
      setEditing(fresh);
      return "This reminder changed elsewhere; the list now shows the latest version. Save again to overwrite it, or cancel.";
    }
    try {
      await props.api.updateTrigger(basis.id, {
        ...body,
        version: fresh.version,
      });
      resetForm();
      refresh();
      return undefined;
    } catch (retryCaught) {
      return retryCaught instanceof Error
        ? retryCaught.message
        : "Could not update reminder";
    }
  };

  const remove = (triggerId: string, version: number) => {
    void (async () => {
      setError(undefined);
      try {
        await props.api.deleteTrigger(triggerId, version);
        refresh();
      } catch (caught) {
        // A fired trigger's version is bumped server-side, so a delete carrying
        // the row we loaded 409s. Rather than dead-end on a bare "Request
        // failed: 409", refetch the current version and retry once so the
        // reminder actually goes away (the invalidate-on-fire refresh usually
        // beats the click, but this closes the race and reconnect windows).
        if (caught instanceof ApiError && caught.status === 409) {
          setError(await retryDeleteWithFreshVersion(triggerId));
          return;
        }
        setError(
          caught instanceof Error
            ? caught.message
            : "Could not delete reminder",
        );
      }
    })();
  };

  // Refetch triggers, then retry the delete with the current version. Returns
  // undefined when the reminder is now gone (deleted here, or already absent
  // server-side), or the retry's own error message to show.
  const retryDeleteWithFreshVersion = async (
    triggerId: string,
  ): Promise<string | undefined> => {
    await queryClient.refetchQueries({ queryKey: queryKeys.triggers });
    const fresh = (
      queryClient.getQueryData<Trigger[]>(queryKeys.triggers) ?? []
    ).find((candidate) => candidate.id === triggerId);
    if (fresh === undefined) {
      refresh();
      return undefined;
    }
    try {
      await props.api.deleteTrigger(triggerId, fresh.version);
      refresh();
      return undefined;
    } catch (retryCaught) {
      return retryCaught instanceof Error
        ? retryCaught.message
        : "Could not delete reminder";
    }
  };

  const onSubmit: JSX.EventHandler<HTMLFormElement, SubmitEvent> = (event) => {
    event.preventDefault();
    submit();
  };

  return (
    <section aria-label="Reminders" class={panelClass}>
      <h2 class="mb-3 text-sm font-semibold">Reminders</h2>
      <form class="space-y-3" onSubmit={onSubmit}>
        <TextField onChange={setPayload} value={payload()}>
          <TextFieldLabel>Reminder</TextFieldLabel>
          <TextFieldInput name="payload" />
        </TextField>
        <label class="grid gap-1">
          <span class={fieldLabelClass}>Repeat</span>
          <select
            class={selectClass}
            name="recurrence"
            onChange={(event) => {
              setRecurrence(event.currentTarget.value as TriggerRecurrence);
            }}
            value={recurrence()}
          >
            <option value="once">Once</option>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
          </select>
        </label>
        <label class="grid gap-1">
          <span class={fieldLabelClass}>Action</span>
          <select
            class={selectClass}
            name="action_kind"
            onChange={(event) => {
              setActionKind(event.currentTarget.value as TriggerActionKind);
            }}
            value={actionKind()}
          >
            <option value="message">Notify me with this text</option>
            <option value="prompt">Run this as an agent prompt</option>
          </select>
          <span class="text-muted-foreground text-xs">
            {actionKind() === "prompt"
              ? "The agent runs your text when it fires; its answer arrives as a notification."
              : "Your text is delivered verbatim as a notification when it fires."}
          </span>
        </label>
        <Show when={recurrence() === "once"}>
          <TextField onChange={setFireAt} value={fireAt()}>
            <TextFieldLabel>Date and time</TextFieldLabel>
            <TextFieldInput
              min={localDateTimeStamp(new Date())}
              name="fire_at"
              step={1}
              type="datetime-local"
            />
          </TextField>
        </Show>
        <Show when={recurrence() !== "once"}>
          <TextField onChange={setTimeOfDay} value={timeOfDay()}>
            <TextFieldLabel>Time of day</TextFieldLabel>
            <TextFieldInput name="time_of_day" type="time" />
          </TextField>
          <TextField onChange={setTimezone} value={timezone()}>
            <TextFieldLabel>Time zone</TextFieldLabel>
            <TextFieldInput name="timezone" />
          </TextField>
        </Show>
        <Show when={recurrence() === "weekly"}>
          <label class="grid gap-1">
            <span class={fieldLabelClass}>Day of week</span>
            <select
              class={selectClass}
              name="weekday"
              onChange={(event) => {
                setWeekday(Number(event.currentTarget.value));
              }}
              value={weekday()}
            >
              <For each={WEEKDAYS}>
                {(day, index) => <option value={index()}>{day}</option>}
              </For>
            </select>
          </label>
        </Show>
        <div class="flex gap-2">
          <Button type="submit">
            {editing() === undefined ? "Add reminder" : "Save reminder"}
          </Button>
          <Show when={editing()}>
            <Button onClick={resetForm} type="button" variant="ghost">
              Cancel
            </Button>
          </Show>
        </div>
      </form>
      <Show when={error()}>
        {(message) => (
          <p class="text-destructive mt-2 text-sm" role="alert">
            {message()}
          </p>
        )}
      </Show>
      <ul class="mt-3 space-y-2">
        <For each={triggersQuery.data ?? []}>
          {(trigger) => (
            <li
              aria-label={`Reminder: ${trigger.payload}`}
              class="bg-muted flex flex-wrap items-center gap-1 rounded-md border px-3 py-2 text-sm"
            >
              <span class="font-medium">{trigger.payload}</span>
              <span class="text-muted-foreground text-xs">{` · ${trigger.recurrence} · ${trigger.status}`}</span>
              <span class="text-muted-foreground text-xs">{` · next ${formatFireTime(trigger.next_fire_at)}`}</span>
              <Button
                class="ml-auto"
                onClick={() => {
                  startEdit(trigger);
                }}
                size="sm"
                type="button"
                variant="ghost"
              >
                Edit
              </Button>
              <Button
                onClick={() => {
                  remove(trigger.id, trigger.version);
                }}
                size="sm"
                type="button"
                variant="ghost"
              >
                Delete
              </Button>
            </li>
          )}
        </For>
      </ul>
    </section>
  );
}
