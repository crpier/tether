import { describe, expect, test } from "vitest";

import { toSpeechText } from "./speech-text";

describe("speech text normalization", () => {
  test("plain prose passes through trimmed", () => {
    expect(toSpeechText("Hello there.\n\nSecond paragraph.")).toBe(
      "Hello there.\n\nSecond paragraph.",
    );
  });

  test.each([
    ["# Title", "Title"],
    ["## Subtitle", "Subtitle"],
    ["### Deep **heading**", "Deep heading"],
  ])("heading syntax is stripped but words remain (%s)", (input, expected) => {
    expect(toSpeechText(input)).toBe(expected);
  });

  test.each([
    ["**bold** and *italic*", "bold and italic"],
    ["_under_ and __double__", "under and double"],
    ["a `code span` stays", "a code span stays"],
    ["~~struck~~", "struck"],
  ])("emphasis markers are stripped (%s)", (input, expected) => {
    expect(toSpeechText(input)).toBe(expected);
  });

  test("links become their readable labels", () => {
    expect(
      toSpeechText("See [the docs](https://example.com/x) for more."),
    ).toBe("See the docs for more.");
  });

  test("images are dropped entirely", () => {
    expect(toSpeechText("Before ![chart](https://x/y.png) after")).toBe(
      "Before after",
    );
  });

  test("bare URLs are omitted", () => {
    expect(
      toSpeechText("Visit https://example.com/very/long/path today."),
    ).toBe("Visit today.");
  });

  test("fenced code bodies are omitted, surrounding prose kept", () => {
    expect(
      toSpeechText(
        "Here is how it works:\n\n```python\ndef f(x):\n    return x + 1\n```\n\nThat is the whole idea.",
      ),
    ).toBe("Here is how it works:\n\nThat is the whole idea.");
  });

  test("widget fences (mermaid, vega-lite, artifact) are omitted", () => {
    for (const language of ["mermaid", "vega-lite", "artifact"]) {
      expect(
        toSpeechText(
          `Flow:\n\n\`\`\`${language}\ngraph TD; A-->B;\n\`\`\`\nDone.`,
        ),
      ).toBe("Flow:\n\nDone.");
    }
  });

  test("table separator rows are dropped and cells are spoken as prose", () => {
    expect(
      toSpeechText(
        "| Name | Age |\n| --- | --- |\n| Ada | 36 |\n| Grace | 45 |",
      ),
    ).toBe("Name, Age\nAda, 36\nGrace, 45");
  });

  test("list markers are stripped but items remain", () => {
    expect(toSpeechText("- first\n- second\n1. third")).toBe(
      "first\nsecond\nthird",
    );
  });

  test("blockquote markers are stripped", () => {
    expect(toSpeechText("> quoted wisdom\n> more")).toBe("quoted wisdom\nmore");
  });

  test("horizontal rules are dropped", () => {
    expect(toSpeechText("Above\n\n---\n\nBelow")).toBe("Above\n\nBelow");
  });

  test("pathological whitespace collapses", () => {
    expect(toSpeechText("Words   with\t\tgaps\n\n\n\nand  breaks")).toBe(
      "Words with gaps\n\nand breaks",
    );
  });

  test.each(["", "   \n\n  ", "---\n***\n___", "```js\nonly();\n```"])(
    "blank or markup-only output yields no playback (%j)",
    (input) => {
      expect(toSpeechText(input)).toBe("");
    },
  );

  test("a table-only message degrades to spoken cells", () => {
    expect(toSpeechText("| a | b |\n| - | - |")).toBe("a, b");
  });
});
