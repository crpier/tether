// The chat composer's single voice-conversation control. It owns browser
// microphone wiring and recording/transcription status while the page owns
// the wider spoken loop. Starting it arms a hands-free recording immediately;
// ending it abandons the current clip and returns the composer to text.
import {
  Show,
  createEffect,
  createSignal,
  lazy,
  onCleanup,
  onMount,
  type Accessor,
} from "solid-js";

import type { SpeechPlayerState } from "@/speech-player";
import type {
  MinimalMediaRecorder,
  VoiceRecorderState,
} from "@/voice-recorder";
import { VoiceRecorder } from "@/voice-recorder";
import { watchForSpeechEnd } from "@/speech-end-watcher";
import { Button } from "@/components/ui/button";

const VoiceVisualizer = lazy(() =>
  import("./voice-visualizer").then((module) => ({
    default: module.VoiceVisualizer,
  })),
);

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
  active: Accessor<boolean>;
  /** Incremented for the first recording and each hands-free re-arm. */
  autoStartSignal: Accessor<number>;
  onEndConversation: () => void;
  onRecordingStart: () => Promise<void>;
  onRecordingStop: () => void;
  onStartConversation: () => void;
  playbackState: Accessor<SpeechPlayerState>;
  recordingCancelSignal: Accessor<number>;
  onTranscript: (transcript: string) => void;
  transcribe: (blob: Blob) => Promise<string>;
}) {
  const [state, setState] = createSignal<VoiceRecorderState>({ kind: "idle" });
  const [nowMs, setNowMs] = createSignal(Date.now());

  const recorder = new VoiceRecorder(
    {
      beforeRecordingStart: props.onRecordingStart,
      createRecorder: (stream) => adaptMediaRecorder(new MediaRecorder(stream)),
      getUserMedia: () => navigator.mediaDevices.getUserMedia({ audio: true }),
      stopStream: (stream) => {
        for (const track of stream.getTracks()) {
          track.stop();
        }
      },
      transcribe: props.transcribe,
      watchForSpeechEnd,
    },
    setState,
    props.onTranscript,
  );
  onCleanup(() => {
    recorder.cancel();
  });

  // Emit the stop transition after MediaRecorder has stopped, so its cue can
  // never leak into the clip being transcribed.
  let previousStateKind: VoiceRecorderState["kind"] = "idle";
  createEffect(() => {
    const currentStateKind = state().kind;
    if (previousStateKind === "recording" && currentStateKind !== "recording") {
      props.onRecordingStop();
    }
    previousStateKind = currentStateKind;
  });

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

  // Escape is the keyboard path for leaving a listening conversation.
  onMount(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && props.active()) {
        props.onEndConversation();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    onCleanup(() => {
      window.removeEventListener("keydown", onKeyDown);
    });
  });

  const start = () => {
    void recorder.start();
  };

  // Each increment arms one recording. Increments while recording are ignored;
  // the spoken loop can only ever own one microphone session.
  let handledAutoStart = 0;
  createEffect(() => {
    const tick = props.autoStartSignal();
    if (tick <= handledAutoStart || state().kind !== "idle") {
      return;
    }
    handledAutoStart = tick;
    start();
  });

  // A typed prompt takes over the turn without ending voice conversation.
  let handledRecordingCancel = 0;
  createEffect(() => {
    const tick = props.recordingCancelSignal();
    if (tick <= handledRecordingCancel) {
      return;
    }
    handledRecordingCancel = tick;
    recorder.cancel();
  });

  // Ending the wider conversation abandons any clip still being captured.
  createEffect(() => {
    if (!props.active()) {
      recorder.cancel();
      recorder.discard();
    }
  });

  return (
    <div aria-label="Voice input" class="relative flex shrink-0" role="group">
      <Button
        aria-label={
          props.active() ? "End voice conversation" : "Start voice conversation"
        }
        aria-pressed={props.active()}
        class="rounded-full"
        onClick={() => {
          if (props.active()) {
            props.onEndConversation();
          } else {
            props.onStartConversation();
          }
        }}
        size="icon-sm"
        title={
          props.active() ? "End voice conversation" : "Start voice conversation"
        }
        type="button"
        variant={props.active() ? "default" : "outline"}
      >
        <span aria-hidden="true">{props.active() ? "■" : "🎙"}</span>
      </Button>
      <Show when={state()} keyed>
        {(current) => (
          <Show when={current.kind === "recording" && current}>
            {(recording) => (
              <div
                class="bg-muted absolute right-0 bottom-full z-10 mb-2 flex w-max max-w-[calc(100vw-2rem)] items-center gap-2 rounded-md border px-3 py-1.5 text-sm shadow-sm"
                role="status"
              >
                <VoiceVisualizer
                  class="text-red-500"
                  label="Microphone is listening"
                  state="listening"
                />
                <span>Listening…</span>
                <span class="tabular-nums opacity-70">
                  {elapsedLabel(recording().startedAt, nowMs())}
                </span>
              </div>
            )}
          </Show>
        )}
      </Show>
      <Show
        when={
          props.active() &&
          props.playbackState() === "playing" &&
          state().kind === "idle"
        }
      >
        <div
          class="bg-muted absolute right-0 bottom-full z-10 mb-2 flex w-max max-w-[calc(100vw-2rem)] items-center gap-2 rounded-md border px-3 py-1.5 text-sm shadow-sm"
          role="status"
        >
          <VoiceVisualizer
            class="text-primary"
            label="Tether is speaking"
            state="speaking"
          />
          <span>Tether is speaking…</span>
        </div>
      </Show>
      <Show when={state().kind === "uploading"}>
        <p
          class="bg-background text-muted-foreground absolute right-0 bottom-full z-10 mb-2 rounded-md border px-3 py-2 text-sm shadow-sm"
          role="status"
        >
          Transcribing…
        </p>
      </Show>
      <Show when={state()} keyed>
        {(current) => (
          <Show when={current.kind === "failed" && current}>
            {(failed) => (
              <div
                class="border-destructive/40 bg-background text-destructive absolute right-0 bottom-full z-10 mb-2 flex w-80 max-w-[calc(100vw-2rem)] items-center gap-2 rounded-md border px-3 py-2 text-sm shadow-sm"
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
                    props.onEndConversation();
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
