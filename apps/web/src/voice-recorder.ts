// Voice input recording controller: starts, stops, or abandons hands-free
// microphone capture and uploads completed clips for transcription. This
// module owns recording/upload state, never chat sending.
//
// Depends on abstractions rather than `navigator.mediaDevices`/`MediaRecorder`
// directly so the state machine is unit-testable without a real browser
// microphone (see `voice-recorder.test.ts`); `chat-page.tsx` wires the real
// browser APIs as `VoiceRecorderDeps`.

export type VoiceRecorderState =
  | { kind: "failed"; message: string }
  | { kind: "idle" }
  | { kind: "recording"; startedAt: number }
  | { kind: "uploading" };

// The slice of `MediaRecorder` this module actually drives — small enough to
// fake in tests without a real browser recorder.
export interface MinimalMediaRecorder {
  ondataavailable: ((event: { data: Blob }) => void) | null;
  onstop: (() => void) | null;
  start(): void;
  stop(): void;
}

export interface VoiceRecorderDeps {
  /** Optional local feedback to finish after permission but before capture. */
  beforeRecordingStart?: () => Promise<void>;
  createRecorder: (stream: MediaStream) => MinimalMediaRecorder;
  getUserMedia: () => Promise<MediaStream>;
  // Injectable clock so `startedAt`/elapsed-time tests don't depend on real
  // wall-clock time.
  now?: () => number;
  // Releases the microphone stream once recording stops (real deps stop each
  // track); optional so fakes without a real `MediaStream` can omit it.
  stopStream?: (stream: MediaStream) => void;
  transcribe: (blob: Blob) => Promise<string>;
  /** Watch one stream until speech ends; returns a watcher cleanup. */
  watchForSpeechEnd: (
    stream: MediaStream,
    onSpeechEnd: () => void,
  ) => () => void;
}

export class VoiceRecorder {
  private blob: Blob | null = null;
  private chunks: Blob[] = [];
  private readonly now: () => number;
  private recorder: MinimalMediaRecorder | null = null;
  private state: VoiceRecorderState = { kind: "idle" };
  private startToken = 0;
  private stream: MediaStream | null = null;
  private stopWatchingSpeech: (() => void) | null = null;
  private uploadToken = 0;

  constructor(
    private readonly deps: VoiceRecorderDeps,
    private readonly onChange: (state: VoiceRecorderState) => void,
    private readonly onTranscript: (transcript: string) => void,
  ) {
    this.now = deps.now ?? (() => Date.now());
  }

  getState(): VoiceRecorderState {
    return this.state;
  }

  /** Start hands-free recording (a no-op unless currently idle). */
  async start(): Promise<void> {
    if (this.state.kind !== "idle") {
      return;
    }
    const startToken = ++this.startToken;
    let stream: MediaStream;
    try {
      stream = await this.deps.getUserMedia();
    } catch {
      if (startToken === this.startToken) {
        this.setState({
          kind: "failed",
          message: "Microphone access was denied.",
        });
      }
      return;
    }
    if (startToken !== this.startToken) {
      this.deps.stopStream?.(stream);
      return;
    }
    try {
      await this.deps.beforeRecordingStart?.();
    } catch {
      // Optional feedback must never block microphone capture.
    }
    if (startToken !== this.startToken) {
      this.deps.stopStream?.(stream);
      return;
    }
    this.stream = stream;
    this.chunks = [];
    const recorder = this.deps.createRecorder(stream);
    this.recorder = recorder;
    recorder.ondataavailable = (event) => {
      if (event.data.size > 0) {
        this.chunks.push(event.data);
      }
    };
    recorder.onstop = () => {
      this.finishRecording();
    };
    recorder.start();
    this.setState({ kind: "recording", startedAt: this.now() });
    this.stopWatchingSpeech = this.deps.watchForSpeechEnd(stream, () => {
      this.stop();
    });
  }

  /** Stop recording and upload the clip for transcription. */
  stop(): void {
    if (this.state.kind !== "recording") {
      return;
    }
    this.stopSpeechWatcher();
    this.recorder?.stop();
  }

  /** Abandon an in-progress recording; nothing is uploaded or kept. */
  cancel(): void {
    // Also invalidates a pending getUserMedia request. If it resolves after
    // cancellation, start() releases that stream without opening a recorder.
    this.startToken += 1;
    this.uploadToken += 1;
    if (this.state.kind !== "recording") {
      if (this.state.kind !== "idle") {
        this.blob = null;
        this.setState({ kind: "idle" });
      }
      return;
    }
    if (this.recorder) {
      // Suppress the upload path `onstop` would otherwise trigger.
      this.recorder.onstop = null;
      this.recorder.stop();
    }
    this.releaseStream();
    this.chunks = [];
    this.blob = null;
    this.setState({ kind: "idle" });
  }

  /** Re-upload the retained clip from a failed transcription. */
  retry(): void {
    if (this.state.kind !== "failed") {
      return;
    }
    if (this.blob === null) {
      this.setState({ kind: "idle" });
      void this.start();
      return;
    }
    void this.upload(this.blob);
  }

  /** Discard the retained clip from a failed transcription. */
  discard(): void {
    if (this.state.kind !== "failed") {
      return;
    }
    this.blob = null;
    this.setState({ kind: "idle" });
  }

  private finishRecording(): void {
    this.releaseStream();
    const blob = new Blob(this.chunks, { type: "audio/webm" });
    this.chunks = [];
    void this.upload(blob);
  }

  private async upload(blob: Blob): Promise<void> {
    const uploadToken = ++this.uploadToken;
    this.blob = blob;
    this.setState({ kind: "uploading" });
    try {
      const transcript = await this.deps.transcribe(blob);
      if (uploadToken !== this.uploadToken) {
        return;
      }
      if (transcript.trim().length === 0) {
        this.setState({
          kind: "failed",
          message: "No speech was detected. Try again.",
        });
        return;
      }
      this.blob = null;
      this.setState({ kind: "idle" });
      this.onTranscript(transcript);
    } catch (error) {
      if (uploadToken !== this.uploadToken) {
        return;
      }
      this.setState({
        kind: "failed",
        message:
          error instanceof Error && error.message.length > 0
            ? error.message
            : "Transcription failed.",
      });
    }
  }

  private stopSpeechWatcher(): void {
    this.stopWatchingSpeech?.();
    this.stopWatchingSpeech = null;
  }

  private releaseStream(): void {
    this.stopSpeechWatcher();
    if (this.stream) {
      this.deps.stopStream?.(this.stream);
      this.stream = null;
    }
    this.recorder = null;
  }

  private setState(state: VoiceRecorderState): void {
    this.state = state;
    this.onChange(state);
  }
}
