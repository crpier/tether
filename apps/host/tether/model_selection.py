"""Curated agent model allowlist and HTTP surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tether.openapi import EndpointRoute, endpoint

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh"]
"""Reasoning-effort levels the pi `set_thinking_level` RPC accepts."""


class ModelSelectionConfigError(Exception):
    """Raised when the host model allowlist is internally inconsistent."""


class ModelNotAllowedError(Exception):
    """Raised when a conversation selects a model outside the allowlist."""


@dataclass(frozen=True, slots=True)
class AgentModelConfig:
    """One operator-enabled model choice.

    ```python
    model = AgentModelConfig(
        id="cheap",
        provider="faux",
        model_id="faux-small",
        display_name="Cheap Faux",
    )
    assert model.id == "cheap"
    ```
    """

    display_name: str
    id: str
    model_id: str
    provider: str
    thinking_level: ThinkingLevel | None = None
    """Reasoning effort to request via `set_thinking_level` after `set_model`.

    `None` means the model has no configured thinking level and the runtime
    skips the RPC entirely, matching pre-thinking-level behaviour."""


@dataclass(frozen=True, slots=True)
class AgentModelCatalog:
    """Validated lookup table for host-curated model choices."""

    default_model: str | None
    models: tuple[AgentModelConfig, ...]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for model in self.models:
            if model.id in seen:
                message = f"duplicate model id: {model.id}"
                raise ModelSelectionConfigError(message)
            seen.add(model.id)
        if self.default_model is not None and self.default_model not in seen:
            message = "default model is not present in the allowlist"
            raise ModelSelectionConfigError(message)

    @classmethod
    def from_legacy_default(
        cls,
        *,
        default_model_id: str | None,
        default_model_provider: str | None,
    ) -> AgentModelCatalog:
        """Build a catalog from the pre-allowlist default fields when present."""
        if default_model_id is None or default_model_provider is None:
            return cls(default_model=None, models=())
        return cls(
            default_model=default_model_id,
            models=(
                AgentModelConfig(
                    display_name=default_model_id,
                    id=default_model_id,
                    model_id=default_model_id,
                    provider=default_model_provider,
                ),
            ),
        )

    @property
    def default_config(self) -> AgentModelConfig | None:
        """Return the configured global default model, if the host has one."""
        if self.default_model is None:
            return None
        return self.resolve(self.default_model)

    def resolve(self, selected_model: str | None) -> AgentModelConfig | None:
        """Return the concrete provider model for a stored selection id."""
        if selected_model is None:
            return self.default_config
        for model in self.models:
            if model.id == selected_model:
                return model
        raise ModelNotAllowedError(selected_model)


class AgentModelRead(BaseModel):
    """HTTP representation of one enabled model choice."""

    display_name: str
    id: str
    model_id: str
    provider: str
    thinking_level: ThinkingLevel | None

    @classmethod
    def from_config(cls, model: AgentModelConfig) -> AgentModelRead:
        """Render an allowlist entry for browser clients."""
        return cls(
            display_name=model.display_name,
            id=model.id,
            model_id=model.model_id,
            provider=model.provider,
            thinking_level=model.thinking_level,
        )


class ModelListRead(BaseModel):
    """HTTP response containing the curated allowlist and global default."""

    default_model: str | None
    models: list[AgentModelRead]


@endpoint(response=ModelListRead)
async def list_models(request: Request) -> Response:
    """List host-enabled agent model choices."""
    catalog = request.app.state.model_catalog
    return JSONResponse(
        ModelListRead(
            default_model=catalog.default_model,
            models=[AgentModelRead.from_config(model) for model in catalog.models],
        ).model_dump(mode="json")
    )


model_routes: list[Route] = [EndpointRoute("/api/models", list_models, methods=["GET"])]
