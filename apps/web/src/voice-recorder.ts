// Voice input recording controller (issue #19): a toggle recorder driving the
// chat composer's two voice buttons. Click starts recording, click again
// stops it and uploads the clip for transcription; an explicit cancel
// discards it mid-recording with no upload. Which button started the
// recording (`review` vs `auto-send`) is threaded through untouched so the
// caller can decide what a successful transcript does — fill the composer for
// editing, or send it immediately through the normal chat-send path — this
// module only owns the record/upload state machine, never the chat send.
//
// Depends on abstractions rather than `navigator.mediaDevices`/`MediaRecorder`
// directly so the state machine is unit-testable without a real browser
// microphone (see `voice-recorder.test.ts`); `chat-page.tsx` wires the real
// browser APIs as `VoiceRecorderDeps`.

export type VoiceMode = "auto-send" | "review";

export type VoiceRecorderState =
  | { kind: "failed"; message: string; mode: VoiceMode }
  | { kind: "idle" }
  | { kind: "recording"; mode: VoiceMode; startedAt: number }
  | { kind: "uploading"; mode: VoiceMode };

// The slice of `MediaRecorder` this module actually drives — small enough to
// fake in tests without a real browser recorder.
export interface MinimalMediaRecorder {
  ondataavailable: ((event: { data: Blob }) => void) | null;
  onstop: (() => void) | null;
  start(): void;
  stop(): void;
}

export interface VoiceRecorderDeps {
  createRecorder: (stream: MediaStream) => MinimalMediaRecorder;
  getUserMedia: () => Promise<MediaStream>;
  // Injectable clock so `startedAt`/elapsed-time tests don't depend on real
  // wall-clock time.
  now?: () => number;
  // Releases the microphone stream once recording stops (real deps stop each
  // track); optional so fakes without a real `MediaStream` can omit it.
  stopStream?: (stream: MediaStream) => void;
  transcribe: (blob: Blob) => Promise<string>;
}

export class VoiceRecorder {
  private blob: Blob | null = null;
  private chunks: Blob[] = [];
  private readonly now: () => number;
  private recorder: MinimalMediaRecorder | null = null;
  private state: VoiceRecorderState = { kind: "idle" };
  private stream: MediaStream | null = null;

  constructor(
    private readonly deps: VoiceRecorderDeps,
    private readonly onChange: (state: VoiceRecorderState) => void,
    private readonly onTranscript: (
      transcript: string,
      mode: VoiceMode,
    ) => void,
  ) {
    this.now = deps.now ?? (() => Date.now());
  }

  getState(): VoiceRecorderState {
    return this.state;
  }

  /** Start recording in the given mode (a no-op unless currently idle). */
  async start(mode: VoiceMode): Promise<void> {
    if (this.state.kind !== "idle") {
      return;
    }
    let stream: MediaStream;
    try {
      stream = await this.deps.getUserMedia();
    } catch {
      this.setState({
        kind: "failed",
        message: "Microphone access was denied.",
        mode,
      });
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
      this.finishRecording(mode);
    };
    recorder.start();
    this.setState({ kind: "recording", mode, startedAt: this.now() });
  }

  /** Stop recording and upload the clip for transcription. */
  stop(): void {
    if (this.state.kind !== "recording") {
      return;
    }
    this.recorder?.stop();
  }

  /** Abandon an in-progress recording; nothing is uploaded or kept. */
  cancel(): void {
    if (this.state.kind !== "recording") {
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
    if (this.state.kind !== "failed" || this.blob === null) {
      return;
    }
    void this.upload(this.state.mode, this.blob);
  }

  /** Discard the retained clip from a failed transcription. */
  discard(): void {
    if (this.state.kind !== "failed") {
      return;
    }
    this.blob = null;
    this.setState({ kind: "idle" });
  }

  private finishRecording(mode: VoiceMode): void {
    this.releaseStream();
    const blob = new Blob(this.chunks, { type: "audio/webm" });
    this.chunks = [];
    void this.upload(mode, blob);
  }

  private async upload(mode: VoiceMode, blob: Blob): Promise<void> {
    this.blob = blob;
    this.setState({ kind: "uploading", mode });
    try {
      const transcript = await this.deps.transcribe(blob);
      if (transcript.trim().length === 0) {
        this.setState({
          kind: "failed",
          message: "No speech was detected. Try again.",
          mode,
        });
        return;
      }
      this.blob = null;
      this.setState({ kind: "idle" });
      this.onTranscript(transcript, mode);
    } catch (error) {
      this.setState({
        kind: "failed",
        message:
          error instanceof Error && error.message.length > 0
            ? error.message
            : "Transcription failed.",
        mode,
      });
    }
  }

  private releaseStream(): void {
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
