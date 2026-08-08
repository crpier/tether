import { createTetherClient } from "../generated";
import type { TetherClient } from "../generated";
import type { HttpStatusMessages } from "../lib/http-errors";
import { ApiError } from "./error";

export interface RestHostDependencies {
  client?: TetherClient;
  fetch?: typeof globalThis.fetch;
}

export interface RestContext {
  client: TetherClient;
  fetch: typeof globalThis.fetch;
}

export function createRestContext(
  dependencies: RestHostDependencies = {},
): RestContext {
  return {
    client: dependencies.client ?? createTetherClient(),
    // Keep the browser's native fetch receiver. Storing `window.fetch` directly
    // and later calling it as `context.fetch(...)` gives it the wrong `this`
    // in Chromium, so the request fails before reaching Playwright or the host.
    fetch:
      dependencies.fetch ?? ((input, init) => globalThis.fetch(input, init)),
  };
}

export function requireData<T>(
  data: T | undefined,
  response: Response,
  messages?: HttpStatusMessages,
): T {
  if (!response.ok) {
    throw new ApiError(response.status, messages);
  }
  if (data === undefined) {
    throw new Error("Request returned no data");
  }
  return data;
}

export function requireOk(
  response: Response,
  messages?: HttpStatusMessages,
): void {
  if (!response.ok) {
    throw new ApiError(response.status, messages);
  }
}
