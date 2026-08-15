# Result

`Result[T, E]` represents either an expected success or an expected failure:

```python
from snekok import Err, Ok, Result


def read_count(raw: str) -> Result[int, str]:
    if not raw.isdecimal():
        return Err("count must be an integer")
    return Ok(int(raw))
```

## Variants

- `Ok(value)` contains a successful `T` in `.value`.
- `Err(error)` contains a failed `E` in `.error`.
- `Result[T, E]` is the union `Ok[T, E] | Err[T, E]`.

Both variants are immutable, slotted value types. Each carries a phantom type
for the absent channel, allowing transformations to preserve and widen both
covariant channels without reconstructing an enclosing result.

## Consumption

Use structural pattern matching when both outcomes affect control flow:

```python
match read_count(raw):
    case Ok(count):
        consume(count)
    case Err(error):
        report(error)
```

Pyright can narrow each branch and report a non-exhaustive match when configured
with `reportMatchNotExhaustive = "error"`.

Direct field access is appropriate after an existing branch has already narrowed
the variant:

```python
outcome = read_count(raw)
if isinstance(outcome, Err):
    report(outcome.error)
```

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

Transform callbacks are ordinary Python calls. Exceptions raised by them are not
caught or converted into `Err` values.

## Exception boundary

`Result` describes expected outcomes. It does not catch exceptions implicitly.
Programmer errors, cancellation, and broken invariants remain exceptions.
Exception-capture helpers will be introduced only when real boundary code
establishes their required semantics.
