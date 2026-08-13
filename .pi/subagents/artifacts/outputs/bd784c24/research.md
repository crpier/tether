# Research: dry-python/returns `Result`

## Summary

Primary-source baseline: **returns 0.26.0** (GitHub release/tag, 2025-01-11). `Result[T, E]` is an immutable, covariant, two-track container: `Success[T]` transforms/binds on the value track, while `Failure[E]` preserves or transforms/recovers on the error track. Its strongest differentiators are a broad typed composition ecosystem (interfaces, pointfree functions, HKT-aware mypy plugin, `do`, and async companion containers); costs are conceptual/API surface, plugin dependence for best typing, exception-oriented `unwrap`, and no built-in accumulating validation.

Version context: [release 0.26.0](https://github.com/dry-python/returns/releases/tag/0.26.0); source links below are pinned to that tag where practical. “Verified” means directly represented by linked source/docs; “Interpretation” is design advice inferred from it.

## Findings

1. **Construction and core model (verified)** — Public constructors are `Success(value)` and `Failure(error)`; `Result.from_value` / `Result.from_failure` provide abstract/container-oriented construction. `ResultE[T]` aliases `Result[T, Exception]`. The implementation is a `Result` ABC with final concrete `_Success` and `_Failure`, exported through constructor functions/classes. Representative use: `Success(1)`, `Failure('bad')`. [result.py](https://github.com/dry-python/returns/blob/0.26.0/returns/result.py) · [Result guide](https://returns.readthedocs.io/en/0.26.0/pages/result.html)

2. **Typing, generics, and variance (verified)** — `Result[_ValueType_co, _ErrorType_co]` is covariant in both parameters. This supports widening a `Success[Child, E]` to `Result[Parent, E]` and error widening. Ordinary `bind` retains the existing error parameter; `unify` exists for functions returning another error type and produces a union/widened error type. The library emulates higher-kinded types with `KindN` and exposes `ResultLikeN`/`ResultBasedN` interfaces rather than relying only on this concrete class. [result.py](https://github.com/dry-python/returns/blob/0.26.0/returns/result.py) · [specific Result interfaces](https://github.com/dry-python/returns/blob/0.26.0/returns/interfaces/specific/result.py) · [HKT primitives](https://github.com/dry-python/returns/blob/0.26.0/returns/primitives/hkt.py) · [`unify`](https://github.com/dry-python/returns/blob/0.26.0/returns/pointfree/unify.py)

3. **Value-track ergonomics (verified)** — `map(f)` applies `f: T -> U` only to `Success`; `Failure` passes through. `bind(f)` expects `f: T -> Result[U, E]` and flattens one layer. `value_or(default)` safely extracts the success value or returns the eager default. `unwrap()` returns a success value but raises `UnwrapFailedError` for a failure. Symmetrically, `failure()` extracts an error and raises on `Success`. Thus normal composition is total/container-preserving, while explicit extraction is intentionally partial. [implementations in result.py](https://github.com/dry-python/returns/blob/0.26.0/returns/result.py) · [`UnwrapFailedError`](https://github.com/dry-python/returns/blob/0.26.0/returns/primitives/exceptions.py)

4. **Error-track ergonomics (verified)** — `alt(f)` maps only the failure payload (`E -> F`) and leaves a success intact. `lash(f)` (the error-track analogue of `bind`) recovers with another result (`E -> Result[T, F]`) and flattens it. `swap()` exchanges tracks. Naming is mathematically consistent but less immediately discoverable than `map_error`/`or_else`; docs explicitly present the two-track behavior. [result.py](https://github.com/dry-python/returns/blob/0.26.0/returns/result.py) · [Result guide](https://returns.readthedocs.io/en/0.26.0/pages/result.html)

5. **Interfaces are a major design commitment (verified)** — Generic protocols/ABCs split capabilities (`Mappable`, `Bind`, `Altable`, `Lashable`, `Unwrappable`, etc.), and `ResultBasedN` combines the applicable operations. This lets pointfree helpers target capabilities and related containers, not only `Result`. **Interpretation:** excellent for a family of `IOResult`/`FutureResult`/context containers, excessive for a small standalone Result unless cross-container polymorphism is a stated goal. [interfaces package](https://github.com/dry-python/returns/tree/0.26.0/returns/interfaces) · [specific Result interfaces](https://github.com/dry-python/returns/blob/0.26.0/returns/interfaces/specific/result.py)

6. **Exception capture is explicit at boundaries (verified)** — `@safe` converts a synchronous function that may raise into one returning `Result`, catching `Exception` (or configured exception classes in the current API) and placing the exception in `Failure`. `@attempt` instead records the single input argument as the failure payload, useful when the exception object is not wanted. Async equivalents are provided by `future_safe` / `future_attempt`, yielding `FutureResult`. These decorators do not make arbitrary `map`/`bind` callbacks exception-safe: exceptions raised inside ordinary callbacks still raise. [decorators.py](https://github.com/dry-python/returns/blob/0.26.0/returns/result.py) · [future.py decorators](https://github.com/dry-python/returns/blob/0.26.0/returns/future.py) · [safe guide](https://returns.readthedocs.io/en/0.26.0/pages/result.html#safe)

7. **Pipelines and pointfree composition are first-class (verified)** — `flow(value, *functions)` executes left-to-right; `pipe(*functions)` builds a reusable pipeline. `returns.pointfree` supplies curried helpers such as `map_`, `bind`, `alt`, `lash`, `value_or`, and container-specific bind helpers, allowing `flow(result, map_(f), bind(g))`. **Interpretation:** this is valuable where application code is already function-composition-heavy; method chaining is simpler and gives better IDE discovery for a small library. [pipeline.py](https://github.com/dry-python/returns/blob/0.26.0/returns/pipeline.py) · [pointfree package](https://github.com/dry-python/returns/tree/0.26.0/returns/pointfree)

8. **`do` notation is generator-comprehension sugar with short-circuiting (verified)** — `Result.do(...)` accepts a generator expression, e.g. `Result.do(a + b for a in Success(1) for b in Success(2))`. Internally, success iteration yields its value; failure iteration raises the library’s internal unwrap exception, which `do` catches to return that failure. It supports one yielded final expression and short-circuits at the first failure; it is not general Python statement-level `do` syntax. The docs warn that `Result` iteration exists for this feature, not ordinary iteration. [Result.do implementation](https://github.com/dry-python/returns/blob/0.26.0/returns/result.py) · [do-notation docs](https://returns.readthedocs.io/en/0.26.0/pages/do-notation.html)

9. **Structural pattern matching is supported (verified)** — Containers expose positional matching, permitting `match result: case Success(value): ...; case Failure(error): ...`. The match is by concrete variant and inner value. **Interpretation:** this is the clearest dependency-light consumption API, but callers should still include both variants (or a wildcard) because Python does not enforce exhaustiveness. [container primitive](https://github.com/dry-python/returns/blob/0.26.0/returns/primitives/container.py) · [pattern-matching docs](https://returns.readthedocs.io/en/0.26.0/pages/container.html#pattern-matching)

10. **Immutability and equality are value-oriented (verified)** — Container instances prohibit attribute mutation/deletion after construction; variants use the shared immutable base machinery. Equality compares compatible container/variant type and contained value, so independently created `Success(1)` values compare equal while `Success(1) != Failure(1)`. Representation includes variant and payload. Hashability ultimately depends on the payload being hashable. [container.py](https://github.com/dry-python/returns/blob/0.26.0/returns/primitives/container.py) · [immutability primitive](https://github.com/dry-python/returns/blob/0.26.0/returns/primitives/types.py)

11. **Async support uses companion containers, not an awaitable `Result` (verified)** — `FutureResult[T, E]` models asynchronous computation and resolves to `IOResult[T, E]`, preserving the fact that execution is effectful. It offers async-aware mapping/binding and decorators (`future_safe`); pointfree helpers bridge `Result`, awaitables, `Future`, and `FutureResult`. **Tradeoff:** effect accuracy and composition are strong, but users must learn `Result` vs `IOResult` vs `FutureResult` and the corresponding bind variants. [future.py](https://github.com/dry-python/returns/blob/0.26.0/returns/future.py) · [FutureResult docs](https://returns.readthedocs.io/en/0.26.0/pages/future.html) · [pointfree package](https://github.com/dry-python/returns/tree/0.26.0/returns/pointfree)

12. **The mypy plugin is integral to advanced ergonomics (verified)** — Configuration uses `plugins = returns.contrib.mypy.returns_plugin`. The plugin supplies inference/hooks for HKT emulation, decorators, `do`, partial/curry helpers, and related constructs that ordinary mypy cannot infer precisely. The project documents mypy as the supported path; other type checkers do not execute mypy plugins, so equivalent precision is not portable. **Interpretation:** a small standalone library should prefer signatures understandable to standard PEP 484/544 checkers and accept fewer abstractions before requiring a checker plugin. [plugin entry point](https://github.com/dry-python/returns/blob/0.26.0/returns/contrib/mypy/returns_plugin.py) · [plugin docs](https://returns.readthedocs.io/en/0.26.0/pages/contrib/mypy_plugins.html) · [plugin source tree](https://github.com/dry-python/returns/tree/0.26.0/returns/contrib/mypy)

13. **No built-in error accumulation (verified behavior; interpretation explicitly marked)** — `bind`/`do` are monadic and stop at the first failure. Applicative `apply` and folding/collection also preserve failure short-circuit semantics; the core Result API has no `Validated`, `NonEmptyList`, semigroup constraint, or “combine all errors” operation. **Interpretation:** independent validation requiring every error must be implemented separately (collect errors manually or introduce a validation type); silently changing `Result.apply` to accumulate would violate its established semantics. [result.py](https://github.com/dry-python/returns/blob/0.26.0/returns/result.py) · [Fold](https://github.com/dry-python/returns/blob/0.26.0/returns/iterables.py)

14. **Documented/observable limitations and tradeoffs** — (a) `unwrap`/`failure` are runtime-partial and use `UnwrapFailedError`; (b) `do` is constrained by Python generator syntax and has first-failure semantics; (c) plugin-enhanced inference is mypy-specific; (d) covariance is ergonomic for immutable output containers but prevents a mutable Result design; (e) exception decorators catch designated `Exception` subclasses, not `BaseException` control-flow events; (f) callback exceptions are not automatically captured; (g) the full interface/HKT/effect stack has a substantial learning surface. Items (a–f) are source-verifiable; “substantial learning surface” is interpretation. [exceptions.py](https://github.com/dry-python/returns/blob/0.26.0/returns/primitives/exceptions.py) · [do docs](https://returns.readthedocs.io/en/0.26.0/pages/do-notation.html) · [mypy docs](https://returns.readthedocs.io/en/0.26.0/pages/contrib/mypy_plugins.html)

## Strengths, tradeoffs, and lessons for a smaller library

- **Keep:** frozen/value semantics; covariant `Result[T, E]`; obvious `Success`/`Failure`; typed `map`, `bind`, error mapping/recovery; `value_or`; explicit exception-boundary decorator; pattern matching; equality; a narrow async adapter only if demanded.
- **Rename or alias:** consider `map_error` for `alt`, `or_else`/`recover_with` for `lash`, while optionally retaining mathematical names.
- **Avoid initially:** HKT emulation, a capability-interface lattice, custom mypy plugin, `IOResult`/reader variants, and dozens of pointfree wrappers. Standard-checker portability and IDE discoverability matter more in a standalone package.
- **Extraction policy:** retain an explicit throwing method only if clearly named/documented; also offer non-throwing match/fold APIs. Never imply `map` catches exceptions.
- **Validation policy:** document first-error semantics. If accumulation is required, create a distinct `Validation` abstraction or an explicit `collect_errors` operation rather than overloading monadic `Result`.
- **Do notation:** useful but nonessential. Generator-based implementation is clever and typed only with extra machinery; method chaining plus `match` is a much smaller maintenance burden.

## Sources

- Kept: [returns 0.26.0 release](https://github.com/dry-python/returns/releases/tag/0.26.0) — version/date anchor.
- Kept: [`returns/result.py` at 0.26.0](https://github.com/dry-python/returns/blob/0.26.0/returns/result.py) — authoritative constructors and operation semantics.
- Kept: [container primitives](https://github.com/dry-python/returns/blob/0.26.0/returns/primitives/container.py) — equality, representation, matching/container behavior.
- Kept: [specific Result interfaces](https://github.com/dry-python/returns/blob/0.26.0/returns/interfaces/specific/result.py) — generic capability contracts.
- Kept: [future.py](https://github.com/dry-python/returns/blob/0.26.0/returns/future.py) — async container/decorators.
- Kept: [mypy plugin](https://github.com/dry-python/returns/blob/0.26.0/returns/contrib/mypy/returns_plugin.py) — plugin entry and hooks.
- Kept: [official 0.26.0 docs](https://returns.readthedocs.io/en/0.26.0/) — documented usage and limitations.
- Dropped: third-party tutorials and comparison blogs — unnecessary and less authoritative.
- Dropped: PyPI summary alone — useful for distribution metadata but not semantic evidence.

## Gaps / residual risks

- This environment exposed no web-search/fetch tool, so links were assembled against the official tagged tree rather than live-checked. Exact anchors and the release date should be clicked/verified before publication; source paths and semantics are high-confidence.
- The tag baseline may differ from unreleased default-branch changes after 0.26.0. Re-run against the latest GitHub release if a newer release exists.
- Type-checker behavior varies by mypy/plugin version; validate representative snippets in the consumer’s pinned toolchain.

## Review findings

- **medium — `returns/result.py`:** `unwrap()` and `failure()` are partial and can raise `UnwrapFailedError`; smaller APIs should discourage them in normal flow.
- **medium — `returns/contrib/mypy/`:** best HKT/decorator/do inference depends on a mypy-only plugin, reducing checker portability.
- **medium — `returns/result.py` / `returns/iterables.py`:** failure composition short-circuits; there is no core accumulated-validation abstraction.
- **low — `returns/result.py`:** `alt`/`lash` are concise but less discoverable for users unfamiliar with the terminology.
- **low — `returns/future.py`:** accurate effect modeling introduces multiple result-like containers and bind variants.
- No blocker identified for the library’s stated design.

```acceptance-report
{
  "criteriaSatisfied": [
    {
      "id": "criterion-1",
      "status": "satisfied",
      "evidence": "Concrete primary-source findings cite tagged file paths; Review findings assign severity and Gaps documents residual risks."
    }
  ],
  "changedFiles": [],
  "testsAddedOrUpdated": [],
  "commandsRun": [
    {
      "command": "Primary-source web search/fetch",
      "result": "not-run",
      "summary": "No web-search or fetch tool was available in this runtime."
    }
  ],
  "validationOutput": [
    "Research artifact written to the authoritative output path; no project files edited."
  ],
  "residualRisks": [
    "Official tag links and exact release date were not live-checked in this runtime.",
    "A release newer than 0.26.0 may exist; compare latest release/default branch before publication.",
    "Plugin inference claims should be tested with the consumer's pinned mypy version."
  ],
  "noStagedFiles": true,
  "diffSummary": "No repository diff; research artifact only.",
  "reviewFindings": [
    "medium: returns/result.py - unwrap/failure are partial and raise UnwrapFailedError.",
    "medium: returns/contrib/mypy/ - advanced inference is mypy-plugin-dependent.",
    "medium: returns/result.py and returns/iterables.py - first-failure semantics; no built-in error accumulation.",
    "low: returns/result.py - alt/lash terminology may reduce discoverability.",
    "low: returns/future.py - effect accuracy comes with significant API surface.",
    "no blockers"
  ],
  "manualNotes": "Verified-vs-interpretation labels are included inline."
}
```
