export type ConversationCue = "listening-start" | "listening-stop" | "tool";

export interface ConversationCuePlayer {
  /** Release browser audio resources owned by this player. */
  dispose(): void;
  /** Play one cue and settle when it ends. */
  play(cue: ConversationCue): Promise<void>;
  /** Unlock browser audio while a user gesture is active. */
  unlock(): void;
}

interface CueNote {
  durationSeconds: number;
  frequencyHz: number;
}

const CUE_NOTES: Record<ConversationCue, readonly CueNote[]> = {
  "listening-start": [
    { durationSeconds: 0.065, frequencyHz: 392 },
    { durationSeconds: 0.085, frequencyHz: 587.33 },
  ],
  "listening-stop": [
    { durationSeconds: 0.065, frequencyHz: 587.33 },
    { durationSeconds: 0.085, frequencyHz: 392 },
  ],
  tool: [
    { durationSeconds: 0.05, frequencyHz: 523.25 },
    { durationSeconds: 0.05, frequencyHz: 659.25 },
    { durationSeconds: 0.075, frequencyHz: 523.25 },
  ],
};

const NOTE_GAP_SECONDS = 0.018;
const CUE_VOLUME = 0.035;

class WebAudioConversationCuePlayer implements ConversationCuePlayer {
  private context: AudioContext | null = null;

  dispose(): void {
    const context = this.context;
    this.context = null;
    if (context !== null) {
      void context.close().catch(() => undefined);
    }
  }

  async play(cue: ConversationCue): Promise<void> {
    const context = this.openContext();
    if (context === null) {
      return;
    }
    try {
      if (context.state === "suspended") {
        await context.resume();
      }
      const notes = CUE_NOTES[cue];
      const startedAt = context.currentTime + 0.01;
      let offset = 0;
      let lastEnded: Promise<void> = Promise.resolve();

      for (const [index, note] of notes.entries()) {
        const noteStartsAt = startedAt + offset;
        const noteEndsAt = noteStartsAt + note.durationSeconds;
        const oscillator = context.createOscillator();
        const gain = context.createGain();
        oscillator.frequency.setValueAtTime(note.frequencyHz, noteStartsAt);
        oscillator.type = cue === "tool" ? "triangle" : "sine";
        gain.gain.setValueAtTime(0.0001, noteStartsAt);
        gain.gain.exponentialRampToValueAtTime(
          CUE_VOLUME,
          noteStartsAt + 0.008,
        );
        gain.gain.exponentialRampToValueAtTime(0.0001, noteEndsAt);
        oscillator.connect(gain);
        gain.connect(context.destination);
        if (index === notes.length - 1) {
          lastEnded = new Promise((resolve) => {
            oscillator.onended = () => {
              oscillator.disconnect();
              gain.disconnect();
              resolve();
            };
          });
        } else {
          oscillator.onended = () => {
            oscillator.disconnect();
            gain.disconnect();
          };
        }
        oscillator.start(noteStartsAt);
        oscillator.stop(noteEndsAt);
        offset += note.durationSeconds + NOTE_GAP_SECONDS;
      }

      await lastEnded;
    } catch {
      // Cues are optional feedback. Browser audio failures stay silent.
    }
  }

  unlock(): void {
    const context = this.openContext();
    if (context?.state === "suspended") {
      void context.resume().catch(() => undefined);
    }
  }

  private openContext(): AudioContext | null {
    if (this.context !== null) {
      return this.context;
    }
    if (typeof AudioContext === "undefined") {
      return null;
    }
    try {
      this.context = new AudioContext();
      return this.context;
    } catch {
      return null;
    }
  }
}

export function createConversationCuePlayer(): ConversationCuePlayer {
  return new WebAudioConversationCuePlayer();
}
