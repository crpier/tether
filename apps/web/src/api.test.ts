import { describe, expect, test } from "vitest";

import { createRestApi } from "./api";
import { createTetherClient } from "./generated";

// Drive the real typed client (matching the house pattern in
// rest-client-smoke.test.ts) with a stub fetch that returns a fixed status,
// so we exercise the actual requireData/requireOk wiring without a live host.
function apiForStatus(status: number) {
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
  return createRestApi(client);
}

describe("createRestApi error messages", () => {
  test("maps a wrong-password login to friendly text", async () => {
    const api = apiForStatus(401);
    await expect(api.login("nope")).rejects.toThrow("Incorrect password.");
  });

  test("maps a reminder-delete conflict to friendly text", async () => {
    const api = apiForStatus(409);
    await expect(api.deleteTrigger("trigger-1", 1)).rejects.toThrow(
      "That changed elsewhere. Refresh and try again.",
    );
  });

  test("maps a server error to friendly text without the raw code", async () => {
    const api = apiForStatus(500);
    await expect(api.listTriggers()).rejects.toThrow(
      "Something went wrong on the server. Please try again.",
    );
  });
});
