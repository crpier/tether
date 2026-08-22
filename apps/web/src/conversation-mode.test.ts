import { createRoot } from "solid-js";
import { describe, expect, test } from "vitest";

import { createConversationMode } from "./conversation-mode";
import type { SpeechPlayer, SpeechPlayerState } from "./speech-player";

class FakeCuePlayer {
  played: string[] = [];

  dispose(): void {
    // No browser resources in the fake.
  }

  play(cue: string): Promise<void> {
    this.played.push(cue);
    return Promise.resolve();
  }

  unlock(): void {
    // Browser autoplay policy does not apply to the fake.
  }
}

class FakeSpeechPlayer implements SpeechPlayer {
  cancellations = 0;
  onEnded: (() => void) | undefined;
  spoken: { text: string }[] = [];
  private playbackState: SpeechPlayerState = "idle";

  cancel(): void {
    this.cancellations += 1;
    this.spoken = [];
    this.playbackState = "idle";
  }

  enqueue(text: string): void {
    this.spoken.push({ text });
    this.playbackState = "playing";
  }

  finishSpeaking(): void {
    this.playbackState = "idle";
    this.onEnded?.();
  }

  speak(text: string): void {
    this.cancel();
    this.enqueue(text);
  }

  state(): SpeechPlayerState {
    return this.playbackState;
  }
}

function stubSpeech(): FakeSpeechPlayer {
  return new FakeSpeechPlayer();
}

function withSpeech(speech: FakeSpeechPlayer) {
  return {
    playerFactory: (onEnded: () => void): SpeechPlayer => {
      speech.onEnded = onEnded;
      return speech;
    },
  };
}

