import { describe, expect, test } from "vitest";

import { createRestHost } from "./host";
import { createTetherClient } from "./generated";

// Drive the real typed client (matching the house pattern in
// rest-client-smoke.test.ts) with a stub fetch that returns a fixed status,
// so we exercise the actual requireData/requireOk wiring without a live host.
function hostForStatus(status: number) {
  const client = createTetherClient({
    // openapi-fetch builds a Request before delegating to fetch, so it needs an
    // absolute base URL to parse against in the node test environment.
    baseUrl: "http://localhost",
    fetch: () =>
      Promise.resolve(
        new Response(status === 204 ? null : "{}", {
          headers: { "content-type": "application/json" },
          status,
        }),
      ),
  });
  return createRestHost({ client });
}

describe("REST host domain ports", () => {
  test("maps a wrong-password login to friendly text", async () => {
    const host = hostForStatus(401);
    await expect(host.auth.login("nope")).rejects.toThrow(
      "Incorrect password.",
    );
  });

  test("maps a reminder-delete conflict to friendly text", async () => {
    const host = hostForStatus(409);
    await expect(host.triggers.deleteTrigger("trigger-1", 1)).rejects.toThrow(
      "That changed elsewhere. Refresh and try again.",
    );
  });

  test("maps a server error to friendly text without the raw code", async () => {
    const host = hostForStatus(500);
    await expect(host.triggers.listTriggers()).rejects.toThrow(
      "Something went wrong on the server. Please try again.",
    );
  });

  test("maps chat history pagination onto the generated contract", async () => {
    let requestedUrl = "";
    const client = createTetherClient({
      baseUrl: "http://localhost",
      fetch: (request) => {
        requestedUrl = request.url;
        return Promise.resolve(
          new Response("[]", {
            headers: { "content-type": "application/json" },
            status: 200,
          }),
        );
      },
    });
    const host = createRestHost({ client });

    await host.chat.listMessages("conversation-1", {
      beforeSeq: 42,
      limit: 25,
    });

    const url = new URL(requestedUrl);
    expect(url.pathname).toBe("/api/conversations/conversation-1/messages");
    expect(url.searchParams.get("before_seq")).toBe("42");
    expect(url.searchParams.get("limit")).toBe("25");
  });

  test("keeps browser credentials on manual contract adapters", async () => {
    let requestedCredentials: RequestCredentials | undefined;
    const host = createRestHost({
      fetch: (_input, init) => {
        requestedCredentials = init?.credentials;
        return Promise.resolve(
          new Response("[]", {
            headers: { "content-type": "application/json" },
            status: 200,
          }),
        );
      },
    });

    await host.chat.synthesizeSpeech("hello", undefined);

    expect(requestedCredentials).toBe("include");
  });
});
