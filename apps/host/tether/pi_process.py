"""Construction policy for host-owned pi subprocesses."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from anyio import Path as AsyncPath

from tether.tool_runtime import SessionRegistry

BUNDLED_PI_SKILL_NAMES = ("grilling", "writing-great-skills")
"""Release-managed skills explicitly allowlisted into every pi process."""


@dataclass(frozen=True)
class PiRuntimeConfig:
    """Configuration for one host-owned pi subprocess."""

    tool_base_url: str
    tool_secret: str
    cwd: Path | None = None
    extra_extension_paths: Sequence[Path] = field(default_factory=tuple)
    extension_path: Path | None = None
    pi_binary: Path | None = None
    session_dir: Path | None = None
    session_id: str | None = None
    system_prompt: str | None = None


class PiSpawner[RuntimeT](Protocol):
    """Injectable process-spawn seam used by persistent and ephemeral owners."""

    async def __call__(
        self,
        config: PiRuntimeConfig,
        *,
        session_registry: SessionRegistry,
    ) -> RuntimeT:
        """Spawn and register a pi runtime for `config`."""
        ...


@dataclass(frozen=True, slots=True)
class PiSpawnRequest:
    """Shared inputs for resolving a session directory and spawning pi."""

    session_dir: Path | Callable[[str], Path]
    session_id: str | None
    system_prompt: str
    tool_base_url: str
    tool_secret: str
    extra_extension_paths: Sequence[Path] = field(default_factory=tuple)
    pi_binary: Path | None = None


async def spawn_pi_runtime[RuntimeT](
    request: PiSpawnRequest,
    *,
    session_registry: SessionRegistry,
    spawn: PiSpawner[RuntimeT],
) -> tuple[RuntimeT, str]:
    """Resolve process identity and storage before delegating process ownership."""
    resolved_session_id = request.session_id or str(uuid.uuid7())
    resolved_session_dir = (
        request.session_dir(resolved_session_id)
        if callable(request.session_dir)
        else request.session_dir
    )
    await AsyncPath(resolved_session_dir).mkdir(parents=True, exist_ok=True)
    runtime = await spawn(
        PiRuntimeConfig(
            tool_base_url=request.tool_base_url,
            tool_secret=request.tool_secret,
            extra_extension_paths=request.extra_extension_paths,
            pi_binary=request.pi_binary,
            session_dir=resolved_session_dir,
            session_id=resolved_session_id,
            system_prompt=request.system_prompt,
        ),
        session_registry=session_registry,
    )
    return runtime, resolved_session_id


def _repo_root() -> Path:
    """Return the repository root from the installed host package layout."""
    return Path(__file__).resolve().parents[3]


def build_pi_spawn_command(config: PiRuntimeConfig, session_id: str) -> list[str]:
    """Build the closed-tool-world command line for one pi process."""
    agent_root = _repo_root() / "apps/agent"
    command = [
        str(config.pi_binary or agent_root / "node_modules/.bin/pi"),
        "--mode",
        "rpc",
        "--no-builtin-tools",
        "--no-extensions",
        "--no-skills",
        "--approve",
        "--session-id",
        session_id,
    ]
    if config.session_dir is not None:
        command.extend(["--session-dir", str(config.session_dir)])
    if config.system_prompt is not None:
        command.extend(["--system-prompt", config.system_prompt, "--no-context-files"])
    for extension_path in [
        config.extension_path or agent_root / "src/generated/index.ts",
        agent_root / "src/restricted-skill-read.ts",
        *config.extra_extension_paths,
    ]:
        command.extend(["-e", str(extension_path)])
    for skill_name in BUNDLED_PI_SKILL_NAMES:
        command.extend(["--skill", str(agent_root / "skills" / skill_name)])
    return command


def build_pi_spawn_environment(
    config: PiRuntimeConfig, session_id: str
) -> dict[str, str]:
    """Inject loopback credentials and process identity into pi."""
    environment = os.environ.copy()
    environment.update(
        {
            "TETHER_TOOL_BASE_URL": config.tool_base_url,
            "TETHER_TOOL_SECRET": config.tool_secret,
            "TETHER_TOOL_SESSION_ID": session_id,
        }
    )
    return environment


__all__ = [
    "BUNDLED_PI_SKILL_NAMES",
    "PiRuntimeConfig",
    "PiSpawnRequest",
    "PiSpawner",
    "build_pi_spawn_command",
    "build_pi_spawn_environment",
    "spawn_pi_runtime",
]
