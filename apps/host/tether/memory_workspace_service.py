"""Domain service surface for canonical Memory workspace discovery."""

from __future__ import annotations

from pathlib import Path
from re import findall

from tether.memory_projection import memory_projection_root
from tether.memory_workspace import (
    MemoryWorkspace,
    MemoryWorkspaceDiagnostic,
    MemoryWorkspaceScanResult,
    MemoryWorkspaceTopic,
)
from tether.structured_logging import Logger

_SEARCH_STOP_WORDS = frozenset(
    {"a", "an", "and", "have", "i", "in", "is", "of", "the", "what"}
)
_SEARCH_SUFFIXES = ("ing", "ed", "es", "s")


def _search_terms(text: str) -> set[str]:
    """Normalize lightweight lexical terms for the small-corpus direct path."""
    terms: set[str] = set()
    for raw_term in findall(r"[\w-]+", text.casefold()):
        if raw_term in _SEARCH_STOP_WORDS:
            continue
        term = raw_term
        for suffix in _SEARCH_SUFFIXES:
            if term.endswith(suffix) and len(term) > len(suffix) + 2:
                term = term[: -len(suffix)]
                break
        terms.add(term)
    return terms


def memory_workspace_root(kb_root: str | Path) -> Path:
    """Return the canonical workspace directory under a knowledge-base root."""

    return memory_projection_root(kb_root)


class MemoryWorkspaceService:
    """Expose Memory workspace scanning as a typed service boundary."""

    def __init__(self, kb_root: str | Path) -> None:
        self.kb_root = Path(kb_root)
        self.workspace_root = memory_workspace_root(self.kb_root)
        self.workspace = MemoryWorkspace(self.workspace_root)

    async def scan(self, logger: Logger) -> MemoryWorkspaceScanResult:
        """Scan the canonical workspace for valid topics and diagnostics.

        The workspace service delegates raw filesystem parsing to
        :class:`~tether.memory_workspace.MemoryWorkspace` and logs whether it
        found valid topics so callers can decide how to proceed.
        """
        result = await self.workspace.scan()
        if result.diagnostics:
            logger.debug(
                "Memory workspace scan found diagnostics",
                topic_count=len(result.topics),
                diagnostics_count=len(result.diagnostics),
                diagnostic_codes=tuple(
                    diagnostic.code for diagnostic in result.diagnostics
                ),
            )
        else:
            logger.debug(
                "Memory workspace scan completed",
                topic_count=len(result.topics),
            )
        return result

    async def search(
        self,
        query: str,
        *,
        limit: int,
        logger: Logger,
    ) -> list[MemoryWorkspaceTopic]:
        """Return current Topics ranked by direct lexical relevance."""
        topics = (await self.scan(logger=logger)).topics
        terms = _search_terms(query)
        if not terms:
            return sorted(topics, key=lambda topic: str(topic.path))[:limit]

        ranked: list[tuple[int, str, MemoryWorkspaceTopic]] = []
        for topic in topics:
            title_terms = _search_terms(topic.title)
            body_terms = _search_terms(topic.body)
            score = sum(
                3 * int(term in title_terms) + int(term in body_terms) for term in terms
            )
            if score:
                ranked.append((score, str(topic.path), topic))
        ranked.sort(key=lambda match: (-match[0], match[1]))
        return [topic for _, _, topic in ranked[:limit]]

    async def render_context(
        self,
        query: str,
        *,
        limit: int,
        logger: Logger,
    ) -> str:
        """Render complete relevant Topics for transient foreground context."""
        topics = await self.search(query, limit=limit, logger=logger)
        if not topics:
            return ""
        rendered_topics = "\n\n".join(
            f"## {topic.title} ({topic.path.relative_to(self.workspace_root)})\n"
            f"{topic.body.strip()}"
            for topic in topics
        )
        return f"<current_memory>\n{rendered_topics}\n</current_memory>"


__all__ = [
    "MemoryWorkspaceDiagnostic",
    "MemoryWorkspaceScanResult",
    "MemoryWorkspaceService",
    "MemoryWorkspaceTopic",
    "memory_workspace_root",
]
