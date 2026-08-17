import type {
  TranscriptDecision,
  TranscriptDecisionOutcome,
  YouTubeAuthStatus,
  YouTubeHost,
  YouTubeSyncStatus,
} from "../../host/youtube";

export class FakeYouTubeHost implements YouTubeHost {
  youTubeAuthStatus: YouTubeAuthStatus = {
    authorization_url: null,
    error: null,
    state: "connected",
  };
  nextYouTubeAuthStatus: YouTubeAuthStatus | null = null;
  startYouTubeAuthCalls = 0;
  youTubeSyncStatus: YouTubeSyncStatus = {
    api_paused_until: null,
    last_synced_at: null,
    quota: { limit: 10000, remaining: 10000, used: 0 },
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

  getYouTubeAuthStatus(): Promise<YouTubeAuthStatus> {
    return Promise.resolve(this.youTubeAuthStatus);
  }

  startYouTubeAuth(): Promise<YouTubeAuthStatus> {
    this.startYouTubeAuthCalls += 1;
    if (this.nextYouTubeAuthStatus !== null) {
      this.youTubeAuthStatus = this.nextYouTubeAuthStatus;
      this.nextYouTubeAuthStatus = null;
    }
    return Promise.resolve(this.youTubeAuthStatus);
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
