import type { components } from "../generated";
import { requireData, type RestContext } from "./transport";

export type GmailAuthStatus = components["schemas"]["GmailAuthStatus"];

export interface GmailHost {
  getGmailAuthStatus(): Promise<GmailAuthStatus>;
  startGmailAuth(): Promise<GmailAuthStatus>;
}

export function createGmailHost(context: RestContext): GmailHost {
  return {
    async getGmailAuthStatus() {
      const { data, response } = await context.client.GET("/api/gmail-auth");
      return requireData(data, response);
    },
    async startGmailAuth() {
      const { data, response } = await context.client.POST("/api/gmail-auth");
      return requireData(data, response);
    },
  };
}
