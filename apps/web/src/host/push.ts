import type { components } from "../generated";
import { requireData, requireOk, type RestContext } from "./transport";

export type PushConfig = components["schemas"]["PushConfigRead"];
export type PushStatus = components["schemas"]["PushStatusRead"];

export interface PushHost {
  getPushConfig(): Promise<PushConfig>;
  getPushStatus(endpoint: string): Promise<PushStatus>;
  subscribePush(endpoint: string, p256dh: string, auth: string): Promise<void>;
  unsubscribePush(endpoint: string): Promise<PushStatus>;
}

export function createPushHost(context: RestContext): PushHost {
  return {
    async getPushConfig() {
      const { data, response } = await context.client.GET("/api/push/config");
      return requireData(data, response);
    },
    async getPushStatus(endpoint) {
      const { data, response } = await context.client.GET("/api/push/status", {
        params: { query: { endpoint } },
      });
      return requireData(data, response);
    },
    async subscribePush(endpoint, p256dh, auth) {
      const { response } = await context.client.POST(
        "/api/push/subscriptions",
        { body: { endpoint, p256dh, auth } },
      );
      requireOk(response);
    },
    async unsubscribePush(endpoint) {
      const { data, response } = await context.client.DELETE(
        "/api/push/subscriptions",
        { body: { endpoint } },
      );
      return requireData(data, response);
    },
  };
}
