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
 * - Starting voice conversation arms recording immediately; each naturally
 *   finished spoken reply re-arms it so the user can keep talking.
 * - Any user interaction during playback (typing, clicking) means the user
 *   took over: the loop stands down for that cycle, and barge-in (sending a
 *   prompt, starting a recording, Escape) stops playback outright.
 */

import { createSignal, onCleanup } from "solid-js";

import type { ReplyMode } from "./chat-bus";
import {
  createConversationCuePlayer,
  type ConversationCue,
  type ConversationCuePlayer,
} from "./conversation-cues";
import { createSpeechPlayer } from "./speech-player";
import type {
  SpeechPlayer,
  SpeechPlayerState,
  SynthesizeSpeech,
} from "./speech-player";
import { toSpeechText } from "./speech-text";
import type { SpokenTurnSink } from "./live-chat-turn";

type SpeechOptions =
  | {
      playerFactory?: never;
      synthesize: SynthesizeSpeech;
    }
  | {
      playerFactory: (onEnded: () => void) => SpeechPlayer;
      synthesize?: never;
    };

export type ConversationModeOptions = SpeechOptions & {
  cuePlayer?: ConversationCuePlayer;
};

export interface ConversationMode {
  /** Whether the hands-free spoken conversation is active. */
  enabled(): boolean;
  /** Start the spoken loop and immediately arm its first recording. */
  start(): void;
  /** End the spoken loop and cancel active playback. */
  stop(): void;
  /** Reply mode for prompts enqueued while the conversation is in this state. */
  replyMode(): ReplyMode;
  /** Barge-in: the user sent a prompt; stop active playback. */
  onPromptSent(): void;
  /** Barge-in and cue: a recording is about to open the microphone. */
  onRecordingStart(): Promise<void>;
  /** Cue that microphone capture has stopped. */
  onRecordingStop(): void;
  /** Bumped when a sent prompt supersedes microphone capture. */
  recordingCancelSignal(): number;
  /** Spoken-turn sink to hand to `createLiveChatTurn`. */
  spokenTurn: SpokenTurnSink;
  /** Whether a settled assistant text was spoken this session (🔊 chip). */
  isSpoken(text: string): boolean;
  /** Bumped for the initial recording and each hands-free re-arm. */
  voiceAutoStart(): number;
  playbackState(): SpeechPlayerState;
}

export function createConversationMode(
  options: ConversationModeOptions,
): ConversationMode {
  const [enabled, setEnabled] = createSignal(false);
  // Transcript texts whose settled form was spoken this session (#546): the
  // 🔊 chip. Session-scoped by design — durable per-message mode flags need a
  // host schema change and are deliberately out of scope here.
  const [spokenTexts, setSpokenTexts] = createSignal<Set<string>>(new Set());
  const [recordingCancelSignal, setRecordingCancelSignal] = createSignal(0);
  const [voiceAutoStart, setVoiceAutoStart] = createSignal(0);
  const cuePlayer = options.cuePlayer ?? createConversationCuePlayer();

  const playCue = (
    cue: ConversationCue,
    requiresActiveConversation = true,
  ): Promise<void> => {
    if (requiresActiveConversation && !enabled()) {
      return Promise.resolve();
    }
    try {
      return cuePlayer.play(cue).catch(() => undefined);
    } catch {
      return Promise.resolve();
    }
  };

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
    if (enabled() && lastInteractionAt < spokeAt && spokeAt > 0) {
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
      if (enabled()) {
        stop();
      } else {
        start();
      }
    }
  };
  window.addEventListener("keydown", markInteraction, { capture: true });
  window.addEventListener("keydown", onKeyDown);
  window.addEventListener("pointerdown", markInteraction, { capture: true });
  // Leaving the chat page must never leave speech running.
  onCleanup(() => {
    speechPlayer.cancel();
    cuePlayer.dispose();
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
      if (!enabled()) {
        return;
      }
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
      void playCue("tool");
    },
    settle: (unspokenTail, info) => {
      if (!enabled()) {
        return;
      }
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

  const start = () => {
    if (enabled()) {
      return;
    }
    try {
      cuePlayer.unlock();
    } catch {
      // Cues are feedback only. Audio support must not gate conversation.
    }
    setEnabled(true);
    setVoiceAutoStart((tick) => tick + 1);
  };
  const stop = () => {
    if (!enabled()) {
      return;
    }
    setEnabled(false);
    markedSpeechStart = false;
    speechPlayer.cancel();
  };

  return {
    enabled,
    start,
    stop,
    replyMode: () => (enabled() ? "spoken" : "text"),
    onPromptSent: () => {
      setRecordingCancelSignal((tick) => tick + 1);
      speechPlayer.cancel();
    },
    onRecordingStart: () => {
      // Avoid microphone feedback from an ongoing reply. Waiting for the cue
      // keeps its sound out of the captured clip.
      speechPlayer.cancel();
      return playCue("listening-start");
    },
    onRecordingStop: () => {
      // The active flag may already be down when the recorder reports its
      // transition to idle. That edge still needs its closing cue.
      void playCue("listening-stop", false);
    },
    recordingCancelSignal,
    spokenTurn,
    isSpoken: (text) => spokenTexts().has(text),
    voiceAutoStart,
    playbackState: () => speechPlayer.state(),
  };
}
