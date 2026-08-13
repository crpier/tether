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
- `Result[T, E]` is the union `Ok[T] | Err[E]`.

Both variants are immutable, slotted value types. Their type parameters are
covariant, so each channel can widen without reconstructing the result.

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

## Exception boundary

`Result` describes expected outcomes. It does not catch exceptions implicitly.
Programmer errors, cancellation, and broken invariants remain exceptions.
Exception-capture helpers will be introduced only when real boundary code
establishes their required semantics.
