import type { components } from "../generated";
import { requireData, type RestContext } from "./transport";

export type HealthOverview = components["schemas"]["HealthOverviewRead"];

export interface HealthHost {
  getOverview(days?: number): Promise<HealthOverview>;
}

export function createHealthHost(context: RestContext): HealthHost {
  return {
    async getOverview(days = 7) {
      const { data, response } = await context.client.GET(
        "/api/health/overview",
        { params: { query: { days } } },
      );
      return requireData(data, response);
    },
  };
}
