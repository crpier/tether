import type { components } from "../generated";
import { requireData, type RestContext } from "./transport";

export type DreamRun = components["schemas"]["DreamRunRead"];
export type DreamRunDetail = components["schemas"]["DreamRunDetailRead"];
export type DreamingMutation = components["schemas"]["DreamingMutationRead"];

export interface DreamingHost {
  listDreamRuns(): Promise<DreamRun[]>;
  getDreamRun(runId: string): Promise<DreamRunDetail>;
}

export function createDreamingHost(context: RestContext): DreamingHost {
  return {
    async listDreamRuns() {
      const { data, response } = await context.client.GET("/api/dream-runs");
      return requireData(data, response);
    },
    async getDreamRun(runId) {
      const { data, response } = await context.client.GET(
        "/api/dream-runs/{run_id}",
        { params: { path: { run_id: runId } } },
      );
      return requireData(data, response);
    },
  };
}
