import type { components } from "../generated";
import { requireData, requireOk, type RestContext } from "./transport";

export type Session = components["schemas"]["SessionResponse"];

export interface AuthHost {
  getSession(): Promise<Session>;
  login(password: string): Promise<void>;
  logout(): Promise<void>;
}

export function createAuthHost(context: RestContext): AuthHost {
  return {
    async getSession() {
      const { data, response } = await context.client.GET("/api/auth/session");
      return requireData(data, response);
    },
    async login(password) {
      const { response } = await context.client.POST("/api/auth/login", {
        body: { password },
      });
      requireOk(response, { 401: "Incorrect password." });
    },
    async logout() {
      const { response } = await context.client.POST("/api/auth/logout");
      requireOk(response);
    },
  };
}
