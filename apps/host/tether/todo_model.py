"""Domain values and policy constants for the Todo vertical."""

from typing import Literal

type TodoStatus = Literal["active", "completed", "abandoned"]
"""A Todo's lifecycle state. `active` is live; `completed` and `abandoned`
are terminal. A convention over the string column, not a schema-enforced enum."""

READY_DIGEST_CAP = 10
"""Maximum ready Todos carried in the standing digest."""

WAITING_DIGEST_CAP = 15
"""Maximum waiting Todos carried for relevance-gated mention."""
