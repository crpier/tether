from datetime import datetime
from typing import Literal, Self

import yaml
from anyio import Path, open_file
from pydantic import BaseModel
from snekql.sqlite import (
    MISSING,
    CurrentTimestamp,
    Database,
    DateTime,
    Fetched,
    Integer,
    Model,
    Pending,
    Text,
    insert,
    select,
)

from tether.memory.errors import MemoryParseError

type MemorySource = Literal["conversation", "youtube", "manual"]
type MemoryStatus = Literal["loose", "tethered"]


class MemoryItem[S = Pending](Model[S, "MemoryItem[Fetched]"]):
    """A record in the `memories` table."""

    __tablename__ = "memories"

    id: MemoryItem.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=MISSING
    )
    title: MemoryItem.Col[str] = Text(nullable=False)
    created_at: MemoryItem.GenCol[datetime] = DateTime(
        server_default=CurrentTimestamp(), default=MISSING
    )
    source: MemoryItem.Col[MemorySource] = Text(nullable=False)
    status: MemoryItem.Col[MemoryStatus] = Text(nullable=False)


class Memory(BaseModel):
    """A memory that Tether stores and serves."""

    id: str
    title: str
    body: str
    tags: list[str]

    def file_name(self) -> Path:
        """The name of the file that stores this memory."""
        return Path(f"{self.id}.md")

    async def persist(self) -> None:
        """Persist the Memory to disk."""
        # TODO: should we fail if the file already exists?
        async with await open_file(self.file_name(), mode="w") as f:
            await f.write(str(self))

    @classmethod
    async def load(cls, name: str) -> None:
        """Load the Memory from disk."""
        async with await open_file(name) as f:
            await f.read()

    def __str__(self) -> str:
        """A stringified memory is the markdown document, including frontmatter."""
        return self._dump_frontmatter() + self.body

    @classmethod
    def _load_frontmatter(cls, text: str) -> Self:
        """Parse a Markdown document's frontmatter.
        The frontmatter is a YAML block at the beginning of the document,
        surrounded by lines of three dashes (`---`).
        If the frontmatter is not present or is malformed, raises errors."""
        if not text.startswith("---\n"):
            msg = "Memory does not have a frontmatter"
            raise MemoryParseError(msg)
        text = text[len("---\n") :]
        raw_frontmatter, _ = text.split("\n---\n", 1)
        parsed_frontmatter = yaml.safe_load(raw_frontmatter)
        # Can't really be strict when working with yaml values
        # Must use "extra" so we don't break migrations.
        return cls.model_validate(parsed_frontmatter, strict=False, extra="ignore")

    def _dump_frontmatter(self) -> str:
        """Dump the frontmatter for a Markdown document."""
        return "---\n" + yaml.dump(self.model_dump(exclude={"body"})) + "---\n"


class MemoryService:
    def __init__(self, *, database: Database) -> None:
        self.database = database

    async def capture_memory(self, memory: MemoryItem[Pending]) -> MemoryItem[Fetched]:
        async with self.database.transaction() as tx:
            return await tx.execute(insert(memory).returning())

    async def list_active_memories(self) -> list[MemoryItem[Fetched]]:
        async with self.database.transaction() as tx:
            return await tx.fetch_all(select(MemoryItem))
