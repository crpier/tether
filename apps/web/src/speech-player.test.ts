import { describe, expect, test, vi } from "vitest";

import {
  createSpeechPlayer,
  type PlayableAudio,
  type SpeechPlayerOptions,
} from "./speech-player";

class FakeAudio implements PlayableAudio {
  currentTime = 0;
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  pauses = 0;
  plays = 0;

  pause(): void {
    this.pauses += 1;
  }

  play(): Promise<void> {
    this.plays += 1;
    return Promise.resolve();
  }

  finish(): void {
    this.onended?.();
  }

  fail(): void {
    this.onerror?.();
  }
}

interface SpeechHarness {
  abortSignals: AbortSignal[];
  audios: FakeAudio[];
  player: ReturnType<typeof createSpeechPlayer>;
  revoked: string[];
  spoken: string[];
}

function harness(
  options: Pick<SpeechPlayerOptions, "onEnded"> = {},
): SpeechHarness {
  const abortSignals: AbortSignal[] = [];
  const audios: FakeAudio[] = [];
  const revoked: string[] = [];
  const spoken: string[] = [];
  let objectUrl = 0;
  const player = createSpeechPlayer({
    createAudio: () => {
      const audio = new FakeAudio();
      audios.push(audio);
      return audio;
    },
    createObjectURL: () => `blob:speech-${String(++objectUrl)}`,
    onEnded: options.onEnded,
    revokeObjectURL: (url) => {
      revoked.push(url);
    },
    synthesize: (text, signal) => {
      spoken.push(text);
      abortSignals.push(signal);
      return Promise.resolve(new Blob([text], { type: "audio/mpeg" }));
    },
  });
  return { abortSignals, audios, player, revoked, spoken };
}

async function started(harness: SpeechHarness): Promise<FakeAudio> {
  await vi.waitFor(() => {
    expect(harness.audios).toHaveLength(1);
    expect(harness.audios[0].plays).toBe(1);
  });
  return harness.audios[0];
}

describe("provider speech player", () => {
  test("enqueue generates and plays provider audio", async () => {
    const speech = harness();

    speech.player.enqueue("hello there");
    const audio = await started(speech);

    expect(speech.spoken).toEqual(["hello there"]);
    expect(audio.plays).toBe(1);
    expect(speech.player.state()).toBe("playing");
  });

  test("queued fragments pre-generate but play sequentially", async () => {
    const speech = harness();
    speech.player.enqueue("first");
    speech.player.enqueue("second");
    const first = await started(speech);

    expect(speech.spoken).toEqual(["first", "second"]);
    expect(speech.audios).toHaveLength(1);
    first.finish();
    await vi.waitFor(() => expect(speech.audios).toHaveLength(2));

    expect(speech.audios[1].plays).toBe(1);
  });

  test("the final natural completion returns idle and calls onEnded", async () => {
    const onEnded = vi.fn();
    const speech = harness({ onEnded });
    speech.player.enqueue("first");
    speech.player.enqueue("second");
    const first = await started(speech);

    first.finish();
    await vi.waitFor(() => expect(speech.audios).toHaveLength(2));
    speech.audios[1].finish();
    await vi.waitFor(() => expect(speech.player.state()).toBe("idle"));

    expect(onEnded).toHaveBeenCalledTimes(1);
  });

  test("cancel aborts generation, pauses playback, and drops the queue", async () => {
    const onEnded = vi.fn();
    const speech = harness({ onEnded });
    speech.player.enqueue("first");
    speech.player.enqueue("never generate");
    const first = await started(speech);

    speech.player.cancel();
    first.finish();
    await Promise.resolve();

    expect(first.pauses).toBe(1);
    expect(speech.abortSignals.every((signal) => signal.aborted)).toBe(true);
    expect(speech.spoken).toEqual(["first", "never generate"]);
    expect(speech.player.state()).toBe("idle");
    expect(onEnded).not.toHaveBeenCalled();
  });

  test("speak replaces earlier playback", async () => {
    const speech = harness();
    speech.player.speak("first");
    const first = await started(speech);

    speech.player.speak("second");
    await vi.waitFor(() => expect(speech.audios).toHaveLength(2));

    expect(first.pauses).toBe(1);
    expect(speech.spoken).toEqual(["first", "second"]);
  });

  test("provider failure reports error without calling onEnded", async () => {
    const onEnded = vi.fn();
    const player = createSpeechPlayer({
      createAudio: () => new FakeAudio(),
      createObjectURL: () => "blob:speech",
      onEnded,
      revokeObjectURL: () => undefined,
      synthesize: () => Promise.reject(new Error("provider failed")),
    });

    player.enqueue("hello");
    await vi.waitFor(() => expect(player.state()).toBe("error"));

    expect(onEnded).not.toHaveBeenCalled();
  });

  test("provider failure aborts later prefetched fragments", async () => {
    const signals: AbortSignal[] = [];
    const player = createSpeechPlayer({
      createAudio: () => new FakeAudio(),
      createObjectURL: () => "blob:speech",
      revokeObjectURL: () => undefined,
      synthesize: (text, signal) => {
        signals.push(signal);
        return text === "broken"
          ? Promise.reject(new Error("provider failed"))
          : new Promise<Blob>(() => undefined);
      },
    });

    player.enqueue("broken");
    player.enqueue("prefetched");
    await vi.waitFor(() => expect(player.state()).toBe("error"));

    expect(signals[1].aborted).toBe(true);
  });

  test("audio failure reports error", async () => {
    const speech = harness();
    speech.player.enqueue("hello");
    const audio = await started(speech);

    audio.fail();
    await vi.waitFor(() => expect(speech.player.state()).toBe("error"));
  });

  test("blank text is ignored", () => {
    const speech = harness();

    speech.player.enqueue("   ");

    expect(speech.spoken).toEqual([]);
    expect(speech.player.state()).toBe("idle");
  });

  test("object URLs are revoked after playback", async () => {
    const speech = harness();
    speech.player.enqueue("hello");
    const audio = await started(speech);

    audio.finish();
    await vi.waitFor(() => expect(speech.revoked).toEqual(["blob:speech-1"]));
  });
});
