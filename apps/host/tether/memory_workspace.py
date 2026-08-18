"""Canonical-memory workspace discovery and frontmatter validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

import yaml
from anyio import Path as AsyncPath
from yaml import SafeLoader, YAMLError, safe_load
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode


class FrontmatterParseError(Exception):
    """Raised when a frontmatter blob cannot be parsed into metadata."""


class FrontmatterDuplicateKeyError(FrontmatterParseError):
    """Raised when frontmatter repeats any top-level key."""


class _BlockingScanError(Exception):
    """Raised when a file cannot produce a valid topic."""

    def __init__(self, diagnostic: MemoryWorkspaceDiagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic


_BOUNDARY_OPEN = "---\n"
_BOUNDARY_CLOSE = "\n---\n"
_BOUNDARY_PARTS = 2
_IGNORED_PREFIXES = (".", "~")
_IGNORED_SUFFIXES = ("~", ".swp", ".swo")


@dataclass(frozen=True, slots=True)
class MemoryWorkspaceDiagnostic:
    """One invalid file or scan issue discovered during reconciliation."""

    path: Path
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class MemoryWorkspaceTopic:
    """One validated, recoverable canonical Topic file from the workspace."""

    path: Path
    title: str
    frontmatter: dict[str, object]
    body: str
    evidence: tuple[str, ...]
    context_always: bool
    review_after: date | datetime | None


@dataclass(frozen=True, slots=True)
class MemoryWorkspaceScanResult:
    """The current scan output for a workspace walk."""

    topics: list[MemoryWorkspaceTopic]
    diagnostics: list[MemoryWorkspaceDiagnostic]


def _is_ignored_entry(name: str) -> bool:
    """Return true for editor artefacts and other ignored workspace paths."""
    lower_name = name.lower()
    return lower_name.startswith(_IGNORED_PREFIXES) or lower_name.endswith(
        _IGNORED_SUFFIXES
    )


def _validate_mapping_keys(node: Node) -> None:
    """Reject non-scalar or duplicate keys in every YAML mapping."""
    if isinstance(node, MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            if not isinstance(key_node, ScalarNode):
                message = "frontmatter keys must be scalar"
                raise FrontmatterParseError(message)
            key_value = key_node.value
            if key_value in seen:
                message = f"duplicate key in frontmatter: {key_value}"
                raise FrontmatterDuplicateKeyError(message)
            seen.add(key_value)
            _validate_mapping_keys(value_node)
        return
    if isinstance(node, SequenceNode):
        for item in node.value:
            _validate_mapping_keys(item)


def _decode_frontmatter(frontmatter_text: str) -> dict[str, object]:
    """Decode YAML with recursive duplicate-key and mapping validation."""
    ast = cast("Any", yaml.compose(frontmatter_text, Loader=SafeLoader))  # type: ignore[reportUnknownMemberType]
    if ast is None:
        message = "frontmatter must be a non-empty YAML mapping"
        raise FrontmatterParseError(message)
    if not isinstance(ast, MappingNode):
        message = "frontmatter must be a YAML mapping"
        raise FrontmatterParseError(message)
    _validate_mapping_keys(ast)

    loaded = safe_load(frontmatter_text)
    if loaded is None:
        message = "frontmatter must be a non-empty YAML mapping"
        raise FrontmatterParseError(message)
    if not isinstance(loaded, dict):
        message = "frontmatter must be a YAML mapping"
        raise FrontmatterParseError(message)
    result: dict[str, object] = {}
    for key_node, value in cast("dict[str, Any]", loaded).items():
        key = str(key_node)
        result[key] = value
    return result


def _decode_title(
    frontmatter: dict[str, object], current: Path
) -> tuple[str | None, MemoryWorkspaceDiagnostic | None]:
    """Return a normalized title and optional validation diagnostic."""
    title = frontmatter.get("title")
    if not isinstance(title, str) or not title.strip():
        return None, MemoryWorkspaceDiagnostic(
            path=current,
            code="frontmatter.missing_title",
            message="frontmatter must declare a non-empty title",
        )
    return title.strip(), None


def _decode_evidence(
    frontmatter: dict[str, object], current: Path
) -> tuple[tuple[str, ...], MemoryWorkspaceDiagnostic | None]:
    """Parse optional evidence as a list of strings; malformed values become diagnostics."""
    if "evidence" not in frontmatter:
        return (), None
    raw_evidence = frontmatter["evidence"]
    if isinstance(raw_evidence, list):
        evidence_items = cast("list[Any]", raw_evidence)
        if all(isinstance(item, str) for item in evidence_items):
            return tuple(evidence_items), None
    return (), MemoryWorkspaceDiagnostic(
        path=current,
        code="frontmatter.evidence.invalid",
        message="`evidence` must be a list of strings",
    )


def _decode_context(
    frontmatter: dict[str, object], current: Path
) -> tuple[bool, MemoryWorkspaceDiagnostic | None]:
    """Parse optional context preference and warn when unsupported."""
    if "context" not in frontmatter:
        return False, None
    context = frontmatter["context"]
    if context == "always":
        return True, None
    return False, MemoryWorkspaceDiagnostic(
        path=current,
        code="frontmatter.context.invalid",
        message="`context` must be `always` when provided",
    )


def _decode_review_after(
    frontmatter: dict[str, object], current: Path
) -> tuple[date | datetime | None, MemoryWorkspaceDiagnostic | None]:
    """Parse optional review_after as ISO datetime, non-fatal when malformed."""
    if "review_after" not in frontmatter:
        return None, None
    raw_review_after = frontmatter["review_after"]
    if isinstance(raw_review_after, datetime):
        return raw_review_after, None
    if isinstance(raw_review_after, date):
        return raw_review_after, None
    if not isinstance(raw_review_after, str):
        return None, MemoryWorkspaceDiagnostic(
            path=current,
            code="frontmatter.review_after.invalid",
            message="`review_after` must be an ISO datetime value",
        )
    try:
        return datetime.fromisoformat(raw_review_after), None
    except ValueError as error:
        return None, MemoryWorkspaceDiagnostic(
            path=current,
            code="frontmatter.review_after.invalid",
            message=f"`review_after` must be ISO datetime: {error}",
        )


def _decode_frontmatter_document(
    raw_frontmatter: str, current: Path
) -> tuple[dict[str, object] | None, MemoryWorkspaceDiagnostic | None]:
    """Decode frontmatter text or return a blocking diagnostic."""
    try:
        frontmatter = _decode_frontmatter(raw_frontmatter)
    except FrontmatterDuplicateKeyError:
        return None, MemoryWorkspaceDiagnostic(
            path=current,
            code="frontmatter.duplicate_key",
            message="frontmatter contains a duplicate key",
        )
    except (YAMLError, FrontmatterParseError) as error:
        return None, MemoryWorkspaceDiagnostic(
            path=current,
            code="frontmatter.invalid_yaml",
            message=f"invalid YAML frontmatter: {error}",
        )
    return frontmatter, None


def _extract_frontmatter_blocks(
    normalized: str, current: Path
) -> tuple[tuple[str, str] | None, MemoryWorkspaceDiagnostic | None]:
    """Split content into frontmatter and body chunks."""
    if not normalized.startswith(_BOUNDARY_OPEN):
        return None, MemoryWorkspaceDiagnostic(
            path=current,
            code="frontmatter.missing_boundary",
            message="file must begin with YAML frontmatter",
        )

    front_and_body = normalized.removeprefix(_BOUNDARY_OPEN)
    parts = front_and_body.split(_BOUNDARY_CLOSE, 1)
    if len(parts) != _BOUNDARY_PARTS:
        return None, MemoryWorkspaceDiagnostic(
            path=current,
            code="frontmatter.missing_boundary",
            message="file must contain a frontmatter closing boundary",
        )
    return (parts[0], parts[1]), None


def _decode_frontmatter_or_raise(
    raw_frontmatter: str, current: Path
) -> dict[str, object]:
    """Decode frontmatter and raise on blocking validation failure."""
    frontmatter, frontmatter_diagnostic = _decode_frontmatter_document(
        raw_frontmatter, current
    )
    if frontmatter_diagnostic is not None:
        raise _BlockingScanError(frontmatter_diagnostic)
    assert frontmatter is not None
    return frontmatter


def _decode_title_or_raise(frontmatter: dict[str, object], current: Path) -> str:
    """Require non-empty title for a valid topic."""
    title, title_diagnostic = _decode_title(frontmatter, current)
    if title_diagnostic is not None:
        raise _BlockingScanError(title_diagnostic)
    assert title is not None
    return title


def _extract_frontmatter_blocks_or_raise(
    normalized: str, current: Path
) -> tuple[str, str]:
    """Extract raw frontmatter and body, raising on boundary errors."""
    boundary, boundary_diagnostic = _extract_frontmatter_blocks(normalized, current)
    if boundary_diagnostic is not None:
        raise _BlockingScanError(boundary_diagnostic)
    assert boundary is not None
    return boundary


async def _read_file_text_or_raise(path: AsyncPath, current: Path) -> str:
    """Read UTF-8 file content or raise as a blocking diagnostic."""
    normalized, text_diagnostic = await _read_file_text(path, current)
    if text_diagnostic is not None:
        raise _BlockingScanError(text_diagnostic)
    assert normalized is not None
    return normalized


async def _read_file_text(
    path: AsyncPath, current: Path
) -> tuple[str | None, MemoryWorkspaceDiagnostic | None]:
    """Read file text as UTF-8, with newline normalization."""
    try:
        text = (await path.read_text(encoding="utf-8")).replace("\r\n", "\n")
    except UnicodeDecodeError as error:
        return None, MemoryWorkspaceDiagnostic(
            path=current,
            code="frontmatter.invalid_utf8",
            message=f"file must be UTF-8: {error}",
        )
    return text, None


class MemoryWorkspace:
    """Walk and validate the canonical Markdown workspace."""

    def __init__(self, memory_root: str | Path) -> None:
        self.memory_root: Path = Path(memory_root)

    async def scan(self) -> MemoryWorkspaceScanResult:
        """Return valid topics and surfaced diagnostics for one workspace walk."""
        root = AsyncPath(self.memory_root)
        topics: list[MemoryWorkspaceTopic] = []
        diagnostics: list[MemoryWorkspaceDiagnostic] = []

        if not await root.exists():
            return MemoryWorkspaceScanResult(topics=topics, diagnostics=diagnostics)
        if await root.is_symlink():
            diagnostics.append(
                MemoryWorkspaceDiagnostic(
                    path=self.memory_root,
                    code="workspace.symlinked_root",
                    message="memory root must not be a symlink",
                )
            )
            return MemoryWorkspaceScanResult(topics=topics, diagnostics=diagnostics)
        if not await root.is_dir():
            diagnostics.append(
                MemoryWorkspaceDiagnostic(
                    path=self.memory_root,
                    code="workspace.invalid_root",
                    message="memory root is not a directory",
                )
            )
            return MemoryWorkspaceScanResult(topics=topics, diagnostics=diagnostics)

        stack: list[AsyncPath] = [root]
        while stack:
            directory = stack.pop()
            await self._scan_directory(
                directory=directory,
                topics=topics,
                diagnostics=diagnostics,
                stack=stack,
            )

        return MemoryWorkspaceScanResult(topics=topics, diagnostics=diagnostics)

    async def _scan_directory(
        self,
        *,
        directory: AsyncPath,
        topics: list[MemoryWorkspaceTopic],
        diagnostics: list[MemoryWorkspaceDiagnostic],
        stack: list[AsyncPath],
    ) -> None:
        """Collect valid entries from one directory into shared scan buffers."""
        async for entry in directory.iterdir():
            if _is_ignored_entry(entry.name):
                continue
            if await entry.is_symlink():
                diagnostics.append(
                    MemoryWorkspaceDiagnostic(
                        path=Path(str(entry)),
                        code="path.symlink",
                        message="workspace entries cannot be symlinks",
                    )
                )
                continue

            if await entry.is_dir():
                stack.append(entry)
                continue
            if not await entry.is_file():
                continue
            if entry.suffix.lower() != ".md":
                continue

            topic, entry_diagnostics = await self._scan_file(entry)
            diagnostics.extend(entry_diagnostics)
            if topic is not None:
                topics.append(topic)

    async def _scan_file(
        self, path: AsyncPath
    ) -> tuple[MemoryWorkspaceTopic | None, list[MemoryWorkspaceDiagnostic]]:
        """Parse one `.md` file to a validated topic and diagnostics."""
        current = Path(str(path))
        file_diagnostics: list[MemoryWorkspaceDiagnostic] = []

        try:
            normalized = await _read_file_text_or_raise(path, current)
            raw_frontmatter, body = _extract_frontmatter_blocks_or_raise(
                normalized, current
            )
            frontmatter = _decode_frontmatter_or_raise(raw_frontmatter, current)
            title = _decode_title_or_raise(frontmatter, current)
        except _BlockingScanError as error:
            return None, [error.diagnostic]

        evidence, evidence_diagnostic = _decode_evidence(frontmatter, current)
        if evidence_diagnostic is not None:
            file_diagnostics.append(evidence_diagnostic)

        context_always, context_diagnostic = _decode_context(frontmatter, current)
        if context_diagnostic is not None:
            file_diagnostics.append(context_diagnostic)

        review_after, review_after_diagnostic = _decode_review_after(
            frontmatter, current
        )
        if review_after_diagnostic is not None:
            file_diagnostics.append(review_after_diagnostic)

        topic = MemoryWorkspaceTopic(
            path=current,
            title=title,
            frontmatter=frontmatter,
            body=body,
            evidence=evidence,
            context_always=context_always,
            review_after=review_after,
        )
        return topic, file_diagnostics


__all__ = [
    "MemoryWorkspace",
    "MemoryWorkspaceDiagnostic",
    "MemoryWorkspaceScanResult",
    "MemoryWorkspaceTopic",
]
