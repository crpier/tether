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

function makePlayer() {
  const synthesis = new FakeSynthesis();
  const player = createSpeechPlayer({
    synthesis,
    utteranceFactory: (text) => new FakeUtterance(text),
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
});
