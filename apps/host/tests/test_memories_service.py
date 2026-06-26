"""Behavior tests for the Memory spine service layer.

These drive the *service* seam directly against a real (in-memory) SQLite
database — no HTTP, no agent — which is the primary testing seam: call a
capability and assert on observable behavior, never on internal structure.

The service under test is `tether.memories.MemoryService`, constructed over a
snekql `Database`. Each method owns its own transaction:

    capture(text)                    -> Memory
    tether(memory)                   -> Memory
    edit_content(memory, text)       -> Memory
    delete(memory)                   -> Memory
    search(query)                    -> list[Memory]

A `Memory` exposes `.id`, `.content`, `.version`, and the
`.tethered_at` / `.deleted_at` timestamps that derive its state.
"""

import asyncio
import contextlib
from pathlib import Path

from anyio import TemporaryDirectory
from snekql.sqlite import Config, Database, Fetched, delete, select
from snektest import (
    AsyncFixture,
    assert_eq,
    assert_gt,
    assert_in,
    assert_is_none,
    assert_is_not_none,
    assert_not_in,
    assert_raises,
    assert_true,
    load_fixture,
    test,
)

from tether.memories import (
    EmptySearchQueryError,
    KnowledgeBaseService,
    Memory,
    MemoryConflictError,
    MemoryNotFoundError,
    MemoryService,
    create_memory_schema,
)


async def capture_tethered_memory(
    service: MemoryService, content: str
) -> Memory[Fetched]:
    """Create a tethered Memory as test setup."""
    memory = await service.capture(content)
    return await service.tether(memory)


async def fetch_memory_row(
    service: MemoryService, memory: Memory[Fetched]
) -> Memory[Fetched] | None:
    """Fetch a Memory row directly for DB-observable assertions."""
    async with service.database.transaction() as tx:
        return await tx.fetch_one_or_none(select(Memory).where(Memory.id.eq(memory.id)))


async def hard_delete_memory_row(
    service: MemoryService, memory: Memory[Fetched]
) -> None:
    """Physically remove a row to simulate a missing observed Memory."""
    async with service.database.transaction() as tx:
        _ = await tx.execute(delete(Memory).where(Memory.id.eq(memory.id)))


def projection_path(service: MemoryService, memory: Memory[Fetched]) -> Path:
    """Return the derived KB projection path for a Memory."""
    return service.kb_service.kb_root / f"{memory.id}.md"


async def memory_service() -> AsyncFixture[MemoryService]:
    """A fresh, isolated Tether database + an empty markdown KB directory.

    The KB lives in a throwaway temp dir so projection assertions observe real
    files on disk; both DB and dir are torn down after each test.
    """
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(db)
    async with TemporaryDirectory() as kb_root:
        kb_service = KnowledgeBaseService(kb_root=Path(kb_root))
        yield MemoryService(database=db, kb_service=kb_service)
    await db.close()


@test()
async def capture_lands_loose() -> None:
    """Capture always lands loose — never directly tethered."""
    service = await load_fixture(memory_service())

    memory = await service.capture("I prefer aisle seats on flights")

    assert_is_none(memory.tethered_at)


@test()
async def capture_records_manual_provenance() -> None:
    """Capture only ever produces manual provenance."""
    service = await load_fixture(memory_service())

    memory = await service.capture("I prefer aisle seats on flights")

    assert_eq(memory.provenance, {"kind": "manual"})


@test()
async def capture_starts_at_version_one() -> None:
    """Optimistic concurrency starts from the first observed Memory revision."""
    service = await load_fixture(memory_service())

    memory = await service.capture("I prefer aisle seats on flights")

    assert_eq(memory.version, 1)


@test()
async def capturing_does_not_project_markdown() -> None:
    """a loose Memory is absent from the Knowledge base."""
    service = await load_fixture(memory_service())

    memory = await service.capture("I prefer aisle seats")

    assert_true(not projection_path(service, memory).exists())


