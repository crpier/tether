import { describe, expect, test } from "vitest";

import { createSpeechPlayer } from "./speech-player";

class FakeUtterance {
  onend?: () => void;
  onerror?: () => void;
  constructor(public text: string) {}
}

class FakeSynthesis {
  cancellations = 0;
  spoken: FakeUtterance[] = [];

  speak(utterance: FakeUtterance): void {
    this.spoken.push(utterance);
  }

  cancel(): void {
    this.cancellations += 1;
    this.spoken = [];
  }
}

class VoiceLoadingSynthesis extends FakeSynthesis {
  private listeners: (() => void)[] = [];
  private voiceCount = 0;

  addEventListener(_type: "voiceschanged", listener: () => void): void {
    this.listeners.push(listener);
  }

  getVoices(): unknown[] {
    return Array.from({ length: this.voiceCount }, () => ({}));
  }

  loadVoices(count = 1): void {
    this.voiceCount = count;
    for (const listener of this.listeners) {
      listener();
    }
  }
}

function makePlayer(onEnded?: () => void) {
  const synthesis = new FakeSynthesis();
  const player = createSpeechPlayer({
    synthesis,
    utteranceFactory: (text) => new FakeUtterance(text),
    onEnded,
  });
  return { player, synthesis };
}

describe("speech player", () => {
  test("speak queues the text and reports playing", () => {
    const { player, synthesis } = makePlayer();

    player.speak("hello there");

    expect(player.state()).toBe("playing");
    expect(synthesis.spoken).toHaveLength(1);
    expect(synthesis.spoken[0].text).toBe("hello there");
  });

  test("utterance end returns to idle", () => {
    const { player, synthesis } = makePlayer();
    player.speak("hello");

    synthesis.spoken[0].onend?.();

    expect(player.state()).toBe("idle");
  });

  test("utterance error reports an error state", () => {
    const { player, synthesis } = makePlayer();
    player.speak("hello");

    synthesis.spoken[0].onerror?.();

    expect(player.state()).toBe("error");
  });

  test("a later speak cancels the earlier one instead of overlapping", () => {
    const { player, synthesis } = makePlayer();
    player.speak("first");
    player.speak("second");

    expect(synthesis.cancellations).toBeGreaterThanOrEqual(1);
    expect(synthesis.spoken).toHaveLength(1);
    expect(synthesis.spoken[0].text).toBe("second");
    expect(player.state()).toBe("playing");
  });

  test("cancel stops speech and resets to idle", () => {
    const { player, synthesis } = makePlayer();
    player.speak("hello");

    player.cancel();

    expect(synthesis.cancellations).toBeGreaterThanOrEqual(1);
    expect(player.state()).toBe("idle");
  });

  test("blank text is a no-op", () => {
    const { player, synthesis } = makePlayer();

    player.speak("   ");

    expect(synthesis.spoken).toHaveLength(0);
    expect(player.state()).toBe("idle");
  });

  test("an unsupported environment degrades to a safe no-op", () => {
    const player = createSpeechPlayer({ synthesis: null });

    player.speak("hello");

    expect(player.state()).toBe("idle");
  });

  describe("onEnded (hands-free loop, #544)", () => {
    test("fires exactly once when playback completes naturally", () => {
      let ended = 0;
      const { player, synthesis } = makePlayer(() => {
        ended += 1;
      });
      player.speak("hello");

      synthesis.spoken[0].onend?.();
      synthesis.spoken[0].onend?.();

      expect(ended).toBe(1);
      expect(player.state()).toBe("idle");
    });

    test("does not fire when cancel stops playback first", () => {
      // Real browsers fire the cancelled utterance's onend after cancel();
      // that must not count as a natural completion.
      let ended = 0;
      const { player, synthesis } = makePlayer(() => {
        ended += 1;
      });
      player.speak("hello");
      const spoken = synthesis.spoken[0];

      player.cancel();
      // Browsers deliver the cancelled utterance's onend after cancel().
      spoken.onend?.();

      expect(ended).toBe(0);
    });

    test("does not fire when a later speak supersedes the utterance", () => {
      let ended = 0;
      const { player, synthesis } = makePlayer(() => {
        ended += 1;
      });
      player.speak("first");
      player.speak("second");

      synthesis.spoken[0]?.onend?.();
      synthesis.spoken[0].onend?.();

      expect(ended).toBe(1);
    });

    test("does not fire on error", () => {
      let ended = 0;
      const { player, synthesis } = makePlayer(() => {
        ended += 1;
      });
      player.speak("hello");

      synthesis.spoken[0].onerror?.();

      expect(ended).toBe(0);
    });
  });
});

