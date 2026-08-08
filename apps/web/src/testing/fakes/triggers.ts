import type {
  CreateTrigger,
  Trigger,
  TriggersHost,
  UpdateTrigger,
} from "../../host/triggers";
import { ApiError } from "../../host/error";
import { trigger } from "../fixtures";

export class FakeTriggersHost implements TriggersHost {
  createTriggerCalls: CreateTrigger[] = [];
  updateTriggerCalls: { body: UpdateTrigger; triggerId: string }[] = [];
  deleteTriggerCalls: { triggerId: string; version: number }[] = [];
  serverTriggerVersions: Record<string, number> = {};
  serverTriggerEdits: Record<string, Partial<Trigger>> = {};
  updateTriggerRejections: ApiError[] = [];
  deleteTriggerRejections: ApiError[] = [];
  storedTriggers: Trigger[];

  constructor(triggers: Trigger[] = []) {
    this.storedTriggers = triggers;
  }

  listTriggers() {
    return Promise.resolve(this.storedTriggers);
  }

  createTrigger(body: CreateTrigger) {
    this.createTriggerCalls.push(body);
    const created = trigger({
      action_kind: body.action_kind,
      id: `018f0000-0000-7000-8000-0000000000${this.createTriggerCalls.length
        .toString()
        .padStart(2, "0")}`,
      payload: body.payload,
      recurrence: body.recurrence,
    });
    this.storedTriggers = [...this.storedTriggers, created];
    return Promise.resolve(created);
  }

  updateTrigger(triggerId: string, body: UpdateTrigger) {
    this.updateTriggerCalls.push({ body, triggerId });
    const forced = this.updateTriggerRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    const serverVersion = this.serverTriggerVersions[triggerId];
    if (
      Object.hasOwn(this.serverTriggerVersions, triggerId) &&
      serverVersion !== body.version
    ) {
      this.storedTriggers = this.storedTriggers.map((existing) =>
        existing.id === triggerId
          ? {
              ...existing,
              ...this.serverTriggerEdits[triggerId],
              version: serverVersion,
            }
          : existing,
      );
      return Promise.reject(new ApiError(409));
    }
    const current = this.storedTriggers.find(
      (existing) => existing.id === triggerId,
    );
    if (current === undefined) {
      return Promise.reject(new ApiError(404));
    }
    const updated: Trigger = {
      ...current,
      action_kind: body.action_kind,
      payload: body.payload,
      recurrence: body.recurrence,
      timezone: body.timezone ?? "UTC",
      version: body.version + 1,
      wall_time: body.time_of_day,
      weekday: body.weekday,
    };
    this.serverTriggerVersions[triggerId] = updated.version;
    this.storedTriggers = this.storedTriggers.map((existing) =>
      existing.id === triggerId ? updated : existing,
    );
    return Promise.resolve(updated);
  }

  deleteTrigger(triggerId: string, version: number) {
    this.deleteTriggerCalls.push({ triggerId, version });
    const forced = this.deleteTriggerRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    const serverVersion = this.serverTriggerVersions[triggerId];
    if (
      Object.hasOwn(this.serverTriggerVersions, triggerId) &&
      serverVersion !== version
    ) {
      this.storedTriggers = this.storedTriggers.map((existing) =>
        existing.id === triggerId
          ? { ...existing, status: "completed", version: serverVersion }
          : existing,
      );
      return Promise.reject(new ApiError(409));
    }
    this.storedTriggers = this.storedTriggers.filter(
      (existing) => existing.id !== triggerId,
    );
    return Promise.resolve();
  }
}