@test()
async def loose_memory_is_excluded_from_search() -> None:
    """a loose Memory is not yet part of the assistant's Search."""
    service = await load_fixture(memory_service())

    loose = await service.capture("I prefer aisle seats on flights")

    found = [hit.id for hit in await service.search("aisle")]
    assert_not_in(loose.id, found)


@test()
async def tether_makes_loose_memory_searchable() -> None:
    """Tether is the trust transition that admits a Memory to Search."""
    service = await load_fixture(memory_service())

    memory = await capture_tethered_memory(
        service, "I prefer window seats on long flights"
    )

    found = [hit.id for hit in await service.search("window")]
    assert_in(memory.id, found)


@test()
async def deleted_memory_is_excluded_from_search() -> None:
    """Reject removes a tethered Memory from the assistant's Search."""
    service = await load_fixture(memory_service())
    memory = await capture_tethered_memory(
        service, "got a penicillin prescription back in 2019"
    )

    _ = await service.delete(memory)

    found = [hit.id for hit in await service.search("penicillin")]
    assert_not_in(memory.id, found)


@test()
async def search_requires_a_non_empty_query() -> None:
    """Keyword Search rejects blank queries instead of browsing all Memory."""
    service = await load_fixture(memory_service())

    with assert_raises(EmptySearchQueryError):
        _ = await service.search("   ")


@test()
async def search_matches_memory_with_all_terms() -> None:
    """Keyword Search includes Memories containing every query term."""
    service = await load_fixture(memory_service())
    matching = await capture_tethered_memory(
        service, "I prefer window seats on flights"
    )

    found = [hit.id for hit in await service.search("window flights")]

    assert_in(matching.id, found)


@test()
async def search_excludes_memory_missing_a_query_term() -> None:
    """Keyword Search ANDs whitespace terms together."""
    service = await load_fixture(memory_service())
    non_matching = await capture_tethered_memory(
        service, "I prefer window tables in cafes"
    )

    found = [hit.id for hit in await service.search("window flights")]

    assert_not_in(non_matching.id, found)


@test()
async def search_orders_matches_newest_first() -> None:
    """Keyword Search is unranked, so recency orders equal LIKE matches."""
    service = await load_fixture(memory_service())
    older = await capture_tethered_memory(service, "needle older memory")

    # We sleep a bit because the precision of our datetimes in sqlite is miliseconds.
    await asyncio.sleep(0.01)
    newer = await capture_tethered_memory(service, "needle newer memory")

    found = [hit.id for hit in await service.search("needle")]

    assert_eq(found, [newer.id, older.id])


@test()
async def human_edit_of_tethered_memory_is_searchable_by_new_text() -> None:
    """A human edit of tethered Memory makes the new text Searchable."""
    service = await load_fixture(memory_service())
    memory = await capture_tethered_memory(service, "I live in Berlin")

    memory = await service.edit_content(memory, "I live in Munich")

    found = [hit.id for hit in await service.search("Munich")]
    assert_in(memory.id, found)


@test()
async def human_edit_of_tethered_memory_drops_from_old_text() -> None:
    """An edit replaces searchable text: old wording no longer matches."""
    service = await load_fixture(memory_service())
    memory = await capture_tethered_memory(service, "I live in Berlin")

    memory = await service.edit_content(memory, "I live in Munich")

    found = [hit.id for hit in await service.search("Berlin")]
    assert_not_in(memory.id, found)


@test()
async def tether_bumps_version() -> None:
    """Tether consumes one observed revision and returns the next revision."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I prefer aisle seats on flights")

    tethered = await service.tether(memory)

    assert_eq(tethered.version, memory.version + 1)


@test()
async def edit_bumps_version() -> None:
    """A human edit consumes one observed revision and returns the next revision."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I live in Berlin")

    edited = await service.edit_content(memory, "I live in Munich")

    assert_eq(edited.version, memory.version + 1)


@test()
async def tether_stamps_tethered_at() -> None:
    """tether records when the trust transition happened."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I prefer window seats on long flights")

    tethered = await service.tether(memory)

    _ = assert_is_not_none(tethered.tethered_at)


@test()
async def re_tethering_a_memory_raises_conflict() -> None:
    """a tethered Memory cannot pass through Review twice."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I prefer aisle seats")
    _ = await service.tether(memory)

    with assert_raises(MemoryConflictError):
        _ = await service.tether(memory)


