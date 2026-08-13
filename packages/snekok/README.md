# snekok

Small, typed tools for treating expected failures as values in Python.

```python
from snekok import Err, Ok, Result


def parse_port(raw: str) -> Result[int, str]:
    if raw.isdecimal():
        return Ok(int(raw))
    return Err("port must be an integer")


match parse_port("8080"):
    case Ok(port):
        print(port)
    case Err(error):
        print(error)
```

The initial API deliberately contains only `Ok`, `Err`, and `Result`. Concrete
consumer needs will drive additional operations rather than snekok anticipating
a large functional-programming framework.

See [`docs/result.md`](docs/result.md) for the contract. Pinned design references
for Better Auth and `dry-python/returns` live in
[`docs/research/`](docs/research/).
