import type { components } from "../generated";
import { requireData, type RestContext } from "./transport";

export type Evidence =
  | components["schemas"]["ExerciseEvidenceRead"]
  | components["schemas"]["MessageEvidenceRead"]
  | components["schemas"]["SleepEvidenceRead"];

export interface EvidenceHost {
  resolveEvidence(uri: string): Promise<Evidence>;
}

export function createEvidenceHost(context: RestContext): EvidenceHost {
  return {
    async resolveEvidence(uri) {
      const { data, response } = await context.client.GET("/api/evidence", {
        params: { query: { uri } },
      });
      return requireData(data, response);
    },
  };
}
