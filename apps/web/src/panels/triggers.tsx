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

// A `datetime-local` value is a local (not UTC) `YYYY-MM-DDTHH:MM` stamp, so the
// `min` guard has to be built from local components rather than `toISOString()`.
function localDateTimeStamp(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  const year = String(date.getFullYear()).padStart(4, "0");
  const month = pad(date.getMonth() + 1);
  const day = pad(date.getDate());
  const hours = pad(date.getHours());
  const minutes = pad(date.getMinutes());
  return `${year}-${month}-${day}T${hours}:${minutes}`;
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

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.triggers });
    void queryClient.refetchQueries({ queryKey: queryKeys.triggers });
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
    void (async () => {
      setError(undefined);
      try {
        await props.api.createTrigger(body);
        setPayload("");
        setFireAt("");
        refresh();
      } catch (caught) {
        setError(
          caught instanceof Error
            ? caught.message
            : "Could not create reminder",
        );
      }
    })();
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
          const recovered = await retryDeleteWithFreshVersion(triggerId);
          if (recovered) {
            return;
          }
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
  // whether the reminder is now gone (deleted here, or already absent server-side).
  const retryDeleteWithFreshVersion = async (
    triggerId: string,
  ): Promise<boolean> => {
    await queryClient.refetchQueries({ queryKey: queryKeys.triggers });
    const fresh = (
      queryClient.getQueryData<Trigger[]>(queryKeys.triggers) ?? []
    ).find((candidate) => candidate.id === triggerId);
    if (fresh === undefined) {
      refresh();
      return true;
    }
    try {
      await props.api.deleteTrigger(triggerId, fresh.version);
      refresh();
      return true;
    } catch {
      return false;
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
        <Button type="submit">Add reminder</Button>
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
