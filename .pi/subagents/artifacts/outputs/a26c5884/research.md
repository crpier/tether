# Research: Better Auth functional composition and errors-as-values

## Summary

At Better Auth **v1.6.27**, commit **`be47e9418b4a25a4ecd51ba781d2296373b65a03`**, the browser client is Result-like (`{ data, error }`) by default, while direct server `auth.api.*` calls return values and **throw** `APIError`; `asResponse: true` converts the same operation into a `Response`. Composition is endpoint/middleware/hook/plugin based, with unusually strong route/schema inference, but there is no general `Result` algebra (`map`, `andThen`, exhaustive variants) in the reviewed Better Auth sources. This report used the official repository tag checkout and docs at that commit; no broader live-web verification was available.

## Findings

1. **[informational] Public behavior deliberately differs by boundary.** Official client docs say most client actions return `{ data, error }`; server docs say `auth.api.*` throws `APIError` on failure. Source confirms `dispatchAuthEndpoint()` catches endpoint `APIError`, carries it through after-hooks as a value, then either serializes it when a `Response` is requested or rethrows it for non-response direct calls:
   ```ts
   if (isAPIError(result.response) && !shouldReturnResponse) {
     throw result.response;
   }
   return shouldReturnResponse ? toResponse(...) : result.response;
   ```
   Representative client return type is `Promise<BetterFetchResponse<Data, Error, ThrowFlag>>`; `FetchOptions["throw"]` or global `fetchOptions.throw` controls its third generic. This is a pragmatic values-at-I/O / exceptions-in-trusted-server split, not one uniform Result contract. Strength: idiomatic ergonomics per environment. Tradeoff: the same logical operation changes failure semantics by entry point and flags. [Server docs](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/docs/content/docs/concepts/api.mdx) · [client docs](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/docs/content/docs/concepts/client.mdx) · [`dispatch.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/better-auth/src/api/dispatch.ts) · [`path-to-object.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/better-auth/src/client/path-to-object.ts)

2. **[informational] Errors are structured exceptions, with stable symbolic codes.** `@better-auth/core` subclasses better-call's `APIError`, retaining `status`, `statusCode`, `body`, `headers`, `message`, and `errorStack`; helpers are `APIError.fromStatus(status, body?)` and `APIError.from(status, { code, message })`. `BetterAuthError` is a separate configuration/internal exception and intentionally clears `stack`. Base domain codes use uppercase snake case (`INVALID_PASSWORD`, `SESSION_EXPIRED`, etc.); `defineErrorCodes()` enforces that naming at compile time and produces `{ readonly code, message, toString() }`. This is stronger than stringly errors but remains open (`code: string`) at some constructors/boundaries. [`error/index.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/core/src/error/index.ts) · [`error/codes.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/core/src/error/codes.ts) · [`error-codes.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/core/src/utils/error-codes.ts)

