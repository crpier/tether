"""The one ordered registry of every internal tool, across all domains.

Each domain module owns its own `ToolSpec` tuple (the single source of truth for
that domain's tool names, endpoints, params, handlers, and error tables).
`all_tool_specs()` concatenates them in the canonical generated-file order so the
mounted `/internal/tools/*` surface and the pi codegen schema document both
derive from the exact same specs — a tool can never be mounted without a shim,
nor shimmed without an endpoint.
"""

from __future__ import annotations

from tether.artifact_tools import ARTIFACT_TOOL_SPECS
from tether.bucket_tools import BUCKET_TOOL_SPECS
from tether.conversation_history_tools import CONVERSATION_HISTORY_TOOL_SPECS
from tether.recall_tools import RECALL_TOOL_SPECS
from tether.tools import MEMORY_TOOL_SPECS, ToolSpec
from tether.triage_tools import TRIAGE_TOOL_SPECS
from tether.trigger_tools import TRIGGER_TOOL_SPECS
from tether.youtube_tools import YOUTUBE_TOOL_SPECS


def all_tool_specs() -> tuple[ToolSpec, ...]:
    """Every internal tool spec, in the order the generated shims expect.

    >>> {spec.name for spec in all_tool_specs()} >= {"capture", "add_movie"}
    True
    """
    return (
        *MEMORY_TOOL_SPECS,
        *BUCKET_TOOL_SPECS,
        *TRIAGE_TOOL_SPECS,
        *YOUTUBE_TOOL_SPECS,
        *TRIGGER_TOOL_SPECS,
        *RECALL_TOOL_SPECS,
        *CONVERSATION_HISTORY_TOOL_SPECS,
        *ARTIFACT_TOOL_SPECS,
    )
