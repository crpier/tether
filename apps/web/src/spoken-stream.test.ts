import { describe, expect, test } from "vitest";

import { createSpokenStream } from "./spoken-stream";

describe("spoken stream (#545)", () => {
  test("emits complete sentences as deltas arrive", () => {
    const emitted: string[] = [];
    const stream = createSpokenStream((sentence) => {
      emitted.push(sentence);
    });

    stream.push("Hello there. ");
    expect(emitted).toEqual(["Hello there. "]);

    stream.push("How are");
    expect(emitted).toEqual(["Hello there. "]);

    stream.push(" you?\n");
    expect(emitted).toEqual(["Hello there. ", "How are you?\n"]);
  });

  test("holds a trailing incomplete sentence until it completes", () => {
    const emitted: string[] = [];
    const stream = createSpokenStream((sentence) => {
      emitted.push(sentence);
    });

    stream.push("One two three");
    expect(emitted).toEqual([]);

    stream.push(" four.");
    expect(emitted).toEqual(["One two three four."]);
  });

  test("handles abbreviations conservatively (splits after any period+space)", () => {
    const emitted: string[] = [];
    const stream = createSpokenStream((sentence) => {
      emitted.push(sentence);
    });

    stream.push("See e.g. the docs. Done.");
    // MVP trade-off: "e.g. " splits too — acceptable for speech cadence.
    expect(emitted).toEqual(["See e.g. ", "the docs. ", "Done."]);
  });

  test("tail returns everything not yet emitted", () => {
    const emitted: string[] = [];
    const stream = createSpokenStream(() => undefined);
    stream.push("First sentence. Second sen");

    const tail = stream.tail("First sentence. Second sentence.");

    expect(emitted).toEqual([]);
    expect(tail).toBe("Second sentence.");
  });

  test("tail returns the full text when nothing was emitted", () => {
    const stream = createSpokenStream(() => undefined);

    expect(stream.tail("All of it.")).toBe("All of it.");
  });

  test("restart invalidates the prefix so tail is the whole final text", () => {
    let restarted = false;
    const stream = createSpokenStream(
      () => undefined,
      () => {
        restarted = true;
      },
    );
    stream.push("Pre-tool prose. ");
    stream.restart();

    expect(restarted).toBe(true);
    expect(stream.tail("Post-tool answer.")).toBe("Post-tool answer.");
  });

  test("reset clears buffer and emitted prefix", () => {
    const emitted: string[] = [];
    const stream = createSpokenStream((sentence) => {
      emitted.push(sentence);
    });
    stream.push("Old turn text. ");

    stream.reset();
    stream.push("New turn. ");
    void emitted;

    expect(stream.tail("New turn. Tail.")).toBe("Tail.");
  });
});