3. **[tradeoff] `isAPIError` favors cross-package/runtime resilience over strict nominal identity.** It accepts `instanceof` either better-call or Better Auth `APIError`, plus any object whose `name === "APIError"`. That handles duplicate package copies/realms, but name-based acceptance can misclassify arbitrary objects. Lesson: a Python library should prefer a closed dataclass/Protocol discriminator for values, retaining an explicit adapter for foreign exceptions rather than loose name matching. [`is-api-error.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/core/src/utils/is-api-error.ts)

4. **[informational] Internally, errors temporarily become values to make hooks composable.** The dispatcher catches handler `APIError` into `{ response: e, status, headers }`; after-hooks inspect/replace `ctx.context.returned`, which may be success or `APIError`. An after-hook's thrown `APIError` is likewise caught and fed to later hooks. Before-hooks may return `{ context: ... }` to transform input or any other object to short-circuit. Headers are separately accumulated and merged, with special append behavior for `set-cookie`. This resembles an implicit state-and-error pipeline, but its union is `unknown` and discrimination is runtime (`isAPIError`) rather than an explicit tagged Result. [`dispatch.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/better-auth/src/api/dispatch.ts) · [hook docs](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/docs/content/docs/concepts/hooks.mdx) · [`call.test.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/better-auth/src/call.test.ts)

5. **[informational] Hooks/plugins form a declarative composition surface.** `BetterAuthPlugin` requires `id` and may contribute `init`, `endpoints`, route `middlewares`, `hooks.before/after`, `onRequest`, `onResponse`, schema/migrations, adapter overrides, rate limits, `$Infer`, and `$ERROR_CODES`. Request hooks explicitly return `{ response } | { request } | void`; response hooks return `{ response } | void`. Plugin hooks use matcher/handler pairs; user hooks run first, then plugin hooks. Direct plain endpoint invocation skips hooks, while `dispatchAuthEndpoint()` intentionally re-enters the canonical pipeline. Strength: features are data objects and factory functions, naturally composable. Tradeoff: many optional extension points and distinct early-return shapes increase cognitive load. [`plugin.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/core/src/types/plugin.ts) · [`dispatch.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/better-auth/src/api/dispatch.ts) · [plugin docs](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/docs/content/docs/concepts/plugins.mdx)

6. **[informational] Endpoint creation is typed functional construction.** `createAuthEndpoint(path, options, handler)` returns `StrictEndpoint<Path, Options, R>`; a pathless overload and explicit `createAuthEndpoint.serverOnly(options, handler)` exist. The wrapper installs `AuthContext`, AsyncLocalStorage endpoint context, and preserves response headers on thrown API errors via better-call's `kAPIErrorHeaderSymbol`. `getCurrentAuthContext()` throws when used outside the dynamic scope—another clear exceptional programmer-error boundary. [`api/index.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/core/src/api/index.ts) · [`endpoint-context.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/core/src/context/endpoint-context.ts)

7. **[informational] Type inference is extensive and convention-driven.** Plugin endpoints are intersected into `auth.api`; client paths become nested camelCase methods (`/my-plugin/hello-world` → `myPlugin.helloWorld`); Standard Schema endpoint error schemas infer client error output; plugin schemas extend user/session returns; `$Infer` exposes inferred session/plugin types. Metadata excludes `SERVER_ONLY`, HTTP-only, and non-action endpoints from client routes. Strength: minimal glue and accurate autocomplete. Tradeoffs: advanced conditional/intersection types are hard to read/debug, depend on literal preservation (`satisfies`, `as const`), and require strict null checking. [`api/index.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/better-auth/src/api/index.ts) · [`path-to-object.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/better-auth/src/client/path-to-object.ts) · [`types/api.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/better-auth/src/types/api.ts) · [TypeScript docs](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/docs/content/docs/concepts/typescript.mdx)

8. **[informational] Naming conventions optimize discoverability.** Server method keys are camelCase (`signInEmail`), client URLs are kebab-case and projected to nested camelCase (`signIn.email`), reactive hooks begin `use`, type-only inference uses `$Infer`, and constants/registries use `$ERROR_CODES`. Plugin docs recommend factories, one data argument plus optional fetch options, and `{ data, error }` results. The `$` names clearly mark meta/tooling surfaces, though they are framework-specific magic rather than general language concepts. [plugin docs](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/docs/content/docs/concepts/plugins.mdx) · [client docs](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/docs/content/docs/concepts/client.mdx)

