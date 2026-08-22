import { expect, test } from "vitest";

import { createRestHost } from "./index";

interface FetchCall {
  init: RequestInit | undefined;
  input: RequestInfo | URL;
}

test("chat speech client posts text and returns provider audio", async () => {
  const calls: FetchCall[] = [];
  const fetch = (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> => {
    calls.push({ init, input });
    return Promise.resolve(
      new Response("provider-audio", {
        headers: { "Content-Type": "audio/mpeg" },
        status: 200,
      }),
    );
  };
  const host = createRestHost({ fetch });
  const controller = new AbortController();

  const audio = await host.chat.synthesizeSpeech(
    "Read this.",
    controller.signal,
  );

  expect(audio.size).toBe(14);
  expect(audio.type).toBe("audio/mpeg");
  expect(calls).toEqual([
    {
      init: {
        body: JSON.stringify({ text: "Read this." }),
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        method: "POST",
        signal: controller.signal,
      },
      input: "/api/tts/speech",
    },
  ]);
});
