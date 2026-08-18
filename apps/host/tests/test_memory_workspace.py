"""Behavior tests for canonical memory workspace discovery and validation."""

from datetime import date
from pathlib import Path

from anyio import TemporaryDirectory
from snektest import assert_eq, assert_in, assert_not_in, test

from tether.memory_workspace import MemoryWorkspace


def _valid_topic(path: Path) -> None:
    path.write_text(
        "\n".join(
            (
                "---",
                "title: Travel preferences",
                "evidence:",
                "  - tether://message/019A",
                "context: always",
                "review_after: 2026-01-01T00:00:00+00:00",
                "note: keep this metadata",
                "---",
                "Prefer aisle seats on long flights.",
            )
        ),
        encoding="utf-8",
    )


@test()
async def scan_collects_valid_topics_and_ignores_editor_artifacts() -> None:
    """Scan returns only non-hidden `.md` files with required frontmatter."""
    async with TemporaryDirectory() as kb_root:
        root = Path(kb_root) / "memory"
        root.mkdir(parents=True)

        _valid_topic(root / "travel.md")
        (root / "notes.txt").write_text("skip", encoding="utf-8")
        _valid_topic(root / ".hidden.md")
        (root / "~temp.md").write_text("should-be-ignored", encoding="utf-8")
        (root / "draft.swp").write_text("skip", encoding="utf-8")

        result = await MemoryWorkspace(root).scan()

    assert_eq(len(result.topics), 1)
    assert_eq(result.topics[0].path.name, "travel.md")
    assert_eq(result.topics[0].title, "Travel preferences")
    assert_eq(result.topics[0].frontmatter["note"], "keep this metadata")
    assert_not_in("hidden", [item.path.name for item in result.topics])
    assert_not_in("~temp.md", [item.path.name for item in result.topics])
    assert_not_in("draft.swp", [item.path.name for item in result.topics])
    assert_not_in("notes.txt", [item.path.name for item in result.topics])
    assert_eq(result.diagnostics, [])


@test()
async def scan_rejects_non_utf8_and_non_markdown_files() -> None:
    """Binary payloads and non-Markdown files are not valid current topics."""
    async with TemporaryDirectory() as kb_root:
        root = Path(kb_root) / "memory"
        root.mkdir(parents=True)
        (root / "binary.md").write_bytes(bytes.fromhex("ff"))
        (root / "notes.txt").write_text("just text", encoding="utf-8")

        result = await MemoryWorkspace(root).scan()

    assert_eq(result.topics, [])
    assert_in(
        "frontmatter.invalid_utf8",
        [item.code for item in result.diagnostics],
    )
    assert_not_in(
        "frontmatter.invalid_yaml",
        [item.code for item in result.diagnostics],
    )


@test()
async def scan_rejects_bad_frontmatter_and_surfacing_details() -> None:
    """Malformed frontmatter does not become current Memory topics."""
    async with TemporaryDirectory() as kb_root:
        root = Path(kb_root) / "memory"
        root.mkdir(parents=True)

        (root / "duplicate.md").write_text(
            "\n".join(
                (
                    "---",
                    "title: One",
                    "title: Two",
                    "---",
                    "body",
                )
            ),
            encoding="utf-8",
        )
        (root / "missing-title.md").write_text(
            "\n".join(
                (
                    "---",
                    "evidence: [tether://message/019A]",
                    "---",
                    "body",
                )
            ),
            encoding="utf-8",
        )
        (root / "custom-tag.md").write_text(
            "\n".join(
                (
                    "---",
                    "title: tagged",
                    "note: !dangerous yes",
                    "---",
                    "body",
                )
            ),
            encoding="utf-8",
        )

        result = await MemoryWorkspace(root).scan()

    assert_eq(result.topics, [])
    codes = {item.code for item in result.diagnostics}
    assert_in("frontmatter.duplicate_key", codes)
    assert_in("frontmatter.missing_title", codes)
    assert_in("frontmatter.invalid_yaml", codes)


@test()
async def scan_rejects_nested_duplicate_frontmatter_keys() -> None:
    """Duplicate YAML keys are rejected at every mapping depth."""
    async with TemporaryDirectory() as kb_root:
        root = Path(kb_root) / "memory"
        root.mkdir(parents=True)
        (root / "nested-duplicate.md").write_text(
            "\n".join(
                (
                    "---",
                    "title: Duplicate nested metadata",
                    "extra:",
                    "  nested: one",
                    "  nested: two",
                    "---",
                    "Body content.",
                )
            ),
            encoding="utf-8",
        )

        result = await MemoryWorkspace(root).scan()

    assert_eq(result.topics, [])
    assert_in("frontmatter.duplicate_key", {item.code for item in result.diagnostics})