@test()
async def editing_with_a_stale_version_raises_conflict() -> None:
    """A human edit targets the Memory revision the human actually saw."""
    service = await load_fixture(memory_service())
    captured = await service.capture("I live in Berlin")
    observed = await service.tether(captured)
    _ = await service.edit_content(observed, "I live in Munich")

    with assert_raises(MemoryConflictError):
        _ = await service.edit_content(observed, "I live in Paris")


@test()
async def stale_edit_preserves_content() -> None:
    """A rejected stale edit does not overwrite current content."""
    service = await load_fixture(memory_service())
    captured = await service.capture("I live in Berlin")
    observed = await service.tether(captured)
    current = await service.edit_content(observed, "I live in Munich")

    with contextlib.suppress(MemoryConflictError):
        _ = await service.edit_content(observed, "I live in Paris")

    row = await fetch_memory_row(service, observed)
    assert row is not None, "stale edit must not remove the Memory"
    assert_eq(row.content, current.content)


@test()
async def stale_edit_preserves_version() -> None:
    """A rejected stale edit does not advance the stored version."""
    service = await load_fixture(memory_service())
    captured = await service.capture("I live in Berlin")
    observed = await service.tether(captured)
    current = await service.edit_content(observed, "I live in Munich")

    with contextlib.suppress(MemoryConflictError):
        _ = await service.edit_content(observed, "I live in Paris")

    row = await fetch_memory_row(service, observed)
    assert row is not None, "stale edit must not remove the Memory"
    assert_eq(row.version, current.version)


@test()
async def tethering_with_a_stale_version_raises_conflict() -> None:
    """Review targets the loose Memory revision the human actually saw."""
    service = await load_fixture(memory_service())
    observed = await service.capture("I think I prefer aisle seats")
    _ = await service.edit_content(observed, "I prefer aisle seats")

    with assert_raises(MemoryConflictError):
        _ = await service.tether(observed)


@test()
async def stale_tether_leaves_memory_loose() -> None:
    """A rejected stale tether does not promote the Memory."""
    service = await load_fixture(memory_service())
    observed = await service.capture("I think I prefer aisle seats")
    _ = await service.edit_content(observed, "I prefer aisle seats")

    with contextlib.suppress(MemoryConflictError):
        _ = await service.tether(observed)

    row = await fetch_memory_row(service, observed)
    assert row is not None, "stale tether must not remove the Memory"
    assert_is_none(row.tethered_at)


@test()
async def stale_tether_preserves_version() -> None:
    """A rejected stale tether does not advance the stored version."""
    service = await load_fixture(memory_service())
    observed = await service.capture("I think I prefer aisle seats")
    current = await service.edit_content(observed, "I prefer aisle seats")

    with contextlib.suppress(MemoryConflictError):
        _ = await service.tether(observed)

    row = await fetch_memory_row(service, observed)
    assert row is not None, "stale tether must not remove the Memory"
    assert_eq(row.version, current.version)


@test()
async def stale_tether_does_not_project_markdown() -> None:
    """A rejected stale tether does not admit the Memory to the Knowledge base."""
    service = await load_fixture(memory_service())
    observed = await service.capture("I think I prefer aisle seats")
    _ = await service.edit_content(observed, "I prefer aisle seats")

    with contextlib.suppress(MemoryConflictError):
        _ = await service.tether(observed)

    assert_true(not projection_path(service, observed).exists())


@test()
async def deleting_with_a_stale_version_raises_conflict() -> None:
    """Reject targets the Memory revision the human actually saw."""
    service = await load_fixture(memory_service())
    observed = await service.capture("I live in Berlin")
    _ = await service.tether(observed)

    with assert_raises(MemoryConflictError):
        _ = await service.delete(observed)


