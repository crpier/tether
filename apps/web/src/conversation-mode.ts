/**
 * The conversation-mode spoken loop behind one interface.
 *
 * Owns everything about "replies come out of the speaker": the speech player,
 * the spoken-turn sink handed to the live chat turn, hands-free re-arm,
 * barge-in, and interaction tracking. The chat page consumes this module and
 * stops knowing how speech works — loop bugs concentrate here, and loop tests
 * hit this interface instead of the whole page.
 *
 * Loop rules (issues #542/#544/#545/#546):
 * - Sentences stream to the speaker as they complete; tool activity
 *   invalidates the provisional prefix so the settled answer plays whole.
 * - After a spoken reply finishes naturally, an opt-in hands-free loop re-arms
 *   recording so the user can just talk.
 * - Any user interaction during playback (typing, clicking) means the user
 *   took over: the loop stands down for that cycle, and barge-in (sending a
 *   prompt, starting a recording, Escape) stops playback outright.
 */

import { createSignal, onCleanup } from "solid-js";

import type { ReplyMode } from "./chat-bus";
import { createSpeechPlayer } from "./speech-player";
import type {
  SpeechPlayer,
  SpeechPlayerState,
  SynthesizeSpeech,
} from "./speech-player";
import { toSpeechText } from "./speech-text";
import type { SpokenTurnSink } from "./live-chat-turn";

export type ConversationModeOptions =
  | {
      playerFactory?: never;
      synthesize: SynthesizeSpeech;
    }
  | {
      playerFactory: (onEnded: () => void) => SpeechPlayer;
      synthesize?: never;
    };

export interface ConversationMode {
  /** Whether replies are captured as spoken. */
  enabled(): boolean;
  toggle(): void;
  /** Reply mode for prompts enqueued while the toggle is in this state. */
  replyMode(): ReplyMode;
  /** Whether the hands-free re-arm loop is opted in. */
  handsFree(): boolean;
  toggleHandsFree(): void;
  /** Barge-in: the user sent a prompt; stop active playback. */
  onPromptSent(): void;
  /** Barge-in: a recording is about to open the microphone. */
  onRecordingStart(): void;
  /** Spoken-turn sink to hand to `createLiveChatTurn`. */
  spokenTurn: SpokenTurnSink;
  /** Whether a settled assistant text was spoken this session (🔊 chip). */
  isSpoken(text: string): boolean;
  /** Bumped when the hands-free loop re-arms recording. */
  voiceAutoStart(): number;
  playbackState(): SpeechPlayerState;
  stopPlayback(): void;
}

export function createConversationMode(
  options: ConversationModeOptions,
): ConversationMode {
  const [enabled, setEnabled] = createSignal(false);
  const [handsFree, setHandsFree] = createSignal(false);
  // Transcript texts whose settled form was spoken this session (#546): the
  // 🔊 chip. Session-scoped by design — durable per-message mode flags need a
  // host schema change and are deliberately out of scope here.
  const [spokenTexts, setSpokenTexts] = createSignal<Set<string>>(new Set());
  const [voiceAutoStart, setVoiceAutoStart] = createSignal(0);

  // Hands-free loop state machine. `spokeAt` stamps when the current stretch
  // of speech began; any interaction after that stamp means the user took
  // over and the loop stands down for the cycle.
  let spokeAt = 0;
  let lastInteractionAt = 0;
  // True once `spokeAt` has been stamped for the current stretch of speech;
  // tool activity resets it because the post-tool answer is a fresh start.
  let markedSpeechStart = false;
  const markSpeechStart = () => {
    if (!markedSpeechStart) {
      spokeAt = Date.now();
      markedSpeechStart = true;
    }
  };

  const onPlaybackEnded = () => {
    if (
      enabled() &&
      handsFree() &&
      lastInteractionAt < spokeAt &&
      spokeAt > 0
    ) {
      setVoiceAutoStart((tick) => tick + 1);
    }
  };
  const speechPlayer =
    options.playerFactory === undefined
      ? createSpeechPlayer({
          onEnded: onPlaybackEnded,
          synthesize: options.synthesize,
        })
      : options.playerFactory(onPlaybackEnded);

  // Interaction tracking plus the two global shortcuts. Capture phase so the
  // stamp lands even when a focused control swallows the event.
  const markInteraction = () => {
    lastInteractionAt = Date.now();
  };
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === "Escape" && speechPlayer.state() === "playing") {
      event.preventDefault();
      speechPlayer.cancel();
      return;
    }
    // Ctrl+Shift+V flips conversation mode without leaving the keyboard.
    if (event.ctrlKey && event.shiftKey && event.key.toLowerCase() === "v") {
      event.preventDefault();
      setEnabled((value) => !value);
    }
  };
  window.addEventListener("keydown", markInteraction, { capture: true });
  window.addEventListener("keydown", onKeyDown);
  window.addEventListener("pointerdown", markInteraction, { capture: true });
  // Leaving the chat page must never leave speech running.
  onCleanup(() => {
    speechPlayer.cancel();
    window.removeEventListener("keydown", markInteraction, { capture: true });
    window.removeEventListener("keydown", onKeyDown);
    window.removeEventListener("pointerdown", markInteraction, {
      capture: true,
    });
  });

  const spokenTurn: SpokenTurnSink = {
    // Sentences stream in as they complete (#545): normalized and queued for
    // speech immediately, before the turn settles.
    sentence: (text) => {
      const spoken = toSpeechText(text);
      if (spoken.length > 0) {
        markSpeechStart();
        speechPlayer.enqueue(spoken);
      }
    },
    // Tool activity invalidates provisional prose: stop talking so the
    // settled answer (which plays whole at settle) isn't preceded by a
    // now-stale lead-in.
    restart: () => {
      markedSpeechStart = false;
      speechPlayer.cancel();
    },
    settle: (unspokenTail, info) => {
      if (info.toolOnly) {
        // The settled text is a host-side tool-only marker, not real
        // prose — silence beats speaking internal scaffolding.
        return;
      }
      setSpokenTexts((current) => {
        const next = new Set(current);
        next.add(info.fullText);
        return next;
      });
      const spoken = toSpeechText(unspokenTail);
      if (spoken.length > 0) {
        markSpeechStart();
        speechPlayer.enqueue(spoken);
      }
    },
    discard: () => {
      markedSpeechStart = false;
      speechPlayer.cancel();
    },
  };

  return {
    enabled,
    toggle: () => {
      setEnabled((value) => !value);
    },
    replyMode: () => (enabled() ? "spoken" : "text"),
    handsFree,
    toggleHandsFree: () => {
      setHandsFree((value) => !value);
    },
    onPromptSent: () => {
      speechPlayer.cancel();
    },
    onRecordingStart: () => {
      // Avoid microphone feedback from an ongoing reply.
      speechPlayer.cancel();
    },
    spokenTurn,
    isSpoken: (text) => spokenTexts().has(text),
    voiceAutoStart,
    playbackState: () => speechPlayer.state(),
    stopPlayback: () => {
      speechPlayer.cancel();
    },
  };
}
