# Result

`Result[T, E]` represents either an expected success or an expected failure:

```python
from snekok.result import Err, Ok, Result


def read_count(raw: str) -> Result[int, str]:
    if not raw.isdecimal():
        return Err("count must be an integer")
    return Ok(int(raw))
```

## Variants

- `Result[T, E]` is the nominal base type shared by both variants.
- `Ok(value)` contains a successful `T` in `.value`.
- `Err(error)` contains a failed `E` in `.error`.

Both variants are immutable, slotted value types. Each carries a phantom type
for the absent channel, allowing transformations to preserve and widen both
covariant channels without reconstructing an enclosing result. The nominal base
keeps annotations and type-checker output compact.

## Consumption

Narrow a concrete variant when its field is needed, then unwrap the base type:

```python
outcome = read_count(raw)
if isinstance(outcome, Err):
    report(outcome.error)
else:
    consume(outcome.unwrap())
```

`unwrap()` returns `T` and raises `RuntimeError` for an `Err`. `unwrap_error()`
returns `E` and raises `RuntimeError` for an `Ok`. Check the known variant before
calling either method when both outcomes are expected.

`Ok` and `Err` remain class-pattern compatible, but static type checkers cannot
consider a nominal class hierarchy sealed. A `match` over `Result[T, E]` therefore
needs a fallback case even when it handles both built-in variants.

## Composition

Use `map` to transform a success while preserving any error:

```python
label = read_count(raw).map(lambda count: f"count={count}")
```

Use `map_error` to translate an expected error while preserving a success:

```python
identified = read_count(raw).map_error(lambda message: ("invalid_count", message))
```

Use `and_then` to continue with another fallible operation. Existing and newly
introduced error channels are combined:

```python
def require_positive(count: int) -> Result[int, ValueError]:
    if count <= 0:
        return Err(ValueError("count must be positive"))
    return Ok(count)


positive = read_count(raw).and_then(require_positive)
```

`and_then_async` provides the same error propagation for an asynchronous
continuation:

```python
async def require_positive_async(count: int) -> Result[int, ValueError]:
    return require_positive(count)


validated = await read_count(raw).and_then_async(require_positive_async)
```

Transform callbacks are ordinary Python calls. Exceptions raised by them are not
caught or converted into `Err` values.

## Exception boundary

`Result` describes expected outcomes. It does not catch exceptions implicitly.
Programmer errors, cancellation, and broken invariants remain exceptions.
Exception-capture helpers will be introduced only when real boundary code
establishes their required semantics.
