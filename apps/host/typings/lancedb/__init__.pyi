# Local partial stubs for `lancedb` 0.33 (async API only).
#
# `lancedb` ships inline annotations but no `py.typed` marker, and several of its
# signatures resolve to `Unknown` under the host's strict pyright config. Rather
# than scatter `# pyright: ignore` through `tether/search_index.py` (the sole
# importer), these stubs declare exactly the async surface that module uses, with
# precise types. Keep them in step with the pinned `lancedb` version; they are a
# deliberately narrow slice, not a full typing of the library.

from pathlib import Path

from lancedb.db import AsyncConnection

async def connect_async(uri: str | Path) -> AsyncConnection: ...
