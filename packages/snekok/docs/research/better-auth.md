# Better Auth: errors as values at API boundaries

Research snapshot: Better Auth **v1.6.27**, commit
[`be47e941`](https://github.com/better-auth/better-auth/tree/be47e9418b4a25a4ecd51ba781d2296373b65a03).
Verified behavior is separated from snekok-oriented interpretation below.

## Executive summary

Better Auth is a boundary-design reference, not a complete Result algebra:

- Browser actions normally return `{data, error}`.
- Client callers may opt into throwing with `throw: true`.
- Direct server `auth.api.*` calls return values and throw `APIError`.
- Internally, dispatch temporarily carries API errors as values so hooks can
  inspect or replace them.
- Errors expose stable symbolic codes separately from display messages.
- Route, schema, plugin, and error types are inferred aggressively.
- A CLI helper contains a small explicit `Result<T, E>` union and `tryCatch`,
  but no pervasive `map`/`andThen` algebra was found in core APIs.

Its main lesson is to make common boundaries low-friction while keeping failure
information structured. Its main warning is that one logical operation can
change failure semantics by entry point or option.

## Result-like client boundary

Most browser actions expose a Better Fetch response with mutually exclusive data
and error channels. The return type tracks whether throwing is enabled:

```ts
Promise<BetterFetchResponse<Data, Error, ThrowFlag>>
```

The endpoint's Standard Schema error output can flow into the client error type.
This gives callers useful autocomplete without requiring a framework-wide Result
container.

`FetchOptions["throw"]`, or a global fetch option, changes the client contract.
That is convenient for consumers integrating with exception-oriented code, but
it means failure behavior is configuration-sensitive.

Sources: [client documentation](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/docs/content/docs/concepts/client.mdx),
[`path-to-object.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/better-auth/src/client/path-to-object.ts),
[`query.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/better-auth/src/client/query.ts).

## Server and response boundaries

Direct server calls throw `APIError`. The same dispatcher can instead serialize
an error when a `Response` is requested:

```ts
if (isAPIError(result.response) && !shouldReturnResponse) {
  throw result.response;
}
return shouldReturnResponse ? toResponse(...) : result.response;
```

This is a pragmatic split:

- trusted server calls use conventional exceptions;
- transport-facing calls produce HTTP responses;
- browser calls expose data and error values by default.

For snekok, prefer one stable core `Result` meaning and named adapters at
boundaries rather than a flag that changes the core operation's behavior.

Sources: [server API documentation](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/docs/content/docs/concepts/api.mdx),
[`dispatch.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/better-auth/src/api/dispatch.ts).

## Small explicit CLI Result

Better Auth's CLI contains the clearest standalone Result-like utility found in
the repository:

```ts
type Success<T> = {
  data: T;
  error: null;
};

type Failure<E> = {
  data: null;
  error: E;
};

export type Result<T, E = Error> = Success<T> | Failure<E>;
```

Its `tryCatch` awaits a promise and converts rejection into the failure branch:

```ts
export async function tryCatch<T, E = Error>(
  promise: Promise<T>,
): Promise<Result<T, E>> {
  try {
    return { data: await promise, error: null };
  } catch (error) {
    return { data: null, error: error as E };
  }
}
```

This utility optimizes simple destructuring and TypeScript narrowing. It does
not provide mapping, binding, recovery, traversal, or validation accumulation.
The generic `E` is asserted at the catch site rather than derived from the
runtime exception; a Python adapter should default to `Exception` or require an
explicit error mapper instead of promising an arbitrary error type.

Source: [`packages/cli/src/utils/helper.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/cli/src/utils/helper.ts).

## Structured errors

`@better-auth/core` extends better-call's `APIError`. Errors retain HTTP and
presentation details such as status, body, headers, message, and stack data.
Helpers include `APIError.fromStatus(...)` and `APIError.from(...)`.

Domain codes use stable uppercase snake case, for example
`INVALID_PASSWORD` and `SESSION_EXPIRED`. `defineErrorCodes()` enforces the
naming convention at compile time and associates each code with a message.

Useful snekok lesson: error values should be able to carry stable identity
separately from human-readable presentation. Do not force every error payload to
be an exception or an unstructured string.

Sources: [`error/index.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/core/src/error/index.ts),
[`error/codes.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/core/src/error/codes.ts),
[`error-codes.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/core/src/utils/error-codes.ts).

## Errors temporarily become hook values

The dispatcher catches handler `APIError` instances into an internal response
record. After-hooks may inspect or replace the returned success or error. An
after-hook's own `APIError` is similarly caught so later hooks can observe it.
Before-hooks may transform context or short-circuit the pipeline.

This resembles a state-and-error pipeline but uses runtime discrimination over
an `unknown` value rather than an explicit tagged Result. Headers are accumulated
separately, including special append behavior for `set-cookie`.

If snekok later supports middleware control flow, explicit values such as
`Continue(context)` and `Stop(result)` would be clearer than unrelated object
shapes and `None`.

Sources: [`dispatch.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/better-auth/src/api/dispatch.ts),
[hook documentation](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/docs/content/docs/concepts/hooks.mdx).

## Plugin and inference ergonomics

Plugins declaratively contribute endpoints, middleware, hooks, schema,
migrations, rate limits, inferred types, and error-code registries. Endpoint
paths and schemas project into strongly typed server and client actions. Naming
conventions make generated surfaces discoverable:

- server keys are camelCase;
- URL segments are kebab-case;
- client paths become nested camelCase methods;
- reactive hooks begin with `use`;
- `$Infer` and `$ERROR_CODES` identify type/tooling surfaces.

The strength is low-friction autocomplete. The tradeoff is a large body of
conditional and intersection types whose behavior depends on literal
preservation and strict null checking.

Sources: [`plugin.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/core/src/types/plugin.ts),
[`api/index.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/core/src/api/index.ts),
[plugin documentation](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/docs/content/docs/concepts/plugins.mdx).

## Recognition tradeoff

`isAPIError` accepts nominal instances and objects whose `name` is
`"APIError"`. The fallback survives duplicate packages or runtime realms, but
can misclassify unrelated objects.

Python code should prefer closed variants or a precise Protocol discriminator,
with explicit adapters for foreign exceptions rather than loose name matching.

Source: [`is-api-error.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/core/src/utils/is-api-error.ts).

## Ideas to retain or avoid

Retain:

- ergonomic errors-as-values at consumer boundaries;
- stable machine-readable error identity;
- type inference from the success and error schemas;
- factory/decorator composition without inheritance;
- explicit programmer-error and configuration-error exception boundaries.

Avoid copying directly:

- behavior-changing throw flags in the core Result API;
- runtime unions of arbitrary objects and `None` for control flow;
- loose error recognition by class name;
- TypeScript-specific meta naming where ordinary Python names suffice.

## Research limits

- The snapshot is pinned; current Better Auth may differ.
- better-call 1.4.0 and `@better-fetch/fetch` 1.3.1 internals were not
  independently inspected.
- The findings distinguish repository behavior from snekok-oriented
  interpretation; proposed operations are not commitments.
