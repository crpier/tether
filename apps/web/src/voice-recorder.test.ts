import { describe, expect, test, vi } from "vitest";

import type {
  MinimalMediaRecorder,
  VoiceRecorderState,
} from "./voice-recorder";
import { VoiceRecorder } from "./voice-recorder";

// A scripted fake standing in for `MediaRecorder` — just enough surface for
// the controller to drive (start/stop, ondataavailable, onstop) without a
// real browser recorder.
class FakeRecorder implements MinimalMediaRecorder {
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  started = false;
  stopped = false;

  start(): void {
    this.started = true;
  }

  stop(): void {
    this.stopped = true;
    this.ondataavailable?.({ data: new Blob(["chunk"]) });
    this.onstop?.();
  }
}

function harness(options: {
  getUserMedia?: () => Promise<MediaStream>;
  now?: () => number;
  transcribe?: (blob: Blob) => Promise<string>;
}) {
  const states: VoiceRecorderState[] = [];
  const transcripts: { mode: string; transcript: string }[] = [];
  const stopStreamCalls: MediaStream[] = [];
  const recorders: FakeRecorder[] = [];
  const fakeStream = {} as MediaStream;
  const recorder = new VoiceRecorder(
    {
      createRecorder: () => {
        const created = new FakeRecorder();
        recorders.push(created);
        return created;
      },
      getUserMedia: options.getUserMedia ?? (() => Promise.resolve(fakeStream)),
      now: options.now,
      stopStream: (stream) => {
        stopStreamCalls.push(stream);
      },
      transcribe: options.transcribe ?? (() => Promise.resolve("hello")),
    },
    (state) => {
      states.push(state);
    },
    (transcript, mode) => {
      transcripts.push({ mode, transcript });
    },
  );
  return { recorder, recorders, states, stopStreamCalls, transcripts };
}

describe("VoiceRecorder toggle state machine", () => {
  test("idle -> recording -> uploading -> idle on a successful transcript", async () => {
    const { recorder, states, transcripts } = harness({
      transcribe: () => Promise.resolve("buy oat milk"),
    });

    expect(recorder.getState()).toEqual({ kind: "idle" });

    await recorder.start("review");
    expect(recorder.getState().kind).toBe("recording");

    recorder.stop();
    // The fake recorder's stop() synchronously fires ondataavailable+onstop,
    // which kicks off the (async) upload — flush microtasks.
    await Promise.resolve();
    await Promise.resolve();

    expect(recorder.getState()).toEqual({ kind: "idle" });
    expect(transcripts).toEqual([
      { mode: "review", transcript: "buy oat milk" },
    ]);
    expect(states.map((state) => state.kind)).toEqual([
      "recording",
      "uploading",
      "idle",
    ]);
  });

  test("which button started the recording decides the mode reported on success", async () => {
    const { recorder, transcripts } = harness({
      transcribe: () => Promise.resolve("call the dentist"),
    });

    await recorder.start("auto-send");
    recorder.stop();
    await Promise.resolve();
    await Promise.resolve();

    expect(transcripts).toEqual([
      { mode: "auto-send", transcript: "call the dentist" },
    ]);
  });

  test("elapsed time is derived from the injected clock at recording start", async () => {
    const { recorder } = harness({ now: () => 1_000 });

    await recorder.start("review");

    const state = recorder.getState();
    expect(state.kind).toBe("recording");
    if (state.kind === "recording") {
      expect(state.startedAt).toBe(1_000);
    }
  });

  test("cancel mid-recording discards the clip and never uploads", async () => {
    const { recorder, states, transcripts } = harness({
      transcribe: () => Promise.reject(new Error("should not be called")),
    });

    await recorder.start("review");
    recorder.cancel();

    expect(recorder.getState()).toEqual({ kind: "idle" });
    expect(transcripts).toEqual([]);
    // No "uploading" state ever appears — cancel suppresses the upload path.
    expect(states.map((state) => state.kind)).toEqual(["recording", "idle"]);
  });

  test("a denied microphone surfaces as a failed state, not a thrown error", async () => {
    const { recorder } = harness({
      getUserMedia: () => Promise.reject(new Error("denied")),
    });

    await recorder.start("review");

    expect(recorder.getState()).toEqual({
      kind: "failed",
      message: "Microphone access was denied.",
      mode: "review",
    });
  });

  test("an empty transcript fails without entering chat, keeping the clip", async () => {
    const { recorder, transcripts } = harness({
      transcribe: () => Promise.resolve("   "),
    });

    await recorder.start("auto-send");
    recorder.stop();
    await Promise.resolve();
    await Promise.resolve();

    expect(recorder.getState()).toEqual({
      kind: "failed",
      message: "No speech was detected. Try again.",
      mode: "auto-send",
    });
    expect(transcripts).toEqual([]);
  });

  test("a transcription failure fails with the upstream error message", async () => {
    const { recorder } = harness({
      transcribe: () => Promise.reject(new Error("Transcription failed.")),
    });

    await recorder.start("review");
    recorder.stop();
    await Promise.resolve();
    await Promise.resolve();

    expect(recorder.getState()).toEqual({
      kind: "failed",
      message: "Transcription failed.",
      mode: "review",
    });
  });

  test("retry re-uploads the same retained clip and can then succeed", async () => {
    const transcribe = vi
      .fn<(blob: Blob) => Promise<string>>()
      .mockRejectedValueOnce(new Error("Transcription failed."))
      .mockResolvedValueOnce("buy oat milk");
    const { recorder, transcripts } = harness({ transcribe });

    await recorder.start("review");
    recorder.stop();
    await Promise.resolve();
    await Promise.resolve();
    expect(recorder.getState().kind).toBe("failed");

    recorder.retry();
    await Promise.resolve();
    await Promise.resolve();

    expect(recorder.getState()).toEqual({ kind: "idle" });
    expect(transcripts).toEqual([
      { mode: "review", transcript: "buy oat milk" },
    ]);
    expect(transcribe).toHaveBeenCalledTimes(2);
    // Both calls re-upload the exact same blob instance retained client-side.
    const [firstBlob] = transcribe.mock.calls[0] ?? [];
    const [secondBlob] = transcribe.mock.calls[1] ?? [];
    expect(firstBlob).toBe(secondBlob);
  });

  test("discard drops the retained clip and returns to idle", async () => {
    const { recorder } = harness({
      transcribe: () => Promise.reject(new Error("Transcription failed.")),
    });

    await recorder.start("review");
    recorder.stop();
    await Promise.resolve();
    await Promise.resolve();
    expect(recorder.getState().kind).toBe("failed");

    recorder.discard();

    expect(recorder.getState()).toEqual({ kind: "idle" });
    // Retry after a discard is a no-op — there is nothing left to re-upload.
    recorder.retry();
    expect(recorder.getState()).toEqual({ kind: "idle" });
  });

  test("the microphone stream is released once recording stops", async () => {
    const { recorder, stopStreamCalls } = harness({});

    await recorder.start("review");
    recorder.stop();
    await Promise.resolve();
    await Promise.resolve();

    expect(stopStreamCalls).toHaveLength(1);
  });

  test("start is a no-op while already recording or uploading", async () => {
    const { recorder, recorders } = harness({
      // Never resolves — start() must ignore the second call outright rather
      // than racing a real upload.
      transcribe: () => new Promise<string>(() => undefined),
    });

    await recorder.start("review");
    await recorder.start("auto-send");
    expect(recorders).toHaveLength(1);
    const state = recorder.getState();
    expect(state.kind).toBe("recording");
    if (state.kind === "recording") {
      expect(state.mode).toBe("review");
    }
  });
});
