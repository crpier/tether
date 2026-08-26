import type { components } from "../generated";
import { requireData, type RestContext } from "./transport";

export type Artifact = components["schemas"]["ArtifactRead"];
export type ArtifactEvent = components["schemas"]["ArtifactEventRead"];

export interface ArtifactsHost {
  getArtifact(artifactId: string): Promise<Artifact>;
  postArtifactEvent(
    artifactId: string,
    payload: Record<string, unknown>,
  ): Promise<ArtifactEvent>;
}

export function createArtifactsHost(context: RestContext): ArtifactsHost {
  return {
    async getArtifact(artifactId) {
      const { data, response } = await context.client.GET(
        "/api/artifacts/{artifact_id}",
        { params: { path: { artifact_id: artifactId } } },
      );
      return requireData(data, response);
    },
    async postArtifactEvent(artifactId, payload) {
      const { data, response } = await context.client.POST(
        "/api/artifacts/{artifact_id}/events",
        {
          body: { payload },
          params: { path: { artifact_id: artifactId } },
        },
      );
      return requireData(data, response);
    },
  };
}
