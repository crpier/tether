/**
 * Browser speech playback behind a narrow interface.
 *
 * MVP adapter: the Web Speech API (`window.speechSynthesis`) — no host
 * endpoint, API key, storage, or generated-audio transfer, with immediate
 * access to installed voices. Provider-generated speech can replace this
 * adapter later without touching the chat state machine, which only ever
 * sees speak/cancel/state.
 */

import { createSignal } from "solid-js";

export type SpeechPlayerState = "idle" | "playing" | "error";

/** The slice of `SpeechSynthesisUtterance` playback actually needs. */
export interface SpeakableUtterance {
  onend?: ((...args: never[]) => void) | null;
  onerror?: ((...args: never[]) => void) | null;
}

export interface SpeechSynthesisLike {
  cancel(): void;
  speak(utterance: SpeakableUtterance): void;
}

export interface SpeechPlayer {
  cancel(): void;
  speak(text: string): void;
  state(): SpeechPlayerState;
}

export interface SpeechPlayerOptions {
  /** Injectable synthesis; defaults to `window.speechSynthesis`. */
  synthesis?: SpeechSynthesisLike | null;
  /** Injectable utterance constructor for deterministic tests. */
  utteranceFactory?: (text: string) => SpeakableUtterance;
  /**
   * Invoked exactly once when an utterance finishes playing naturally —
   * never on cancel, supersession by a later speak, or error. This is the
   * hook the hands-free loop (#544) uses to re-arm recording.
   */
  onEnded?: () => void;
}

export function createSpeechPlayer(
  options: SpeechPlayerOptions = {},
): SpeechPlayer {
  const [state, setState] = createSignal<SpeechPlayerState>("idle");
  // Read speechSynthesis through an optional view: the DOM lib types it as
  // always present, but jsdom and older browsers expose nothing at all.
  const domSynthesis = window as { speechSynthesis?: SpeechSynthesisLike };
  const synthesis: SpeechSynthesisLike | null =
    options.synthesis !== undefined
      ? options.synthesis
      : (domSynthesis.speechSynthesis ?? null);
  const utteranceFactory =
    options.utteranceFactory ??
    ((text: string): SpeakableUtterance => new SpeechSynthesisUtterance(text));

  // Bumped whenever speech is cancelled or replaced. Real browsers fire the
  // cancelled utterance's `onend` asynchronously after cancel(); the token
  // lets stale handlers recognize they no longer represent live playback.
  let playbackToken = 0;

  const cancel = () => {
    playbackToken += 1;
    if (synthesis !== null) {
      synthesis.cancel();
    }
    setState("idle");
  };

  const speak = (text: string) => {
    if (synthesis === null || text.trim().length === 0) {
      return;
    }
    // A new spoken reply replaces stale queued speech instead of overlapping.
    playbackToken += 1;
    const token = playbackToken;
    synthesis.cancel();
    const utterance = utteranceFactory(text);
    utterance.onend = () => {
      if (token !== playbackToken) {
        return;
      }
      // Idempotent: some environments can deliver end more than once.
      utterance.onend = null;
      setState("idle");
      options.onEnded?.();
    };
    utterance.onerror = () => {
      if (token !== playbackToken) {
        return;
      }
      setState("error");
    };
    setState("playing");
    synthesis.speak(utterance);
  };

  return { cancel, speak, state };
}