describe("conversation mode", () => {
  test("start activates spoken replies and arms the first recording", () => {
    createRoot((dispose) => {
      const mode = createConversationMode(withSpeech(stubSpeech()));
      expect(mode.enabled()).toBe(false);
      expect(mode.voiceAutoStart()).toBe(0);

      mode.start();

      expect(mode.enabled()).toBe(true);
      expect(mode.replyMode()).toBe("spoken");
      expect(mode.voiceAutoStart()).toBe(1);
      mode.stop();
      expect(mode.replyMode()).toBe("text");
      dispose();
    });
  });

  test("recording transitions play listening cues only during voice conversation", async () => {
    const root = createRoot((dispose) => {
      const cues = new FakeCuePlayer();
      return {
        cues,
        dispose,
        mode: createConversationMode({
          ...withSpeech(stubSpeech()),
          cuePlayer: cues,
        }),
      };
    });

    root.mode.start();
    await root.mode.onRecordingStart();
    root.mode.stop();
    root.mode.onRecordingStop();
    await root.mode.onRecordingStart();

    expect(root.cues.played).toEqual(["listening-start", "listening-stop"]);
    root.dispose();
  });

  test("cue failures do not interrupt conversation behavior", async () => {
    const cues = {
      dispose: () => undefined,
      play: () => Promise.reject(new Error("speaker unavailable")),
      unlock: () => {
        throw new Error("audio unavailable");
      },
    };
    const root = createRoot((dispose) => ({
      dispose,
      mode: createConversationMode({
        ...withSpeech(stubSpeech()),
        cuePlayer: cues,
      }),
    }));

    expect(() => root.mode.start()).not.toThrow();
    await expect(root.mode.onRecordingStart()).resolves.toBeUndefined();
    expect(root.mode.enabled()).toBe(true);
    root.dispose();
  });

  test("Ctrl+Shift+V toggles from the keyboard", () => {
    createRoot((dispose) => {
      const mode = createConversationMode(withSpeech(stubSpeech()));
      fireEventKeyDown({ ctrlKey: true, key: "V", shiftKey: true });
      expect(mode.enabled()).toBe(true);
      fireEventKeyDown({ ctrlKey: true, key: "V", shiftKey: true });
      expect(mode.enabled()).toBe(false);
      dispose();
    });
  });

  test("sentences stream out normalized and queued for speech", () => {
    const speech = stubSpeech();
    createRoot((dispose) => {
      const mode = createConversationMode(withSpeech(speech));
      mode.start();
      mode.spokenTurn.sentence("**Hello** there. ");
      expect(speech.spoken.map((utterance) => utterance.text)).toEqual([
        "Hello there.",
      ]);
      dispose();
    });
  });

  test("settle speaks the unspoken tail and marks the reply as spoken", () => {
    const speech = stubSpeech();
    createRoot((dispose) => {
      const mode = createConversationMode(withSpeech(speech));
      mode.start();
      mode.spokenTurn.sentence("First part.");
      speech.spoken.length = 0;
      mode.spokenTurn.settle(" Second part.", {
        fullText: "First part. Second part.",
        toolOnly: false,
      });
      expect(speech.spoken.map((utterance) => utterance.text)).toEqual([
        "Second part.",
      ]);
      expect(mode.isSpoken("First part. Second part.")).toBe(true);
      expect(mode.isSpoken("Something else")).toBe(false);
      dispose();
    });
  });

  test("tool-only settles stay silent and unmarked", () => {
    const speech = stubSpeech();
    createRoot((dispose) => {
      const mode = createConversationMode(withSpeech(speech));
      mode.start();
      mode.spokenTurn.settle("", {
        fullText: "[ran a tool]",
        toolOnly: true,
      });
      expect(speech.spoken).toHaveLength(0);
      expect(mode.isSpoken("[ran a tool]")).toBe(false);
      dispose();
    });
  });

  test("tool phases play a cue only during voice conversation", () => {
    const root = createRoot((dispose) => {
      const cues = new FakeCuePlayer();
      return {
        cues,
        dispose,
        mode: createConversationMode({
          ...withSpeech(stubSpeech()),
          cuePlayer: cues,
        }),
      };
    });

    root.mode.spokenTurn.restart();
    expect(root.cues.played).toEqual([]);

    root.mode.start();
    root.mode.spokenTurn.restart();
    expect(root.cues.played).toEqual(["tool"]);

    root.mode.stop();
    root.mode.spokenTurn.restart();
    expect(root.cues.played).toEqual(["tool"]);
    root.dispose();
  });

  test("restart and discard cancel active speech", () => {
    const speech = stubSpeech();
    createRoot((dispose) => {
      const mode = createConversationMode(withSpeech(speech));
      mode.start();
      mode.spokenTurn.sentence("Stale lead-in.");
      expect(mode.playbackState()).toBe("playing");
      mode.spokenTurn.restart();
      expect(mode.playbackState()).toBe("idle");

      mode.spokenTurn.sentence("Another lead-in.");
      mode.spokenTurn.discard();
      expect(mode.playbackState()).toBe("idle");
      dispose();
    });
  });

  test("a naturally finished spoken reply re-arms recording", () => {
    const speech = stubSpeech();
    createRoot((dispose) => {
      const mode = createConversationMode(withSpeech(speech));
      mode.start();
      expect(mode.voiceAutoStart()).toBe(1);
      mode.spokenTurn.sentence("All done.");
      speech.finishSpeaking();
      expect(mode.voiceAutoStart()).toBe(2);
      dispose();
    });
  });

  test("ending conversation prevents a spoken reply from re-arming", () => {
    const speech = stubSpeech();
    createRoot((dispose) => {
      const mode = createConversationMode(withSpeech(speech));
      mode.start();
      mode.spokenTurn.sentence("Conversation is ending.");
      mode.stop();
      speech.finishSpeaking();
      expect(mode.voiceAutoStart()).toBe(1);
      dispose();
    });
  });

  test("interacting during playback breaks the loop for that cycle", () => {
    const speech = stubSpeech();
    createRoot((dispose) => {
      const mode = createConversationMode(withSpeech(speech));
      mode.start();
      mode.spokenTurn.sentence("Long reply. Still going.");
      fireEventKeyDown({ key: "a" });
      speech.finishSpeaking();
      expect(mode.voiceAutoStart()).toBe(1);
      dispose();
    });
  });

  test("sending a prompt barge-ins over active playback", () => {
    const speech = stubSpeech();
    createRoot((dispose) => {
      const mode = createConversationMode(withSpeech(speech));
      mode.start();
      mode.spokenTurn.sentence("A fairly long spoken answer.");
      mode.onPromptSent();
      expect(mode.playbackState()).toBe("idle");
      expect(speech.cancellations).toBeGreaterThanOrEqual(1);
      dispose();
    });
  });

  test("starting a recording cancels active playback", () => {
    const speech = stubSpeech();
    createRoot((dispose) => {
      const mode = createConversationMode(withSpeech(speech));
      mode.start();
      mode.spokenTurn.sentence("A fairly long spoken answer.");
      void mode.onRecordingStart();
      expect(mode.playbackState()).toBe("idle");
      dispose();
    });
  });

  test("Escape stops playback", () => {
    const speech = stubSpeech();
    createRoot((dispose) => {
      const mode = createConversationMode(withSpeech(speech));
      mode.start();
      mode.spokenTurn.sentence("A fairly long spoken answer.");
      fireEventKeyDown({ key: "Escape" });
      expect(mode.playbackState()).toBe("idle");
      dispose();
    });
  });
});

function fireEventKeyDown(init: {
  ctrlKey?: boolean;
  key: string;
  shiftKey?: boolean;
}): void {
  window.dispatchEvent(
    new KeyboardEvent("keydown", {
      bubbles: true,
      cancelable: true,
      ...init,
    }),
  );
}
