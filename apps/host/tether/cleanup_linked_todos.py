"""Report and explicitly clear legacy scheduled-trigger links from Todos.

Run `python -m tether.cleanup_linked_todos` for a read-only report. Pass
`--confirm` only after reviewing that report to clear the reported links.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from snekql.sqlite import Config, Database, select, update

from tether.todo_store import Todo


@dataclass(frozen=True, slots=True)
class LinkedTodo:
    """A Todo whose deadline points to a legacy scheduled trigger."""

    action: str
    id: str
    trigger_id: str


class CleanupSettings(BaseSettings):
    """The database-path subset of Tether's environment settings."""

    model_config = SettingsConfigDict(env_prefix="TETHER_", validate_default=True)

    database_path: Path = Path(".tether/tether.sqlite3")


async def _cleanup(*, database_path: Path, confirmed: bool) -> tuple[LinkedTodo, ...]:
    """Fetch linked Todos and clear their links only when explicitly confirmed."""
    async with (
        await Database.initialize(Config(database=database_path)) as database,
        database.transaction(
            mode="immediate" if confirmed else "deferred"
        ) as transaction,
    ):
        todos = await transaction.fetch_all(
            select(Todo)
            .where(Todo.trigger_id.is_not_null())
            .order_by(Todo.created_at.asc(), Todo.id.asc())
        )
        linked_todos = tuple(
            LinkedTodo(
                id=str(todo.id),
                action=todo.action,
                trigger_id=todo.trigger_id,
            )
            for todo in todos
            if todo.trigger_id is not None
        )
        if confirmed and linked_todos:
            _ = await transaction.execute(
                update(Todo)
                .set(Todo.trigger_id.to(None))
                .where(Todo.trigger_id.is_not_null())
            )
        return linked_todos


def _parse_args() -> argparse.Namespace:
    """Parse the explicit mutation flag from the public command line."""
    parser = argparse.ArgumentParser(
        prog="python -m tether.cleanup_linked_todos",
        description=(
            "Report Todos linked to legacy scheduled triggers and, only with "
            "--confirm, clear those links."
        ),
    )
    _ = parser.add_argument(
        "--confirm",
        action="store_true",
        help="clear the reported Todo trigger links",
    )
    return parser.parse_args()


def main() -> None:
    """Report linked Todos and perform the one-shot cleanup when confirmed."""
    confirmed: bool = _parse_args().confirm
    linked_todos = asyncio.run(
        _cleanup(
            database_path=CleanupSettings().database_path,
            confirmed=confirmed,
        )
    )
    if not linked_todos:
        print("No linked Todos found.")
        return

    print(f"Linked Todos ({len(linked_todos)}):")
    for todo in linked_todos:
        print(f"- {todo.id}: {todo.action} [trigger: {todo.trigger_id}]")
    if confirmed:
        print(f"Cleared trigger links from {len(linked_todos)} Todos.")
    else:
        print("Dry run: no changes made.")
        print("Run again with --confirm to clear these trigger links.")


if __name__ == "__main__":
    main()
