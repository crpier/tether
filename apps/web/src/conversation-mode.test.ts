import { createRoot } from "solid-js";
import { afterEach, describe, expect, test, vi } from "vitest";

import { createConversationMode } from "./conversation-mode";
import type { SpeechSynthesisLike } from "./speech-player";

// Scripted stand-in for the Web Speech API: `speak` holds utterances until the
// test resolves them; `cancel` drops everything queued.
class FakeSpeechSynthesis implements SpeechSynthesisLike {
  cancellations = 0;
  spoken: { text: string; onend?: () => void; onerror?: () => void }[] = [];

  speak(utterance: {
    text: string;
    onend?: () => void;
    onerror?: () => void;
  }): void {
    this.spoken.push(utterance);
  }

  cancel(): void {
    this.cancellations += 1;
    this.spoken = [];
  }

  finishSpeaking(): void {
    this.spoken.at(-1)?.onend?.();
  }
}

function stubSpeech(): FakeSpeechSynthesis {
  const fake = new FakeSpeechSynthesis();
  vi.stubGlobal(
    "SpeechSynthesisUtterance",
    class {
      constructor(public text: string) {}
    },
  );
  return fake;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("conversation mode", () => {
  test("defaults off and toggles", () => {
    createRoot((dispose) => {
      const mode = createConversationMode({ synthesis: stubSpeech() });
      expect(mode.enabled()).toBe(false);
      mode.toggle();
      expect(mode.enabled()).toBe(true);
      expect(mode.replyMode()).toBe("spoken");
      mode.toggle();
      expect(mode.replyMode()).toBe("text");
      dispose();
    });
  });

  test("Ctrl+Shift+V toggles from the keyboard", () => {
    createRoot((dispose) => {
      const mode = createConversationMode({ synthesis: stubSpeech() });
      fireEventKeyDown({ ctrlKey: true, key: "V", shiftKey: true });
      expect(mode.enabled()).toBe(true);
      fireEventKeyDown({ ctrlKey: true, key: "V", shiftKey: true });
      expect(mode.enabled()).toBe(false);
      dispose();
    });
  });

  test("hands-free toggles independently and defaults off", () => {
    createRoot((dispose) => {
      const mode = createConversationMode({ synthesis: stubSpeech() });
      expect(mode.handsFree()).toBe(false);
      mode.toggleHandsFree();
      expect(mode.handsFree()).toBe(true);
      dispose();
    });
  });

  test("sentences stream out normalized and queued for speech", () => {
    const speech = stubSpeech();
    createRoot((dispose) => {
      const mode = createConversationMode({ synthesis: speech });
      mode.toggle();
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
      const mode = createConversationMode({ synthesis: speech });
      mode.toggle();
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
      const mode = createConversationMode({ synthesis: speech });
      mode.toggle();
      mode.spokenTurn.settle("", {
        fullText: "[ran a tool]",
        toolOnly: true,
      });
      expect(speech.spoken).toHaveLength(0);
      expect(mode.isSpoken("[ran a tool]")).toBe(false);
      dispose();
    });
  });

  test("restart and discard cancel active speech", () => {
    const speech = stubSpeech();
    createRoot((dispose) => {
      const mode = createConversationMode({ synthesis: speech });
      mode.toggle();
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

  test("a naturally finished spoken reply bumps the hands-free re-arm tick", () => {
    const speech = stubSpeech();
    createRoot((dispose) => {
      const mode = createConversationMode({ synthesis: speech });
      mode.toggle();
      mode.toggleHandsFree();
      expect(mode.voiceAutoStart()).toBe(0);
      mode.spokenTurn.sentence("All done.");
      speech.finishSpeaking();
      expect(mode.voiceAutoStart()).toBe(1);
      dispose();
    });
  });

  test("re-arm requires conversation mode and hands-free", () => {
    const speech = stubSpeech();
    createRoot((dispose) => {
      const mode = createConversationMode({ synthesis: speech });
      mode.toggleHandsFree();
      mode.spokenTurn.sentence("Spoken while mode is off.");
      speech.finishSpeaking();
      expect(mode.voiceAutoStart()).toBe(0);

      mode.toggle();
      mode.toggleHandsFree();
      mode.spokenTurn.sentence("Mode on, hands-free off.");
      speech.finishSpeaking();
      expect(mode.voiceAutoStart()).toBe(0);
      dispose();
    });
  });

  test("interacting during playback breaks the loop for that cycle", () => {
    const speech = stubSpeech();
    createRoot((dispose) => {
      const mode = createConversationMode({ synthesis: speech });
      mode.toggle();
      mode.toggleHandsFree();
      mode.spokenTurn.sentence("Long reply. Still going.");
      fireEventKeyDown({ key: "a" });
      speech.finishSpeaking();
      expect(mode.voiceAutoStart()).toBe(0);
      dispose();
    });
  });

  test("stopping playback early never re-arms recording", () => {
    const speech = stubSpeech();
    createRoot((dispose) => {
      const mode = createConversationMode({ synthesis: speech });
      mode.toggle();
      mode.toggleHandsFree();
      mode.spokenTurn.sentence("Long reply.");
      mode.stopPlayback();
      expect(mode.voiceAutoStart()).toBe(0);
      dispose();
    });
  });

  test("sending a prompt barge-ins over active playback", () => {
    const speech = stubSpeech();
    createRoot((dispose) => {
      const mode = createConversationMode({ synthesis: speech });
      mode.toggle();
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
      const mode = createConversationMode({ synthesis: speech });
      mode.toggle();
      mode.spokenTurn.sentence("A fairly long spoken answer.");
      mode.onRecordingStart();
      expect(mode.playbackState()).toBe("idle");
      dispose();
    });
  });

  test("Escape stops playback", () => {
    const speech = stubSpeech();
    createRoot((dispose) => {
      const mode = createConversationMode({ synthesis: speech });
      mode.toggle();
      mode.spokenTurn.sentence("A fairly long spoken answer.");
      fireEventKeyDown({ key: "Escape" });
      expect(mode.playbackState()).toBe("idle");
      dispose();
    });
  });

  test("supported() reports whether any speech adapter exists", () => {
    createRoot((dispose) => {
      expect(createConversationMode({ synthesis: null }).supported()).toBe(
        false,
      );
      expect(
        createConversationMode({
          synthesis: new FakeSpeechSynthesis(),
        }).supported(),
      ).toBe(true);
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
