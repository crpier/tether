import { describe, expect, test } from "vitest";

import { createRestApi } from "./api";
import type { TetherClient } from "./generated";

function clientReturning(status: number): TetherClient {
  const result = {
    data: undefined,
    error: undefined,
    response: new Response(null, { status }),
  };
  const handler = () => Promise.resolve(result);
  return {
    GET: handler,
    POST: handler,
    PUT: handler,
    PATCH: handler,
    DELETE: handler,
    HEAD: handler,
    OPTIONS: handler,
    TRACE: handler,
  } as unknown as TetherClient;
}

describe("createRestApi error messages", () => {
  test("maps a wrong-password login to friendly text", async () => {
    const api = createRestApi(clientReturning(401));
    await expect(api.login("nope")).rejects.toThrow("Incorrect password.");
  });

  test("maps a reminder-delete conflict to friendly text", async () => {
    const api = createRestApi(clientReturning(409));
    await expect(api.deleteTrigger("trigger-1", 1)).rejects.toThrow(
      "That changed elsewhere. Refresh and try again.",
    );
  });

  test("never surfaces the raw status code", async () => {
    const api = createRestApi(clientReturning(500));
    await expect(api.listTriggers()).rejects.toThrow(
      "Something went wrong on the server. Please try again.",
    );
    await expect(api.listTriggers()).rejects.not.toThrow("500");
  });
});
