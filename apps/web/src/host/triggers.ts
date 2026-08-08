import type { components } from "../generated";
import { requireData, requireOk, type RestContext } from "./transport";

export type Trigger = components["schemas"]["TriggerRead"];
export type CreateTrigger = components["schemas"]["CreateTriggerRequest"];
export type UpdateTrigger = components["schemas"]["UpdateTriggerRequest"];
export type TriggerRecurrence = components["schemas"]["TriggerRecurrence"];
export type TriggerActionKind = components["schemas"]["TriggerActionKind"];

export interface TriggersHost {
  listTriggers(): Promise<Trigger[]>;
  createTrigger(body: CreateTrigger): Promise<Trigger>;
  updateTrigger(triggerId: string, body: UpdateTrigger): Promise<Trigger>;
  deleteTrigger(triggerId: string, version: number): Promise<void>;
}

export function createTriggersHost(context: RestContext): TriggersHost {
  return {
    async listTriggers() {
      const { data, response } = await context.client.GET("/api/triggers");
      return requireData(data, response);
    },
    async createTrigger(body) {
      const { data, response } = await context.client.POST("/api/triggers", {
        body,
      });
      return requireData(data, response);
    },
    async updateTrigger(triggerId, body) {
      const { data, response } = await context.client.PUT(
        "/api/triggers/{trigger_id}",
        { body, params: { path: { trigger_id: triggerId } } },
      );
      return requireData(data, response);
    },
    async deleteTrigger(triggerId, version) {
      const { response } = await context.client.DELETE(
        "/api/triggers/{trigger_id}",
        {
          params: { path: { trigger_id: triggerId }, query: { version } },
        },
      );
      requireOk(response);
    },
  };
}