9. **[tradeoff] No general Result utility was verified in Better Auth/core; better-call supplies transport primitives, not a visible Result algebra here.** Reviewed source imports better-call `APIError`, endpoint/middleware/router creators, `toResponse`, and error-header symbols. Workspace catalog pins **better-call 1.4.0**, and Better Auth pins **@better-fetch/fetch 1.3.1**. The source checkout did not include dependency source, so claims about their internals beyond these consumed exports would be speculation. The only verified Result-like abstraction is `BetterFetchResponse<Data, Error, ThrowFlag>` at the client type boundary. [`pnpm-workspace.yaml`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/pnpm-workspace.yaml) · [`core error/index.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/core/src/error/index.ts) · [`path-to-object.ts`](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/better-auth/src/client/path-to-object.ts)

## Lessons for an ergonomic Python Result library

- Make one explicit `Result[T, E]` invariant; offer named adapters such as `from_exception`, `unwrap`, and `to_response` rather than silently changing semantics by call mode.
- Use frozen, tagged error values with stable uppercase codes, message, metadata/cause, and exhaustive pattern matching; keep programmer/configuration faults exceptional.
- Preserve Better Auth's excellent functional construction: decorators/factories should infer input/output/error types and compose middleware without inheritance.
- Model hook control flow explicitly (`Continue(context)`, `Stop(result)`) instead of unions of arbitrary objects/`None`; thread headers/state separately and immutably where practical.
- Provide the algebra Better Auth lacks: `map`, `map_err`, `and_then`, async equivalents, collection/traversal helpers, and typing-friendly narrowing.
- Keep names unsurprising in Python (`Ok`, `Err`, `Result`, `is_ok`); reserve magic/meta fields only when they materially improve inference.
- Let typed schemas define both success and error payloads. Avoid an unbounded `str` code escape hatch unless explicitly represented as `UnknownError`.

## Sources

- Kept: Better Auth repository v1.6.27 package manifest and changelog ([manifest](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/better-auth/package.json), [changelog](https://github.com/better-auth/better-auth/blob/be47e9418b4a25a4ecd51ba781d2296373b65a03/packages/better-auth/CHANGELOG.md)) — establishes version and package context.
- Kept: Core error, API, context, and plugin source (links above) — authoritative runtime/type contracts.
- Kept: Better Auth dispatch, API inference, client inference, and tests (links above) — authoritative boundary behavior plus representative tests.
- Kept: Official repository docs for API, client, hooks, plugins, and TypeScript (links above) — public conventions and promised ergonomics.
- Dropped: Third-party articles/search summaries — unnecessary and less authoritative.
- Dropped: better-call and better-fetch implementation claims — dependency source was unavailable in the supplied checkout; only Better Auth's imports/types and pinned versions were retained.

## Gaps / residual risks

- No live-web search or upstream dependency checkout was available. Permalinks target the supplied official commit and should be stable, but current `main` behavior may differ.
- better-call 1.4.0 and @better-fetch/fetch 1.3.1 internals were not independently inspected; their exact `APIError` constructor/body and `BetterFetchResponse` field definitions remain unverified here.
- No release date was present in the inspected manifest/changelog excerpt; version + immutable commit are the attested context.

```acceptance-report
{
  "criteriaSatisfied": [
    {
      "id": "criterion-1",
      "status": "satisfied",
      "evidence": "Nine concrete review findings identify severity, repository file paths/permalinks, representative signatures/behavior, tradeoffs, and residual risks for Better Auth v1.6.27 at commit be47e9418b4a25a4ecd51ba781d2296373b65a03."
    }
  ],
  "changedFiles": [
    "/home/crpier/Projects/tether/.pi/subagents/artifacts/outputs/a26c5884/research.md"
  ],
  "testsAddedOrUpdated": [],
  "commandsRun": [
    {
      "command": "Read supplied Better Auth v1.6.27 primary-source checkout",
      "result": "passed",
      "summary": "Inspected official error, dispatch, endpoint, plugin, inference, test, docs, manifest, changelog, and workspace version files."
    }
  ],
  "validationOutput": [
    "Source context attested as v1.6.27, commit be47e9418b4a25a4ecd51ba781d2296373b65a03.",
    "No project source files edited; only the required research artifact was written."
  ],
  "residualRisks": [
    "No live-web verification; current main may differ from the pinned commit.",
    "better-call and better-fetch dependency internals were unavailable, so only consumed exports and pinned versions are verified."
  ],
  "noStagedFiles": true,
  "diffSummary": "Added only the requested research artifact; no product code or tests changed.",
  "reviewFindings": [
    "informational: packages/better-auth/src/api/dispatch.ts - direct server calls throw APIError, while response mode serializes errors and internal hooks temporarily carry them as values.",
    "informational: packages/better-auth/src/client/path-to-object.ts - client calls expose BetterFetchResponse<Data, Error, ThrowFlag>, a Result-like boundary with opt-in throwing.",
    "tradeoff: packages/core/src/utils/is-api-error.ts - name-based fallback improves cross-runtime recognition but can misclassify foreign objects.",
    "tradeoff: reviewed sources - no general Result combinator algebra was verified; no blocker for research completeness."
  ],
  "manualNotes": "Research was constrained to the supervisor-supplied official local tag checkout; all interpretations are labeled separately from verified behavior."
}
```