@test()
async def scan_accepts_date_only_review_after() -> None:
    """The documented date-only maintenance hint is valid."""
    async with TemporaryDirectory() as kb_root:
        root = Path(kb_root) / "memory"
        root.mkdir(parents=True)
        (root / "dated.md").write_text(
            "\n".join(
                (
                    "---",
                    "title: Dated",
                    "review_after: 2027-03-01",
                    "---",
                    "Body content.",
                )
            ),
            encoding="utf-8",
        )

        result = await MemoryWorkspace(root).scan()

    assert_eq(result.diagnostics, [])
    assert_eq(len(result.topics), 1)
    assert_eq(result.topics[0].review_after, date(2027, 3, 1))


@test()
async def scan_allows_unknown_metadata_and_nonblocking_optional_fields() -> None:
    """Optional memory hints may be malformed without blocking a valid file."""
    async with TemporaryDirectory() as kb_root:
        root = Path(kb_root) / "memory"
        root.mkdir(parents=True)
        (root / "loose.md").write_text(
            "\n".join(
                (
                    "---",
                    "title: Loose",
                    "evidence: this-should-be-list",
                    "context: maybe",
                    "review_after: not-a-date",
                    "extra: { nested: true }",
                    "---",
                    "Body content.",
                )
            ),
            encoding="utf-8",
        )

        result = await MemoryWorkspace(root).scan()

    assert_eq(len(result.topics), 1)
    [topic] = result.topics
    assert_eq(topic.title, "Loose")
    assert_eq(topic.frontmatter["extra"], {"nested": True})
    assert_in(
        "frontmatter.evidence.invalid", {item.code for item in result.diagnostics}
    )
    assert_in("frontmatter.context.invalid", {item.code for item in result.diagnostics})
    assert_in(
        "frontmatter.review_after.invalid", {item.code for item in result.diagnostics}
    )
    assert_eq(topic.context_always, False)
    assert topic.review_after is None


@test()
async def scan_scans_nested_directories_and_ignores_nested_artifacts() -> None:
    """Nested directories are discovered while editor artifacts are ignored."""
    async with TemporaryDirectory() as kb_root:
        root = Path(kb_root) / "memory"
        root.mkdir(parents=True)
        nested = root / "topics" / "nested"
        nested.mkdir(parents=True)
        _valid_topic(nested / "valid.md")
        (nested / ".ignore.md").write_text("skip", encoding="utf-8")
        (nested / "tmp~").write_text("skip", encoding="utf-8")

        result = await MemoryWorkspace(root).scan()

    assert_eq(len(result.topics), 1)
    assert_eq(result.topics[0].path.name, "valid.md")
    assert_eq(result.topics[0].title, "Travel preferences")
    assert_eq(result.diagnostics, [])


@test()
async def scan_rejects_symlinked_root_and_symlinked_entries() -> None:
    """Symlinked roots and entries are surfaced as diagnostics, never scanned."""
    async with TemporaryDirectory() as kb_root:
        root = Path(kb_root)
        real_root = root / "real"
        real_root.mkdir()
        (real_root / "safe.md").write_text(
            "\n".join(
                (
                    "---",
                    "title: Safe",
                    "---",
                    "From real root.",
                )
            ),
            encoding="utf-8",
        )
        link_root = root / "memory"
        link_root.symlink_to(real_root)

        result = await MemoryWorkspace(link_root).scan()

    assert_eq(result.topics, [])
    assert_eq(len(result.diagnostics), 1)
    assert_in("workspace.symlinked_root", {item.code for item in result.diagnostics})

    async with TemporaryDirectory() as second_root:
        root = Path(second_root) / "memory"
        root.mkdir(parents=True)
        linked = root / "linked.md"
        linked.write_text("skip", encoding="utf-8")

        outside = root / "outside"
        outside.mkdir()
        _valid_topic(outside / "real.md")

        rogue = outside / "rogue.md"
        rogue.symlink_to(outside / "real.md")

        result = await MemoryWorkspace(root).scan()

    assert_eq(len(result.topics), 1)
    assert_eq(result.topics[0].title, "Travel preferences")
    assert_in("path.symlink", {item.code for item in result.diagnostics})
