import { describe, expect, test } from "vitest";

import { injectMemoryContext } from "../src/memory-context.js";

describe("foreground Memory context", () => {
  test("injects current Topics transiently before the latest user Message", () => {
    const messages = [
      { content: "Earlier answer", role: "assistant" as const },
      { content: "What games have I liked?", role: "user" as const },
    ];

    const injected = injectMemoryContext(
      messages,
      "<current_memory>\n## Gaming\nLikes Roboquest.\n</current_memory>",
    );

    expect(injected).toEqual([
      messages[0],
      {
        content:
          "<current_memory>\n## Gaming\nLikes Roboquest.\n</current_memory>",
        role: "user",
        timestamp: 0,
      },
      messages[1],
    ]);
    expect(messages).toHaveLength(2);
  });
});