@test()
async def stale_delete_leaves_memory_live() -> None:
    """A rejected stale delete does not soft-delete the Memory."""
    service = await load_fixture(memory_service())
    observed = await service.capture("I live in Berlin")
    _ = await service.tether(observed)

    with contextlib.suppress(MemoryConflictError):
        _ = await service.delete(observed)

    row = await fetch_memory_row(service, observed)
    assert row is not None, "stale delete must not remove the Memory"
    assert_is_none(row.deleted_at)


@test()
async def stale_delete_keeps_memory_searchable() -> None:
    """A rejected stale delete does not remove the Memory from Search."""
    service = await load_fixture(memory_service())
    observed = await service.capture("I live in Berlin")
    _ = await service.tether(observed)

    with contextlib.suppress(MemoryConflictError):
        _ = await service.delete(observed)

    found = [hit.id for hit in await service.search("Berlin")]
    assert_in(observed.id, found)


@test()
async def stale_delete_keeps_projection() -> None:
    """A rejected stale delete does not remove the projection."""
    service = await load_fixture(memory_service())
    observed = await service.capture("I live in Berlin")
    _ = await service.tether(observed)

    with contextlib.suppress(MemoryConflictError):
        _ = await service.delete(observed)

    assert_true(projection_path(service, observed).exists())


@test()
async def stale_delete_preserves_version() -> None:
    """A rejected stale delete does not advance the stored version."""
    service = await load_fixture(memory_service())
    observed = await service.capture("I live in Berlin")
    current = await service.tether(observed)

    with contextlib.suppress(MemoryConflictError):
        _ = await service.delete(observed)

    row = await fetch_memory_row(service, observed)
    assert row is not None, "stale delete must not remove the Memory"
    assert_eq(row.version, current.version)


@test()
async def tethering_a_deleted_memory_raises() -> None:
    """a soft-deleted Memory is not a live target for Review."""
    service = await load_fixture(memory_service())
    memory = await service.capture("a rejected loose memory")
    _ = await service.delete(memory)

    with assert_raises(MemoryNotFoundError):
        _ = await service.tether(memory)


@test()
async def tethering_a_deleted_memory_does_not_stamp_tethered_at() -> None:
    """Failed tether on a deleted Memory does not promote it."""
    service = await load_fixture(memory_service())
    memory = await service.capture("a rejected loose memory")
    _ = await service.delete(memory)

    with contextlib.suppress(MemoryNotFoundError):
        _ = await service.tether(memory)

    row = await fetch_memory_row(service, memory)
    assert row is not None, "soft-deleted row must remain inspectable"
    assert_is_none(row.tethered_at)


@test()
async def tethering_a_deleted_memory_does_not_project_markdown() -> None:
    """Failed tether on a deleted Memory does not write a projection."""
    service = await load_fixture(memory_service())
    memory = await service.capture("a rejected loose memory")
    _ = await service.delete(memory)

    with contextlib.suppress(MemoryNotFoundError):
        _ = await service.tether(memory)

    assert_true(not projection_path(service, memory).exists())


@test()
async def tethering_a_deleted_memory_with_current_version_does_not_promote_it() -> None:
    """A deleted Memory stays deleted even if the caller has its latest version."""
    service = await load_fixture(memory_service())
    memory = await service.capture("a rejected loose memory")
    deleted = await service.delete(memory)

    with contextlib.suppress(MemoryNotFoundError):
        _ = await service.tether(deleted)

    row = await fetch_memory_row(service, memory)
    assert row is not None, "soft-deleted row must remain inspectable"
    assert_is_none(row.tethered_at)


@test()
async def editing_a_deleted_memory_with_current_version_preserves_content() -> None:
    """A deleted Memory cannot be edited with its returned delete version."""
    service = await load_fixture(memory_service())
    memory = await service.capture("original rejected content")
    deleted = await service.delete(memory)

    with contextlib.suppress(MemoryNotFoundError):
        _ = await service.edit_content(deleted, "mutated after delete")

    row = await fetch_memory_row(service, memory)
    assert row is not None, "soft-deleted row must remain inspectable"
    assert_eq(row.content, "original rejected content")


