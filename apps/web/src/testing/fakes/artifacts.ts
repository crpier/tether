import type {
  Artifact,
  ArtifactEvent,
  ArtifactsHost,
} from "../../host/artifacts";
import { ApiError } from "../../host/error";

export class FakeArtifactsHost implements ArtifactsHost {
  storedArtifacts: Artifact[] = [];
  getArtifactCalls: string[] = [];
  getArtifactRejections: ApiError[] = [];
  postArtifactEventCalls: { artifactId: string; payload: unknown }[] = [];
  postArtifactEventRejections: ApiError[] = [];

  getArtifact(artifactId: string): Promise<Artifact> {
    this.getArtifactCalls.push(artifactId);
    const forced = this.getArtifactRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    const found = this.storedArtifacts.find(
      (candidate) => candidate.id === artifactId,
    );
    if (found === undefined) {
      return Promise.reject(new ApiError(404));
    }
    return Promise.resolve(found);
  }

  postArtifactEvent(
    artifactId: string,
    payload: Record<string, unknown>,
  ): Promise<ArtifactEvent> {
    this.postArtifactEventCalls.push({ artifactId, payload });
    const forced = this.postArtifactEventRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    return Promise.resolve({
      artifact_id: artifactId,
      created_at: "2026-01-02T00:00:00Z",
      id: `018f0000-0000-7000-8000-0000000004${this.postArtifactEventCalls.length
        .toString()
        .padStart(2, "0")}`,
      payload,
    });
  }
}
