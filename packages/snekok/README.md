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

The `Result` API deliberately stays small: `Ok`, `Err`, `Result`, and the
`map`, `map_error`, and `and_then` composition methods required by concrete
consumers. It does not attempt to provide a functional-programming framework.

## Validated scalar aliases

`NonEmptySecretStr`, `NonEmptyStr`, and `NonNegativeInt` are constrained nominal
aliases with Pydantic support:

```python
from pydantic import BaseModel

from snekok import NonEmptySecretStr, validate_python
from snekok.types import NonEmptyStr, NonNegativeInt


class ApiSettings(BaseModel):
    api_key: NonEmptySecretStr


label = validate_python(NonEmptyStr, "hello").unwrap()
retry_count = validate_python(NonNegativeInt, 0).unwrap()
settings = ApiSettings.model_validate({"api_key": "secret-value"})
assert settings.api_key.get_secret_value() == "secret-value"
```

Pydantic rejects an empty value. Static type checkers also reject an ordinary
`SecretStr` where `NonEmptySecretStr` is required, preventing unvalidated secrets
from crossing the typed boundary.

See [`docs/result.md`](docs/result.md) for the Result contract. Pinned design references
for `dmmulroy/better-result` and `dry-python/returns` live in
[`docs/research/`](docs/research/).