@test()
async def editing_a_loose_memory_changes_content() -> None:
    """a human edit of loose Memory changes its text."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I think I am allergic to penicillin")

    edited = await service.edit_content(memory, "I am allergic to penicillin")

    assert_eq(edited.content, "I am allergic to penicillin")


@test()
async def editing_a_loose_memory_keeps_it_loose() -> None:
    """a human edit of loose Memory does not promote it."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I think I am allergic to penicillin")

    edited = await service.edit_content(memory, "I am allergic to penicillin")

    assert_is_none(edited.tethered_at)


@test()
async def editing_a_loose_memory_stays_excluded_from_search() -> None:
    """edited loose Memory stays outside assistant Search."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I think I am allergic to penicillin")

    _ = await service.edit_content(memory, "I am allergic to penicillin")

    found = [hit.id for hit in await service.search("penicillin")]
    assert_not_in(memory.id, found)


@test()
async def editing_a_loose_memory_does_not_project_markdown() -> None:
    """editing loose Memory does not admit it to the Knowledge base."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I think I prefer aisle seats")

    _ = await service.edit_content(memory, "I prefer aisle seats")

    assert_true(not projection_path(service, memory).exists())


@test()
async def editing_a_memory_bumps_updated_at() -> None:
    """every edit advances `updated_at`."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I live in Berlin")

    await asyncio.sleep(0.01)
    edited = await service.edit_content(memory, "I live in Munich")

    assert_gt(edited.updated_at, memory.updated_at)


@test()
async def deleting_a_memory_stamps_deleted_at() -> None:
    """reject stamps `deleted_at`."""
    service = await load_fixture(memory_service())
    memory = await service.capture("got a penicillin prescription back in 2019")

    _ = await service.delete(memory)

    row = await fetch_memory_row(service, memory)
    assert row is not None, "soft-delete must retain the row in the DB"
    _ = assert_is_not_none(row.deleted_at)


@test()
async def deleting_a_memory_retains_the_row() -> None:
    """reject is a soft-delete, so the DB row survives."""
    service = await load_fixture(memory_service())
    memory = await service.capture("got a penicillin prescription back in 2019")

    _ = await service.delete(memory)

    row = await fetch_memory_row(service, memory)
    assert row is not None, "soft-delete must retain the row in the DB"


@test()
async def deleting_a_memory_preserves_content() -> None:
    """soft-deleted Memory text stays recoverable in the DB."""
    service = await load_fixture(memory_service())
    memory = await service.capture("got a penicillin prescription back in 2019")

    _ = await service.delete(memory)

    row = await fetch_memory_row(service, memory)
    assert row is not None, "soft-delete must retain the row in the DB"
    assert_eq(row.content, "got a penicillin prescription back in 2019")


@test()
async def tethering_a_missing_memory_raises() -> None:
    """operating on an absent Memory is a well-formed error."""
    service = await load_fixture(memory_service())
    memory = await service.capture("a memory removed outside the service")
    await hard_delete_memory_row(service, memory)

    with assert_raises(MemoryNotFoundError):
        _ = await service.tether(memory)


@test()
async def editing_a_deleted_memory_raises() -> None:
    """a soft-deleted Memory is no longer a live target for edits."""
    service = await load_fixture(memory_service())
    memory = await service.capture("a fact I will reject before editing")
    _ = await service.delete(memory)

    with assert_raises(MemoryNotFoundError):
        _ = await service.edit_content(memory, "too late, already gone")


@test()
async def editing_a_deleted_memory_preserves_content() -> None:
    """Failed edits on deleted Memory leave content unchanged."""
    service = await load_fixture(memory_service())
    memory = await service.capture("original rejected content")
    _ = await service.delete(memory)

    with contextlib.suppress(MemoryNotFoundError):
        _ = await service.edit_content(memory, "mutated after delete")

    row = await fetch_memory_row(service, memory)
    assert row is not None, "soft-deleted row must remain inspectable"
    assert_eq(row.content, "original rejected content")


@test()
async def deleting_an_already_deleted_memory_raises() -> None:
    """a second reject finds no live Memory."""
    service = await load_fixture(memory_service())
    memory = await service.capture("a fact I will reject twice")
    _ = await service.delete(memory)

    with assert_raises(MemoryConflictError):
        _ = await service.delete(memory)


@test()
async def tethering_projects_markdown() -> None:
    """tether projects `kb/<id>.md` synchronously."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I prefer aisle seats")

    _ = await service.tether(memory)

    assert_true(projection_path(service, memory).exists())


