import type {
  TranscriptDecision,
  TranscriptDecisionOutcome,
  YouTubeHost,
  YouTubeSyncStatus,
} from "../../host/youtube";

export class FakeYouTubeHost implements YouTubeHost {
  youTubeSyncStatus: YouTubeSyncStatus = {
    api_paused_until: null,
    last_synced_at: null,
    quota: { limit: 10000, remaining: 10000, used: 0 },
    usage: {},
    transcript_providers_paused: [],
    transcripts_done: 0,
    transcripts_needs_review: 0,
    transcripts_pending: 0,
    transcripts_unavailable: 0,
    videos_total: 0,
  };
  storedTranscriptDecisions: TranscriptDecision[];
  keepTryingTranscriptCalls: string[] = [];
  giveUpTranscriptCalls: string[] = [];

  constructor(transcriptDecisions: TranscriptDecision[] = []) {
    this.storedTranscriptDecisions = transcriptDecisions;
  }

  getYouTubeSyncStatus(): Promise<YouTubeSyncStatus> {
    return Promise.resolve(this.youTubeSyncStatus);
  }

  listTranscriptDecisions(): Promise<TranscriptDecision[]> {
    return Promise.resolve(this.storedTranscriptDecisions);
  }

  keepTryingTranscript(videoId: string): Promise<TranscriptDecisionOutcome> {
    this.keepTryingTranscriptCalls.push(videoId);
    this.storedTranscriptDecisions = this.storedTranscriptDecisions.filter(
      (decision) => decision.video_id !== videoId,
    );
    return Promise.resolve({ transcript_status: "pending", video_id: videoId });
  }

  giveUpTranscript(videoId: string): Promise<TranscriptDecisionOutcome> {
    this.giveUpTranscriptCalls.push(videoId);
    this.storedTranscriptDecisions = this.storedTranscriptDecisions.filter(
      (decision) => decision.video_id !== videoId,
    );
    return Promise.resolve({
      transcript_status: "unavailable",
      video_id: videoId,
    });
  }
}
