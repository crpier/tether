# Result design research

These notes preserve the primary-source research that informed snekok. They are
a design reference, not snekok's public contract; the contract lives in
[`../result.md`](../result.md).

## Snapshots

| Reference | Pinned source | Best used for |
| --- | --- | --- |
| [`dmmulroy/better-result`](better-result.md) | 3.0.1, `75d0b106` | Result algebra, typed errors, generator composition, async, and transport tradeoffs |
| [`dry-python/returns`](returns.md) | 0.29.0, `cf5e2548` | Result algebra, typing, exception capture, pattern matching, and complexity tradeoffs |

## Quick comparison

| Concern | `better-result` | `returns` | Current snekok direction |
| --- | --- | --- | --- |
| Core shape | `Ok<T, E> | Err<T, E>` concrete classes | `Success[T]` / `Failure[E]` container | `Ok[T] | Err[E]` |
| Expected failures | Explicit `Err`; extraction may throw | Explicit `Failure`; extraction may throw | Expected failures are values; programmer faults remain exceptions |
| Composition | `map`, `mapError`, `andThen`, recovery, generator `yield*` | `map`, `bind`, `alt`, `lash`, pipelines | Add only operations demanded by real refactors |
| Exception capture | Explicit adapters; callback defects become `Panic` | Explicit `safe` / `future_safe` adapters | Never catch implicitly in `map` or composition |
| Async | Ordinary `Promise<Result>` plus helpers | Separate `FutureResult`/`IOResult` stack | Prefer async functions returning ordinary `Result` initially |
| Typing | Conditional types and overloads; no plugin | Covariance, HKT emulation, mypy plugin | Covariant, Pyright-native types without plugins |
| Validation | First error or partition; no accumulation type | First failure; no core accumulation type | Keep fail-fast semantics; separate validation if needed |
| Pattern matching | Discriminant, `match`, and tagged errors | Structural matching supported | Structural matching is the initial consumption API |

## Using these notes

Before adding a snekok operation:

1. Start from a concrete consumer refactor and its public test seam.
2. Check how both references name and type the operation.
3. Preserve explicit exception boundaries and ordinary Python discoverability.
4. Prefer a small method or function over HKT, point-free, or effect-container
   machinery.
5. Record deliberate divergences in the relevant research note or public
   contract.

Re-check upstream before relying on implementation details: these are immutable,
pinned snapshots, not claims about the projects' latest releases.
