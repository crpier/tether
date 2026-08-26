import type { components } from "../generated";
import { requireData, type RestContext } from "./transport";

export type ProviderAuthStatus = components["schemas"]["ProviderAuthRead"];

export interface ProviderAuthHost {
  getProviderAuthStatus(): Promise<ProviderAuthStatus>;
  startProviderAuth(): Promise<ProviderAuthStatus>;
  cancelProviderAuth(): Promise<ProviderAuthStatus>;
}

export function createProviderAuthHost(context: RestContext): ProviderAuthHost {
  return {
    async getProviderAuthStatus() {
      const { data, response } = await context.client.GET(
        "/api/provider-auth/openai-codex",
      );
      return requireData(data, response);
    },
    async startProviderAuth() {
      const { data, response } = await context.client.POST(
        "/api/provider-auth/openai-codex",
      );
      return requireData(data, response, {
        409: "Provider authorization is already active.",
      });
    },
    async cancelProviderAuth() {
      const { data, response } = await context.client.DELETE(
        "/api/provider-auth/openai-codex",
      );
      return requireData(data, response);
    },
  };
}
