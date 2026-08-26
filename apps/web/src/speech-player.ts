import { createSignal } from "solid-js";

export type SpeechPlayerState = "idle" | "playing" | "error";

export interface PlayableAudio {
  currentTime: number;
  onended: ((event: Event) => void) | null;
  onerror: ((event: Event) => void) | null;
  pause(): void;
  play(): Promise<void>;
}

export type SynthesizeSpeech = (
  text: string,
  signal: AbortSignal,
) => Promise<Blob>;

export interface SpeechPlayer {
  cancel(): void;
  /** Appends speech without cancelling what is already playing or queued. */
  enqueue(text: string): void;
  speak(text: string): void;
  state(): SpeechPlayerState;
}

export interface SpeechPlayerOptions {
  createAudio?: (source: string) => PlayableAudio;
  createObjectURL?: (audio: Blob) => string;
  onEnded?: () => void;
  revokeObjectURL?: (url: string) => void;
  synthesize: SynthesizeSpeech;
}

interface ActivePlayback {
  audio: PlayableAudio | null;
  controller: AbortController;
  objectUrl: string | null;
  runToken: number;
}

interface QueuedSpeech {
  controller: AbortController;
  generated: Promise<{ audio: Blob } | { error: unknown }>;
}

function aborted(): Error {
  const error = new Error("Speech playback cancelled");
  error.name = "AbortError";
  return error;
}

function playAudio(audio: PlayableAudio, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const cleanUp = () => {
      signal.removeEventListener("abort", onAbort);
      audio.onended = null;
      audio.onerror = null;
    };
    const onAbort = () => {
      cleanUp();
      reject(aborted());
    };
    audio.onended = () => {
      cleanUp();
      resolve();
    };
    audio.onerror = () => {
      cleanUp();
      reject(new Error("Audio playback failed"));
    };
    signal.addEventListener("abort", onAbort, { once: true });
    void audio.play().catch((error: unknown) => {
      cleanUp();
      reject(
        error instanceof Error ? error : new Error("Audio playback failed"),
      );
    });
  });
}

export function createSpeechPlayer(options: SpeechPlayerOptions): SpeechPlayer {
  const [state, setState] = createSignal<SpeechPlayerState>("idle");
  const createAudio =
    options.createAudio ?? ((source: string) => new Audio(source));
  const createObjectURL =
    options.createObjectURL ?? ((audio: Blob) => URL.createObjectURL(audio));
  const revokeObjectURL =
    options.revokeObjectURL ?? ((url: string) => URL.revokeObjectURL(url));
  const waiting: QueuedSpeech[] = [];
  let active: ActivePlayback | null = null;
  let activeRunToken: number | null = null;
  let playbackToken = 0;

  const abortWaiting = () => {
    for (const queued of waiting) {
      queued.controller.abort();
    }
    waiting.length = 0;
  };

  const releaseActive = (runToken: number, pause: boolean) => {
    if (active?.runToken !== runToken) {
      return;
    }
    if (pause && active.audio !== null) {
      active.audio.pause();
      active.audio.currentTime = 0;
    }
    if (active.objectUrl !== null) {
      revokeObjectURL(active.objectUrl);
    }
    active = null;
  };

  const drain = async (runToken: number) => {
    try {
      while (runToken === playbackToken && waiting.length > 0) {
        const queued = waiting.shift();
        if (queued === undefined) {
          break;
        }
        active = {
          audio: null,
          controller: queued.controller,
          objectUrl: null,
          runToken,
        };
        const generated = await queued.generated;
        if (runToken !== playbackToken) {
          return;
        }
        if ("error" in generated) {
          throw generated.error instanceof Error
            ? generated.error
            : new Error("Speech generation failed");
        }
        const objectUrl = createObjectURL(generated.audio);
        const audio = createAudio(objectUrl);
        active = {
          audio,
          controller: queued.controller,
          objectUrl,
          runToken,
        };
        await playAudio(audio, queued.controller.signal);
        releaseActive(runToken, false);
      }
      if (runToken === playbackToken) {
        setState("idle");
        options.onEnded?.();
      }
    } catch (error: unknown) {
      releaseActive(runToken, false);
      if (
        runToken === playbackToken &&
        !(error instanceof Error && error.name === "AbortError")
      ) {
        abortWaiting();
        setState("error");
      }
    } finally {
      if (activeRunToken === runToken) {
        activeRunToken = null;
      }
    }
  };

  const startDrain = () => {
    if (activeRunToken !== null || waiting.length === 0) {
      return;
    }
    const runToken = playbackToken;
    activeRunToken = runToken;
    setState("playing");
    void drain(runToken);
  };

  const cancel = () => {
    playbackToken += 1;
    abortWaiting();
    activeRunToken = null;
    if (active !== null) {
      active.controller.abort();
      releaseActive(active.runToken, true);
    }
    setState("idle");
  };

  const enqueue = (text: string) => {
    if (text.trim().length === 0) {
      return;
    }
    const controller = new AbortController();
    waiting.push({
      controller,
      generated: options.synthesize(text, controller.signal).then(
        (audio) => ({ audio }),
        (error: unknown) => ({ error }),
      ),
    });
    startDrain();
  };

  const speak = (text: string) => {
    if (text.trim().length === 0) {
      return;
    }
    cancel();
    enqueue(text);
  };

  return {
    cancel,
    enqueue,
    speak,
    state,
  };
}