@test()
async def projected_file_contains_required_frontmatter_keys() -> None:
    """projection frontmatter carries the required keys."""
    service = await load_fixture(memory_service())
    memory = await capture_tethered_memory(service, "I prefer aisle seats on flights")

    contents = projection_path(service, memory).read_text()

    # TODO: I'd prefer this to use `assert_eq` instead.
    assert_in(str(memory.id), contents)
    assert_in("provenance", contents)
    assert_in("created_at", contents)
    assert_in("tethered_at", contents)
    assert_in("updated_at", contents)


@test()
async def projected_frontmatter_records_manual_provenance() -> None:
    """projected markdown exposes Memory provenance."""
    service = await load_fixture(memory_service())
    memory = await capture_tethered_memory(service, "I prefer aisle seats on flights")

    contents = projection_path(service, memory).read_text()

    assert_in("kind: manual", contents)


@test()
async def projected_file_contains_body() -> None:
    """projection body is the Memory text verbatim."""
    service = await load_fixture(memory_service())
    memory = await capture_tethered_memory(service, "I prefer aisle seats on flights")

    contents = projection_path(service, memory).read_text()

    assert_in("I prefer aisle seats on flights", contents)


@test()
async def editing_a_tethered_memory_reprojects_new_text() -> None:
    """editing tethered Memory writes the new text to markdown."""
    service = await load_fixture(memory_service())
    memory = await capture_tethered_memory(service, "I live in Berlin")

    _ = await service.edit_content(memory, "I live in Munich")

    contents = projection_path(service, memory).read_text()
    assert_in("I live in Munich", contents)


@test()
async def editing_a_tethered_memory_removes_old_projection_text() -> None:
    """re-projection drops the old text."""
    service = await load_fixture(memory_service())
    memory = await capture_tethered_memory(service, "I live in Berlin")

    _ = await service.edit_content(memory, "I live in Munich")

    contents = projection_path(service, memory).read_text()
    assert_not_in("Berlin", contents)


@test()
async def deleting_a_tethered_memory_removes_its_file() -> None:
    """rejecting a tethered Memory removes its markdown file."""
    service = await load_fixture(memory_service())
    memory = await capture_tethered_memory(service, "I prefer aisle seats")

    _ = await service.delete(memory)

    assert_true(not projection_path(service, memory).exists())


@test()
async def the_kb_directory_mirrors_the_tethered_set() -> None:
    """kb/ exactly matches tethered, non-deleted Memories."""
    service = await load_fixture(memory_service())

    _ = await service.capture("loose: never tethered")
    first = await capture_tethered_memory(service, "tethered: aisle seats")
    second = await capture_tethered_memory(service, "tethered: window seats")
    rejected = await capture_tethered_memory(service, "tethered then rejected")
    _ = await service.delete(rejected)

    files = {p.name for p in service.kb_service.kb_root.iterdir()}
    assert_eq(files, {f"{first.id}.md", f"{second.id}.md"})


# --- Filter-only Search: the loose review queue and tethered browse ---
# Backs GET /memories?state=loose|tethered. A filter-only Search (no query) is
# state-agnostic as a mechanism but scoped by the requested state, and always
# excludes soft-deleted Memories.


@test()
async def loose_queue_returns_loose_memories() -> None:
    """GET /memories?state=loose surfaces Memories still awaiting Review."""
    service = await load_fixture(memory_service())
    loose = await service.capture("I think I prefer aisle seats")

    found = [hit.id for hit in await service.browse_by_state("loose")]

    assert_in(loose.id, found)


