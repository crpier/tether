import type { components } from "../generated";
import { requireData, type RestContext } from "./transport";

export type YouTubeSyncStatus = components["schemas"]["YouTubeSyncStatusRead"];
export type TranscriptDecision =
  components["schemas"]["TranscriptDecisionRead"];
export type TranscriptDecisionOutcome =
  components["schemas"]["TranscriptDecisionOutcomeRead"];

export interface YouTubeHost {
  getYouTubeSyncStatus(): Promise<YouTubeSyncStatus>;
  listTranscriptDecisions(): Promise<TranscriptDecision[]>;
  keepTryingTranscript(videoId: string): Promise<TranscriptDecisionOutcome>;
  giveUpTranscript(videoId: string): Promise<TranscriptDecisionOutcome>;
}

export function createYouTubeHost(context: RestContext): YouTubeHost {
  return {
    async getYouTubeSyncStatus() {
      const { data, response } = await context.client.GET(
        "/api/youtube/status",
      );
      return requireData(data, response);
    },
    async listTranscriptDecisions() {
      const { data, response } = await context.client.GET(
        "/api/youtube/transcript-decisions",
      );
      return requireData(data, response);
    },
    async keepTryingTranscript(videoId) {
      const { data, response } = await context.client.POST(
        "/api/youtube/{video_id}/transcript-decision/keep-trying",
        { params: { path: { video_id: videoId } } },
      );
      return requireData(data, response);
    },
    async giveUpTranscript(videoId) {
      const { data, response } = await context.client.POST(
        "/api/youtube/{video_id}/transcript-decision/give-up",
        { params: { path: { video_id: videoId } } },
      );
      return requireData(data, response);
    },
  };
}
