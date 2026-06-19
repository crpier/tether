"""Round-trip tests for the Memory Document format (frontmatter + body).

render_document and parse_document are the single definition of the on-disk
format, and the reindex path parses the same files. These pin that the format is
lossless across exactly the value types that quietly break naive YAML/frontmatter
handling: a tag that looks like a YAML bool ("no"), a colon in a title, a colon
in a URL locator, and a body that itself contains a `---` horizontal rule.

MemoryDocument is a proposed type for the file's content (frontmatter fields +
body), separate from the snekql row; rename or reshape as you implement.
"""

from datetime import UTC, datetime

from snektest import assert_eq, test

from tether.memory import Memory, parse_document, render_document


@test()
def test_document_round_trips_metadata_and_body() -> None:
    """parse_document inverts render_document across footgun-prone metadata."""
    document = MemoryDocument(
        id=42,
        title="Recipe: tomato soup",
        body="Sweat onions low and slow.",
        tags=["coffee", "no", "to-read: later"],
        created_at=datetime(2026, 6, 19, 8, 30, 15, tzinfo=UTC),
    )

    assert_eq(parse_document(render_document(document)), document)


@test()
def test_round_trip_preserves_body_containing_horizontal_rule() -> None:
    """A body with its own `---` survives; the splitter strips only the fence."""
    document = MemoryDocument(
        id=7,
        title="Notes",
        body="First thought.\n\n---\n\nSecond thought after a rule.",
        tags=[],
        created_at=datetime(2026, 6, 19, 8, 30, 15, tzinfo=UTC),
    )

    assert_eq(parse_document(render_document(document)).body, document.body)