@test()
async def loose_queue_excludes_tethered_memories() -> None:
    """The review queue is loose-only: a tethered Memory has left it."""
    service = await load_fixture(memory_service())
    tethered = await capture_tethered_memory(service, "I prefer window seats")

    found = [hit.id for hit in await service.browse_by_state("loose")]

    assert_not_in(tethered.id, found)


@test()
async def loose_queue_excludes_soft_deleted_memories() -> None:
    """A rejected loose Memory drops out of the review queue."""
    service = await load_fixture(memory_service())
    loose = await service.capture("a loose memory I will reject")
    _ = await service.delete(loose)

    found = [hit.id for hit in await service.browse_by_state("loose")]

    assert_not_in(loose.id, found)


@test()
async def loose_queue_orders_newest_first() -> None:
    """fresh captures surface first, reviewed while context is warm."""
    service = await load_fixture(memory_service())
    older = await service.capture("older loose memory")

    await asyncio.sleep(0.01)
    newer = await service.capture("newer loose memory")

    found = [hit.id for hit in await service.browse_by_state("loose")]

    assert_eq(found, [newer.id, older.id])


@test()
async def tethered_browse_returns_tethered_memories() -> None:
    """GET /memories?state=tethered browses the trusted corpus."""
    service = await load_fixture(memory_service())
    tethered = await capture_tethered_memory(service, "I prefer aisle seats")

    found = [hit.id for hit in await service.browse_by_state("tethered")]

    assert_in(tethered.id, found)


@test()
async def tethered_browse_excludes_loose_memories() -> None:
    """Tethered browse never shows Memories still awaiting Review."""
    service = await load_fixture(memory_service())
    loose = await service.capture("a loose memory")

    found = [hit.id for hit in await service.browse_by_state("tethered")]

    assert_not_in(loose.id, found)


@test()
async def tethered_browse_excludes_soft_deleted_memories() -> None:
    """A rejected tethered Memory drops out of the browse list."""
    service = await load_fixture(memory_service())
    memory = await capture_tethered_memory(service, "a tethered memory I will reject")
    _ = await service.delete(memory)

    found = [hit.id for hit in await service.browse_by_state("tethered")]

    assert_not_in(memory.id, found)


@test()
async def tethered_browse_orders_by_tethered_at_not_created_at() -> None:
    """Tethered browse is ordered by tether time, newest first — not capture time."""
    service = await load_fixture(memory_service())
    captured_first = await service.capture("captured first, tethered second")
    captured_second = await service.capture("captured second, tethered first")

    _ = await service.tether(captured_second)
    await asyncio.sleep(0.01)
    _ = await service.tether(captured_first)

    found = [hit.id for hit in await service.browse_by_state("tethered")]

    assert_eq(found, [captured_first.id, captured_second.id])


# --- Keyword Search limit (default 50) ---


@test()
async def search_caps_results_at_the_given_limit() -> None:
    """Keyword Search returns at most `limit` matches."""
    service = await load_fixture(memory_service())
    for _ in range(3):
        _ = await capture_tethered_memory(service, "needle in the haystack")

    found = await service.search("needle", limit=2)

    assert_eq(len(found), 2)


@test()
async def search_keeps_the_newest_within_the_limit() -> None:
    """When limited, keyword Search keeps the newest matches (recency-ordered)."""
    service = await load_fixture(memory_service())
    _ = await capture_tethered_memory(service, "needle oldest")
    await asyncio.sleep(0.01)
    middle = await capture_tethered_memory(service, "needle middle")
    await asyncio.sleep(0.01)
    newest = await capture_tethered_memory(service, "needle newest")

    found = [hit.id for hit in await service.search("needle", limit=2)]

    assert_eq(found, [newest.id, middle.id])


@test()
async def search_defaults_to_a_limit_of_fifty() -> None:
    """Keyword Search defaults `limit` to 50."""
    service = await load_fixture(memory_service())
    for index in range(51):
        _ = await capture_tethered_memory(service, f"needle number {index}")

    found = await service.search("needle")

    assert_eq(len(found), 50)
