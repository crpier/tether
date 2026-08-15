# `dry-python/returns`: Result and functional composition

Curated against `returns` **0.29.0**, commit
[`cf5e2548`](https://github.com/dry-python/returns/tree/cf5e25485921bd02dea637e6f12c163128ba295c).
The retained subagent report used 0.26.0; the core claims below were reconciled
with the separately inspected 0.29.0 checkout. Re-check current upstream before
copying signatures or implementation details.

## Executive summary

`returns.Result[T, E]` is an immutable, covariant, two-track container:

- `Success[T]` transforms and binds on the value track;
- `Failure[E]` preserves the failure or transforms/recovers on the error track;
- normal composition does not catch callback exceptions;
- extraction methods can raise `UnwrapFailedError`;
- exception capture is explicit through decorators such as `safe`;
- pattern matching and generator-based do-notation are supported;
- async/effectful work uses companion containers such as `FutureResult` and
  `IOResult`;
- advanced HKT, decorator, and do-notation typing depends on a mypy plugin;
- core Result composition stops at the first failure rather than accumulating
  validation errors.

The semantic rigor is valuable. The interface hierarchy, point-free mirror,
effect-container family, and checker plugin are too much initial surface for
snekok.

## Construction and representation

Callers construct explicit variants:

```python
Success(1)
Failure("invalid")
```

`Result` itself is an abstract container rather than a public two-argument
constructor. `Result.from_value` and `Result.from_failure` support generic
container-oriented construction. `ResultE[T]` aliases
`Result[T, Exception]`.

Both value and error parameters are covariant. Containers are immutable,
slotted, comparable by variant and payload, and hashable when their payload is
hashable.

Snekok keeps the explicit variant idea but uses Python-familiar `Ok` and `Err`
and represents `Result` directly as their union.

Sources: [`returns/result.py`](https://github.com/dry-python/returns/blob/0.29.0/returns/result.py),
[`container.py`](https://github.com/dry-python/returns/blob/0.29.0/returns/primitives/container.py),
[`types.py`](https://github.com/dry-python/returns/blob/0.29.0/returns/primitives/types.py).

## Value-track operations

| Operation | Success behavior | Failure behavior |
| --- | --- | --- |
| `map(f)` | `Success(f(value))` | passes the failure through |
| `bind(f)` | runs `f(value)` and flattens its Result | passes the failure through |
| `apply(container)` | applies a wrapped function | preserves failure semantics |
| `value_or(default)` | returns the value | returns the eager default |
| `unwrap()` | returns the value | raises `UnwrapFailedError` |

Ordinary `bind` keeps one error type. Point-free `unify` supports a function
whose Result has another error type and widens the resulting error channel.

Snekok naming candidates remain `map`, `and_then`, and `unwrap_or`. Partial
extraction should be conspicuous and unnecessary in normal control flow.

Sources: [`returns/result.py`](https://github.com/dry-python/returns/blob/0.29.0/returns/result.py),
[`unify.py`](https://github.com/dry-python/returns/blob/0.29.0/returns/pointfree/unify.py).

## Error-track operations

| Operation | Failure behavior | Success behavior |
| --- | --- | --- |
| `alt(f)` | maps `E -> F` | passes the success through |
| `lash(f)` | runs `E -> Result[T, F]` and flattens | passes the success through |
| `failure()` | extracts the error | raises `UnwrapFailedError` |
| `swap()` | moves the error to success | moves the value to failure |

`alt` and `lash` are mathematically consistent with the wider library but less
discoverable to many Python users. For snekok, `map_err` and `or_else` or
`recover_with` are clearer candidates.

Source: [`returns/result.py`](https://github.com/dry-python/returns/blob/0.29.0/returns/result.py).

## Exception capture

`safe` wraps a synchronous function and catches `Exception` or configured
exception classes into `Failure`. `attempt` records the function's single input
as the failure payload instead of retaining the exception. Async equivalents,
including `future_safe` and `future_attempt`, produce `FutureResult`.

Important boundary rule: `map` and `bind` callbacks are not implicitly safe. If
a callback raises, the exception still propagates.

This is a strong precedent for snekok:

- exception conversion should have an explicit name;
- adapters should catch selected `Exception` subclasses, never control-flow
  `BaseException` events;
- normal transformations should not silently alter exception behavior.

Sources: [`returns/result.py`](https://github.com/dry-python/returns/blob/0.29.0/returns/result.py),
[`returns/future.py`](https://github.com/dry-python/returns/blob/0.29.0/returns/future.py).

## Pattern matching

Containers expose positional matching:

```python
match outcome:
    case Success(value):
        use(value)
    case Failure(error):
        handle(error)
```

This is the clearest dependency-light consumption style and directly informed
snekok's initial API. Python itself does not universally enforce match
exhaustiveness, so checker configuration remains part of the contract.

Source: [`container.py`](https://github.com/dry-python/returns/blob/0.29.0/returns/primitives/container.py).

## Do-notation

`Result.do` consumes a generator expression:

```python
Result.do(first + second for first in Success(1) for second in Success(2))
```

Success iteration yields a value. Failure iteration raises an internal unwrap
exception that `do` catches and converts back to that failure. The first failure
short-circuits, and the syntax supports one final yielded expression rather than
general statement-level do-notation.

The implementation is clever, but typing and maintenance costs outweigh the
benefit for a small initial library. Method chaining and `match` are simpler.

Sources: [`returns/result.py`](https://github.com/dry-python/returns/blob/0.29.0/returns/result.py),
[do-notation documentation](https://returns.readthedocs.io/en/latest/pages/do-notation.html).

## Point-free pipelines and interfaces

`flow(value, *functions)` executes left-to-right. `pipe(*functions)` builds a
reusable pipeline. `returns.pointfree` mirrors container methods with curried
helpers such as `map_`, `bind`, `alt`, `lash`, and `value_or`.

Underneath, capability interfaces split mapping, binding, error mapping,
recovery, and extraction into reusable abstractions. HKT emulation lets those
helpers target `Result`, `IOResult`, `FutureResult`, reader containers, and
other related types.

This is useful when cross-container polymorphism is a goal. It is excessive for
snekok until at least two real container implementations require the same
abstraction.

Sources: [`pipeline.py`](https://github.com/dry-python/returns/blob/0.29.0/returns/pipeline.py),
[`interfaces`](https://github.com/dry-python/returns/tree/0.29.0/returns/interfaces),
[`pointfree`](https://github.com/dry-python/returns/tree/0.29.0/returns/pointfree).

## Async and effect containers

`FutureResult[T, E]` represents asynchronous computation and resolves through
`IOResult[T, E]`, preserving that executing the computation is effectful.
Async-aware mapping and binding bridge ordinary Results, awaitables, Futures,
and FutureResults.

This models effects precisely but creates a substantial API and learning
surface. Snekok should initially let ordinary async functions return
`Result[T, E]`; a separate async abstraction should require concrete composition
pressure.

Source: [`returns/future.py`](https://github.com/dry-python/returns/blob/0.29.0/returns/future.py).

## Type-checker plugin

The mypy plugin supplies inference hooks for HKT emulation, decorators,
do-notation, partial/curry helpers, and related constructs that standard mypy
cannot represent precisely. Other checkers do not run mypy plugins.

Snekok should keep signatures understandable to ordinary Pyright and standard
PEP typing. Fewer abstractions are preferable to checker-specific magic.

Sources: [plugin documentation](https://returns.readthedocs.io/en/latest/pages/contrib/mypy_plugins.html),
[`returns_plugin.py`](https://github.com/dry-python/returns/blob/0.29.0/returns/contrib/mypy/returns_plugin.py).

## Validation semantics

`bind`, do-notation, applicative application, and collection helpers preserve
first-failure semantics. The core API has no accumulating `Validated` type,
non-empty error collection, or semigroup-constrained combination.

Snekok should not silently make ordinary Result composition accumulate errors.
If independent validation needs every error, add a distinct abstraction or an
explicit collection operation.

Sources: [`returns/result.py`](https://github.com/dry-python/returns/blob/0.29.0/returns/result.py),
[`iterables.py`](https://github.com/dry-python/returns/blob/0.29.0/returns/iterables.py).

## Ideas to retain or avoid

Retain:

- immutable value semantics and covariance;
- explicit variants and structural matching;
- typed mapping, binding, error mapping, and recovery when consumers need them;
- explicit exception-boundary adapters;
- clear first-failure semantics.

Rename or simplify:

- `alt` -> `map_err`;
- `lash` -> `or_else` or `recover_with`;
- prefer methods and IDE discovery over a full point-free mirror.

Avoid initially:

- HKT emulation and a capability-interface lattice;
- a custom checker plugin;
- do-notation;
- `IOResult`, reader, and async container families;
- implicit exception capture in transformations;
- mixing accumulating validation into Result's fail-fast semantics.

## Research limits

- This is a pinned source snapshot; latest upstream may differ.
- Advanced inference varies with mypy and plugin versions.
- Recommendations are interpretation, not verified upstream behavior and not a
  commitment to snekok's future API.
