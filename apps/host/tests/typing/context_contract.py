"""Static guard: ApiRouter binds the handler context type to its ctx_factory.

This is not a runtime test (no ``test_`` prefix, so snektest skips it). Pyright
checks it as part of ``just typecheck``. The router below yields ``PublicCtx``,
so:

- ``correct_context`` annotates its first parameter ``PublicCtx`` and type-checks
  cleanly;
- ``wrong_context`` annotates ``ToolCtx`` and must keep producing a
  ``reportArgumentType`` error. If the enforcement ever regresses, the
  ``pyright: ignore`` below turns unnecessary and fails the type check via
  ``reportUnnecessaryTypeIgnoreComment``.
"""

from dataclasses import dataclass

from starlette import status

from tether.api import ApiRouter, PathParam


@dataclass(frozen=True, slots=True)
class PublicCtx:
    user_name: str


@dataclass(frozen=True, slots=True)
class ToolCtx:
    tool_name: str


async def make_public_ctx() -> PublicCtx:
    return PublicCtx(user_name="demo")


router = ApiRouter(
    prefix="/memories",
    tags=["Memories"],
    security=None,
    ctx_factory=make_public_ctx,
)


@router("GET", "/{memory_id}", status=status.HTTP_200_OK)
async def correct_context(context: PublicCtx, *, memory_id: PathParam[int]) -> int:
    """First parameter matches the router context: no type error."""
    return memory_id


@router("DELETE", "/{memory_id}", status=status.HTTP_204_NO_CONTENT)  # pyright: ignore[reportArgumentType]
async def wrong_context(context: ToolCtx, *, memory_id: PathParam[int]) -> None:
    """First parameter is ToolCtx but the router yields PublicCtx: type error."""
