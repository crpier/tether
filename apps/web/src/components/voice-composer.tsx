// Chat composer voice controls (issue #19): two always-visible icon buttons
// ("record and review" fills the composer, "record and send" sends
// immediately) driving the `VoiceRecorder` toggle state machine. Whichever
// button starts a recording morphs in place into a "Stop" button — the other
// one hides — so recording is stopped from the same spot it was started, no
// separate stop control appears elsewhere. This component owns only the
// browser wiring (getUserMedia/MediaRecorder) and the
// recording/uploading/failed UI — the state machine itself lives in
// `voice-recorder.ts`, and what a successful transcript *does* (fill the
// draft vs. send) is entirely the caller's call via `onTranscript`.
import { Show, createEffect, createSignal, onCleanup, onMount } from "solid-js";

import type {
  MinimalMediaRecorder,
  VoiceMode,
  VoiceRecorderState,
} from "@/voice-recorder";
import { VoiceRecorder } from "@/voice-recorder";
import { Button } from "@/components/ui/button";

function elapsedLabel(startedAt: number, nowMs: number): string {
  const seconds = Math.max(0, Math.round((nowMs - startedAt) / 1000));
  return `${seconds.toString()}s`;
}

// `MediaRecorder`'s own `ondataavailable`/`onstop` setters expect the full
// DOM event types, which don't structurally match `MinimalMediaRecorder`'s
// narrow shape. Wrapping it keeps `voice-recorder.ts` decoupled from DOM
// event types entirely, rather than widening its interface to match them.
function adaptMediaRecorder(
  mediaRecorder: MediaRecorder,
): MinimalMediaRecorder {
  const adapted: MinimalMediaRecorder = {
    ondataavailable: null,
    onstop: null,
    start: () => {
      mediaRecorder.start();
    },
    stop: () => {
      mediaRecorder.stop();
    },
  };
  mediaRecorder.ondataavailable = (event) => {
    adapted.ondataavailable?.({ data: event.data });
  };
  mediaRecorder.onstop = () => {
    adapted.onstop?.();
  };
  return adapted;
}

export function VoiceComposerControls(props: {
  disabled: boolean;
  onTranscript: (transcript: string, mode: VoiceMode) => void;
  transcribe: (blob: Blob) => Promise<string>;
}) {
  const [state, setState] = createSignal<VoiceRecorderState>({ kind: "idle" });
  const [nowMs, setNowMs] = createSignal(Date.now());

  const recorder = new VoiceRecorder(
    {
      createRecorder: (stream) => adaptMediaRecorder(new MediaRecorder(stream)),
      getUserMedia: () => navigator.mediaDevices.getUserMedia({ audio: true }),
      stopStream: (stream) => {
        for (const track of stream.getTracks()) {
          track.stop();
        }
      },
      transcribe: props.transcribe,
    },
    setState,
    props.onTranscript,
  );

  // Ticks the elapsed-time label forward while recording; torn down the
  // instant recording stops so no interval leaks across state changes.
  createEffect(() => {
    if (state().kind !== "recording") {
      return;
    }
    const handle = window.setInterval(() => {
      setNowMs(Date.now());
    }, 1000);
    onCleanup(() => {
      window.clearInterval(handle);
    });
  });

  // Escape cancels an in-progress recording, mirroring the explicit "x"
  // control — a keyboard-only path to abandon a clip without uploading it.
  onMount(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && state().kind === "recording") {
        recorder.cancel();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    onCleanup(() => {
      window.removeEventListener("keydown", onKeyDown);
    });
  });

  const start = (mode: VoiceMode) => {
    void recorder.start(mode);
  };

  // Which mode (if any) is currently recording — drives which of the two
  // buttons morphs into "Stop" in place, rather than popping up a separate
  // stop control elsewhere.
  const recordingMode = () => {
    const current = state();
    return current.kind === "recording" ? current.mode : null;
  };

  return (
    <div aria-label="Voice input" class="flex flex-col gap-2" role="group">
      <Show when={state().kind === "idle" || state().kind === "recording"}>
        <div class="flex gap-2">
          <Show when={recordingMode() === null || recordingMode() === "review"}>
            <Button
              aria-label={
                recordingMode() === "review"
                  ? "Stop recording"
                  : "Record and review"
              }
              disabled={props.disabled}
              onClick={() => {
                if (recordingMode() === "review") {
                  recorder.stop();
                } else {
                  start("review");
                }
              }}
              size="sm"
              title={
                recordingMode() === "review"
                  ? "Stop recording"
                  : "Record and review"
              }
              type="button"
              variant="outline"
            >
              {recordingMode() === "review" ? "⏹" : "🎙 ✎"}
            </Button>
          </Show>
          <Show
            when={recordingMode() === null || recordingMode() === "auto-send"}
          >
            <Button
              aria-label={
                recordingMode() === "auto-send"
                  ? "Stop recording"
                  : "Record and send"
              }
              disabled={props.disabled}
              onClick={() => {
                if (recordingMode() === "auto-send") {
                  recorder.stop();
                } else {
                  start("auto-send");
                }
              }}
              size="sm"
              title={
                recordingMode() === "auto-send"
                  ? "Stop recording"
                  : "Record and send"
              }
              type="button"
              variant="outline"
            >
              {recordingMode() === "auto-send" ? "⏹" : "🎙 ➤"}
            </Button>
          </Show>
        </div>
      </Show>
      <Show when={state()} keyed>
        {(current) => (
          <Show when={current.kind === "recording" && current}>
            {(recording) => (
              <div
                class="bg-muted flex items-center gap-2 rounded-md border px-3 py-1.5 text-sm"
                role="status"
              >
                <span
                  aria-hidden="true"
                  class="inline-block size-2 animate-pulse rounded-full bg-red-500"
                />
                <span>Recording…</span>
                <span class="tabular-nums opacity-70">
                  {elapsedLabel(recording().startedAt, nowMs())}
                </span>
                <button
                  aria-label="Cancel recording"
                  class="text-muted-foreground ml-auto opacity-70 hover:opacity-100"
                  onClick={() => {
                    recorder.cancel();
                  }}
                  type="button"
                >
                  ✕
                </button>
              </div>
            )}
          </Show>
        )}
      </Show>
      <Show when={state().kind === "uploading"}>
        <p class="text-muted-foreground text-sm" role="status">
          Transcribing…
        </p>
      </Show>
      <Show when={state()} keyed>
        {(current) => (
          <Show when={current.kind === "failed" && current}>
            {(failed) => (
              <div
                class="border-destructive/40 bg-destructive/10 text-destructive flex items-center gap-2 rounded-md border px-3 py-2 text-sm"
                role="alert"
              >
                <p class="flex-1">{failed().message}</p>
                <Button
                  onClick={() => {
                    recorder.retry();
                  }}
                  size="sm"
                  type="button"
                  variant="outline"
                >
                  Retry
                </Button>
                <Button
                  onClick={() => {
                    recorder.discard();
                  }}
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  Discard
                </Button>
              </div>
            )}
          </Show>
        )}
      </Show>
    </div>
  );
}