describe("speech queueing (#545)", () => {
  test("enqueue appends without cancelling what is playing", () => {
    const { player, synthesis } = makePlayer();
    player.speak("first");
    const cancellationsAfterSpeak = synthesis.cancellations;

    player.enqueue("second");

    expect(synthesis.cancellations).toBe(cancellationsAfterSpeak);
    expect(synthesis.spoken.map((utterance) => utterance.text)).toEqual([
      "first",
      "second",
    ]);
    expect(player.state()).toBe("playing");
  });

  test("enqueue on an idle player starts playback", () => {
    const { player, synthesis } = makePlayer();

    player.enqueue("only");

    expect(synthesis.spoken.map((utterance) => utterance.text)).toEqual([
      "only",
    ]);
    expect(player.state()).toBe("playing");
  });

  test("onEnded fires only after the last queued utterance ends", () => {
    let ended = 0;
    const { player, synthesis } = makePlayer(() => {
      ended += 1;
    });
    player.speak("first");
    player.enqueue("second");
    player.enqueue("third");

    synthesis.spoken[0].onend?.();
    synthesis.spoken[1].onend?.();
    expect(ended).toBe(0);

    synthesis.spoken[2].onend?.();
    expect(ended).toBe(1);
    expect(player.state()).toBe("idle");
  });

  test("cancel drops the whole queue and suppresses pending end callbacks", () => {
    let ended = 0;
    const { player, synthesis } = makePlayer(() => {
      ended += 1;
    });
    player.speak("first");
    player.enqueue("second");
    const queued = [...synthesis.spoken];

    player.cancel();
    for (const utterance of queued) {
      utterance.onend?.();
    }

    expect(ended).toBe(0);
    expect(player.state()).toBe("idle");
  });

  test("blank enqueue is a no-op", () => {
    const { player, synthesis } = makePlayer();

    player.enqueue("   ");

    expect(synthesis.spoken).toHaveLength(0);
    expect(player.state()).toBe("idle");
  });
});

describe("speech support probe (#546)", () => {
  test("supported reflects the presence of a synthesis adapter", () => {
    const withSynthesis = makePlayer();
    expect(withSynthesis.player.supported()).toBe(true);

    const without = createSpeechPlayer({ synthesis: null });
    expect(without.supported()).toBe(false);
  });

  test("desktop synthesis with no installed voices is unsupported", () => {
    const synthesis = new VoiceLoadingSynthesis();
    const player = createSpeechPlayer({
      synthesis,
      utteranceFactory: (text) => new FakeUtterance(text),
    });

    player.enqueue("hello");

    expect(player.supported()).toBe(false);
    expect(player.state()).toBe("idle");
    expect(synthesis.spoken).toHaveLength(0);
  });

  test("queued speech waits for desktop voices to load", () => {
    const synthesis = new VoiceLoadingSynthesis();
    const player = createSpeechPlayer({
      synthesis,
      utteranceFactory: (text) => new FakeUtterance(text),
    });

    player.enqueue("hello after voices");
    expect(synthesis.spoken).toHaveLength(0);

    synthesis.loadVoices();

    expect(player.supported()).toBe(true);
    expect(player.state()).toBe("playing");
    expect(synthesis.spoken).toHaveLength(1);
    expect(synthesis.spoken[0].text).toBe("hello after voices");
  });

  test("cancel drops speech waiting for desktop voices", () => {
    const synthesis = new VoiceLoadingSynthesis();
    const player = createSpeechPlayer({
      synthesis,
      utteranceFactory: (text) => new FakeUtterance(text),
    });

    player.enqueue("never say this");
    player.cancel();
    synthesis.loadVoices();

    expect(synthesis.spoken).toHaveLength(0);
    expect(player.state()).toBe("idle");
  });
});
