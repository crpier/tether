"""Route contract behavior tests."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from snektest import assert_eq, assert_in, assert_raises, test
from starlette import status
from starlette.applications import Starlette

from tether.api import (
    ApiContractError,
    ApiError,
    ApiModel,
    ApiMount,
    ApiRouter,
    BodyParam,
    PathParam,
    QueryParam,
    route_contract,
)


async def _make_object_context() -> object:
    return object()


async def _make_str_context() -> str:
    return "ctx"


@test(mark="fast")
async def decorated_handler_remains_directly_callable() -> None:
    """The router decorator records metadata without wrapping the handler."""

    router = ApiRouter(
        prefix="/status",
        tags=["Status"],
        security=None,
        ctx_factory=_make_str_context,
    )

    @router("GET", "/ping", status=status.HTTP_200_OK)
    async def ping(context: str) -> str:
        """Return a health string."""
        return f"pong:{context}"

    assert_eq(await ping("ctx"), "pong:ctx")
    assert_eq(route_contract(ping).method, "GET")
    assert_eq(route_contract(ping).path, "/ping")
    assert_eq(route_contract(ping).status_code, status.HTTP_200_OK)
    assert_eq(router.handlers, (ping,))


@test(mark="medium")
async def route_adapter_validates_request_parameters() -> None:
    """Validated path, query, and body values are passed to the handler."""

    @dataclass(frozen=True)
    class RequestContext:
        user_name: str

    class PatchMemoryBody(ApiModel):
        content: str

    class MemoryOut(ApiModel):
        content: str
        include_version: bool
        memory_id: UUID
        user_name: str

    observed_calls: list[tuple[RequestContext, UUID, bool, PatchMemoryBody]] = []

    async def make_context() -> RequestContext:
        return RequestContext(user_name="ada")

    router = ApiRouter(
        prefix="/memories",
        tags=["Memories"],
        security=None,
        ctx_factory=make_context,
    )

    @router("PATCH", "/{memory_id}", status=status.HTTP_200_OK)
    async def patch_memory(
        context: RequestContext,
        *,
        memory_id: PathParam[UUID],
        include_version: QueryParam[bool],
        body: BodyParam[PatchMemoryBody],
    ) -> MemoryOut:
        """Patch one Memory."""
        observed_calls.append((context, memory_id, include_version, body))
        return MemoryOut(
            content=body.content,
            include_version=include_version,
            memory_id=memory_id,
            user_name=context.user_name,
        )

    mount = ApiMount(routes={"/api": [router]})
    app = Starlette(routes=mount.build_routes(title="Tether API", version="0.1.0"))
    memory_id = uuid4()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.patch(
            f"/api/memories/{memory_id}?include_version=true",
            json={"content": "updated"},
        )

    assert_eq(response.status_code, status.HTTP_200_OK)
    assert_eq(
        response.json(),
        {
            "content": "updated",
            "include_version": True,
            "memory_id": str(memory_id),
            "user_name": "ada",
        },
    )
    assert_eq(observed_calls[0][0], RequestContext(user_name="ada"))
    assert_eq(observed_calls[0][1], memory_id)
    assert_eq(observed_calls[0][2], True)
    assert_eq(observed_calls[0][3], PatchMemoryBody(content="updated"))


@test(mark="fast")
def openapi_contains_public_and_internal_runtime_paths() -> None:
    """One schema includes full runtime paths for every mounted surface."""

    memory_router = ApiRouter(
        prefix="/memories",
        tags=["Memories"],
        security="human_session",
        auth_errors=[status.HTTP_401_UNAUTHORIZED],
        ctx_factory=_make_object_context,
    )

    @memory_router("GET", "/{memory_id}", status=status.HTTP_200_OK)
    async def fetch_memory(
        context: object,
        *,
        memory_id: PathParam[UUID],
    ) -> None:
        """Fetch one Memory."""

    tool_router = ApiRouter(
        prefix="/tools",
        tags=["Tools"],
        security="tool_secret",
        auth_errors=[status.HTTP_401_UNAUTHORIZED],
        ctx_factory=_make_object_context,
    )

    @tool_router("POST", "/ping", status=status.HTTP_200_OK)
    async def ping_tool(context: object) -> None:
        """Ping the internal tool surface."""

    mount = ApiMount(
        routes={"/api": [memory_router], "/internal": [tool_router]},
    )
    schema = mount.openapi_schema(title="Tether API", version="0.1.0")

    assert_eq(schema["openapi"], "3.1.0")
    assert_in("/api/memories/{memory_id}", schema["paths"])
    assert_in("/internal/tools/ping", schema["paths"])


@test(mark="fast")
def operations_do_not_emit_audience_metadata() -> None:
    """Audience is no longer a contract concept, so no `x-audience` is emitted."""

    router = ApiRouter(
        prefix="/memories",
        tags=["Memories"],
        security=None,
        ctx_factory=_make_object_context,
    )

    @router("GET", "/{memory_id}", status=status.HTTP_200_OK)
    async def fetch_memory(
        context: object,
        *,
        memory_id: PathParam[UUID],
    ) -> None:
        """Fetch one Memory."""

    mount = ApiMount(routes={"/api": [router]})
    schema: Any = mount.openapi_schema(title="Tether API", version="0.1.0")

    operation = schema["paths"]["/api/memories/{memory_id}"]["get"]
    assert_eq("x-audience" in operation, False)


@test(mark="medium")
async def docs_load_swagger_ui_from_cdn() -> None:
    """Swagger UI is served at `/docs` and reads `/openapi.json`."""

    router = ApiRouter(
        prefix="/status",
        tags=["Status"],
        security=None,
        ctx_factory=_make_object_context,
    )

    @router("GET", "/ping", status=status.HTTP_204_NO_CONTENT)
    async def ping(context: object) -> None:
        """Ping the API."""

    mount = ApiMount(routes={"/api": [router]})
    app = Starlette(routes=mount.build_routes(title="Tether API", version="0.1.0"))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/docs")

    assert_eq(response.status_code, status.HTTP_200_OK)
    assert_in("cdn.jsdelivr.net/npm/swagger-ui-dist", response.text)
    assert_in("/openapi.json", response.text)


@test(mark="medium")
async def openapi_json_route_serves_cached_schema() -> None:
    """The schema endpoint returns the build-time OpenAPI object."""

    router = ApiRouter(
        prefix="/status",
        tags=["Status"],
        security=None,
        ctx_factory=_make_object_context,
    )

    @router("GET", "/ping", status=status.HTTP_204_NO_CONTENT)
    async def ping(context: object) -> None:
        """Ping the API."""

    mount = ApiMount(routes={"/api": [router]})
    app = Starlette(routes=mount.build_routes(title="Tether API", version="0.1.0"))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/openapi.json")

    assert_eq(response.status_code, status.HTTP_200_OK)
    assert_eq(
        response.json(),
        mount.openapi_schema(title="Tether API", version="0.1.0"),
    )


@test(mark="medium")
async def unknown_query_parameters_are_rejected() -> None:
    """Unexpected query keys return `422 ErrorOut` before handler invocation."""

    observed_calls: list[object] = []

    router = ApiRouter(
        prefix="/status",
        tags=["Status"],
        security=None,
        ctx_factory=_make_object_context,
    )

    @router("GET", "/ping", status=status.HTTP_200_OK)
    async def ping(
        context: object,
        *,
        include_version: QueryParam[bool],
    ) -> None:
        """Ping the API."""
        observed_calls.append(context)

    mount = ApiMount(routes={"/api": [router]})
    app = Starlette(routes=mount.build_routes(title="Tether API", version="0.1.0"))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/status/ping?include_version=true&extra=nope")

    assert_eq(response.status_code, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert_eq(response.json()["code"], "validation_error")
    assert_eq(
        response.json()["details"]["errors"][0]["loc"],
        ["query", "extra"],
    )
    assert_eq(observed_calls, [])


@test(mark="medium")
async def repeated_scalar_query_parameters_are_rejected() -> None:
    """Scalar query params may appear at most once."""

    observed_calls: list[object] = []

    router = ApiRouter(
        prefix="/status",
        tags=["Status"],
        security=None,
        ctx_factory=_make_object_context,
    )

    @router("GET", "/ping", status=status.HTTP_200_OK)
    async def ping(
        context: object,
        *,
        include_version: QueryParam[bool],
    ) -> None:
        """Ping the API."""
        observed_calls.append(context)

    mount = ApiMount(routes={"/api": [router]})
    app = Starlette(routes=mount.build_routes(title="Tether API", version="0.1.0"))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/api/status/ping?include_version=true&include_version=false"
        )

    assert_eq(response.status_code, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert_eq(response.json()["code"], "validation_error")
    assert_eq(
        response.json()["details"]["errors"][0]["loc"],
        ["query", "include_version"],
    )
    assert_eq(observed_calls, [])


@test(mark="medium")
async def invalid_path_params_include_source_prefixed_location() -> None:
    """Path validation errors identify the path parameter name."""

    observed_calls: list[object] = []

    router = ApiRouter(
        prefix="/memories",
        tags=["Memories"],
        security=None,
        ctx_factory=_make_object_context,
    )

    @router("GET", "/{memory_id}", status=status.HTTP_204_NO_CONTENT)
    async def fetch_memory(
        context: object,
        *,
        memory_id: PathParam[UUID],
    ) -> None:
        """Fetch one Memory."""
        observed_calls.append(memory_id)

    mount = ApiMount(routes={"/api": [router]})
    app = Starlette(routes=mount.build_routes(title="Tether API", version="0.1.0"))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/memories/not-a-uuid")

    assert_eq(response.status_code, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert_eq(
        response.json()["details"]["errors"][0]["loc"],
        ["path", "memory_id"],
    )
    assert_eq(observed_calls, [])


@test(mark="medium")
async def unsupported_body_content_type_returns_error() -> None:
    """Body routes accept JSON only."""

    class PatchBody(ApiModel):
        content: str

    observed_calls: list[object] = []

    router = ApiRouter(
        prefix="/status",
        tags=["Status"],
        security=None,
        ctx_factory=_make_object_context,
    )

    @router("PATCH", "/ping", status=status.HTTP_204_NO_CONTENT)
    async def ping(context: object, *, body: BodyParam[PatchBody]) -> None:
        """Ping the API."""
        observed_calls.append(body)

    mount = ApiMount(routes={"/api": [router]})
    app = Starlette(routes=mount.build_routes(title="Tether API", version="0.1.0"))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.patch(
            "/api/status/ping",
            content="content=bad",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    assert_eq(response.status_code, status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)
    assert_eq(response.json()["code"], "unsupported_media_type")
    assert_eq(observed_calls, [])


@test(mark="medium")
async def malformed_json_returns_validation_error() -> None:
    """Invalid JSON bodies return `422 ErrorOut`."""

    class PatchBody(ApiModel):
        content: str

    observed_calls: list[object] = []

    router = ApiRouter(
        prefix="/status",
        tags=["Status"],
        security=None,
        ctx_factory=_make_object_context,
    )

    @router("PATCH", "/ping", status=status.HTTP_204_NO_CONTENT)
    async def ping(context: object, *, body: BodyParam[PatchBody]) -> None:
        """Ping the API."""
        observed_calls.append(body)

    mount = ApiMount(routes={"/api": [router]})
    app = Starlette(routes=mount.build_routes(title="Tether API", version="0.1.0"))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.patch(
            "/api/status/ping",
            content="{not-json",
            headers={"content-type": "application/json; charset=utf-8"},
        )

    assert_eq(response.status_code, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert_eq(response.json()["code"], "validation_error")
    assert_eq(response.json()["details"]["errors"][0]["loc"], ["body"])
    assert_eq(observed_calls, [])


@test(mark="medium")
async def invalid_body_shape_returns_source_prefixed_location() -> None:
    """Body validation errors prefix Pydantic locations with `body`."""

    class PatchBody(ApiModel):
        content: str

    observed_calls: list[object] = []

    router = ApiRouter(
        prefix="/status",
        tags=["Status"],
        security=None,
        ctx_factory=_make_object_context,
    )

    @router("PATCH", "/ping", status=status.HTTP_204_NO_CONTENT)
    async def ping(context: object, *, body: BodyParam[PatchBody]) -> None:
        """Ping the API."""
        observed_calls.append(body)

    mount = ApiMount(routes={"/api": [router]})
    app = Starlette(routes=mount.build_routes(title="Tether API", version="0.1.0"))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.patch("/api/status/ping", json={"extra": "nope"})

    assert_eq(response.status_code, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert_eq(response.json()["details"]["errors"][0]["loc"], ["body", "content"])
    assert_eq(observed_calls, [])


@test(mark="medium")
async def declared_api_error_status_is_returned() -> None:
    """Handler `ApiError` statuses are exposed only when declared."""

    router = ApiRouter(
        prefix="/memories",
        tags=["Memories"],
        security=None,
        ctx_factory=_make_object_context,
    )

    @router(
        "GET",
        "/missing",
        status=status.HTTP_200_OK,
        errors=[status.HTTP_404_NOT_FOUND],
    )
    async def missing(context: object) -> None:
        """Fetch a missing Memory."""
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            "missing_memory",
            "Memory not found.",
            details={"memory_id": "abc"},
        )

    mount = ApiMount(routes={"/api": [router]})
    app = Starlette(routes=mount.build_routes(title="Tether API", version="0.1.0"))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/memories/missing")

    assert_eq(response.status_code, status.HTTP_404_NOT_FOUND)
    assert_eq(
        response.json(),
        {
            "code": "missing_memory",
            "details": {"memory_id": "abc"},
            "message": "Memory not found.",
        },
    )


@test(mark="medium")
async def undeclared_api_error_status_becomes_internal_error() -> None:
    """Undeclared handler `ApiError` statuses do not leak to clients."""

    router = ApiRouter(
        prefix="/memories",
        tags=["Memories"],
        security=None,
        ctx_factory=_make_object_context,
    )

    @router("GET", "/conflict", status=status.HTTP_200_OK)
    async def conflict(context: object) -> None:
        """Fetch a conflicting Memory."""
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "version_conflict",
            "Memory version conflict.",
        )

    mount = ApiMount(routes={"/api": [router]})
    app = Starlette(routes=mount.build_routes(title="Tether API", version="0.1.0"))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/memories/conflict")

    assert_eq(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
    assert_eq(
        response.json(),
        {
            "code": "internal_error",
            "details": {},
            "message": "Internal server error.",
        },
    )


@test(mark="medium")
async def declared_context_auth_error_status_is_returned() -> None:
    """Context factories expose only group-declared auth errors."""

    observed_calls: list[object] = []

    async def make_context() -> object:
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED,
            "not_authenticated",
            "Authentication required.",
        )

    router = ApiRouter(
        prefix="/memories",
        tags=["Memories"],
        security="human_session",
        auth_errors=[status.HTTP_401_UNAUTHORIZED],
        ctx_factory=make_context,
    )

    @router("GET", "/private", status=status.HTTP_204_NO_CONTENT)
    async def private(context: object) -> None:
        """Fetch a private resource."""
        observed_calls.append(context)

    mount = ApiMount(routes={"/api": [router]})
    app = Starlette(routes=mount.build_routes(title="Tether API", version="0.1.0"))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/memories/private")

    assert_eq(response.status_code, status.HTTP_401_UNAUTHORIZED)
    assert_eq(response.json()["code"], "not_authenticated")
    assert_eq(observed_calls, [])


@test(mark="medium")
async def response_serialization_uses_return_annotation() -> None:
    """Annotated responses are dumped as JSON without handler-side conversion."""

    class MemoryState(Enum):
        TETHERED = "tethered"

    class MemoryOut(ApiModel):
        captured_at: datetime
        memory_id: UUID
        state: MemoryState

    memory_id = uuid4()
    captured_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    router = ApiRouter(
        prefix="/memory-search",
        tags=["Memories"],
        security=None,
        ctx_factory=_make_object_context,
    )

    @router("GET", "/memories", status=status.HTTP_200_OK)
    async def list_memories(context: object) -> list[MemoryOut]:
        """List Memories."""
        return [
            MemoryOut(
                captured_at=captured_at,
                memory_id=memory_id,
                state=MemoryState.TETHERED,
            )
        ]

    mount = ApiMount(routes={"/api": [router]})
    app = Starlette(routes=mount.build_routes(title="Tether API", version="0.1.0"))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/memory-search/memories")

    assert_eq(
        response.json(),
        [
            {
                "captured_at": "2026-01-02T03:04:05Z",
                "memory_id": str(memory_id),
                "state": "tethered",
            }
        ],
    )


@test(mark="medium")
async def none_response_has_empty_body() -> None:
    """`-> None` handlers return no JSON body."""

    router = ApiRouter(
        prefix="/memories",
        tags=["Memories"],
        security=None,
        ctx_factory=_make_object_context,
    )

    @router("DELETE", "/gone", status=status.HTTP_204_NO_CONTENT)
    async def delete_memory(context: object) -> None:
        """Delete one Memory."""

    mount = ApiMount(routes={"/api": [router]})
    app = Starlette(routes=mount.build_routes(title="Tether API", version="0.1.0"))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.delete("/api/memories/gone")
        schema_response = await client.get("/openapi.json")

    assert_eq(response.status_code, status.HTTP_204_NO_CONTENT)
    assert_eq(response.content, b"")
    assert_eq(
        schema_response.json()["paths"]["/api/memories/gone"]["delete"]["responses"][
            "204"
        ],
        {"description": "Successful response."},
    )


@test(mark="medium")
async def api_model_components_are_suffixed_by_schema_mode() -> None:
    """Input and output DTO components have deterministic names."""

    class MemoryContent(ApiModel):
        text: str

    class PatchMemoryBody(ApiModel):
        content: MemoryContent

    class MemoryOut(ApiModel):
        content: MemoryContent
        memory_id: UUID

    router = ApiRouter(
        prefix="/memories",
        tags=["Memories"],
        security=None,
        ctx_factory=_make_object_context,
    )

    @router("PATCH", "/{memory_id}", status=status.HTTP_200_OK)
    async def patch_memory(
        context: object,
        *,
        memory_id: PathParam[UUID],
        body: BodyParam[PatchMemoryBody],
    ) -> MemoryOut:
        """Patch one Memory."""
        return MemoryOut(content=body.content, memory_id=memory_id)

    mount = ApiMount(routes={"/api": [router]})
    schema: Any = mount.openapi_schema(title="Tether API", version="0.1.0")

    assert_eq(
        sorted(schema["components"]["schemas"]),
        [
            "ErrorOutOutput",
            "MemoryContentInput",
            "MemoryContentOutput",
            "MemoryOutOutput",
            "PatchMemoryBodyInput",
        ],
    )


@test(mark="medium")
async def enum_components_are_not_suffixed() -> None:
    """Enum schemas are shared components across validation and serialization."""

    class MemoryState(Enum):
        LOOSE = "loose"

    class MemoryOut(ApiModel):
        memory_id: UUID
        state: MemoryState

    router = ApiRouter(
        prefix="/memories",
        tags=["Memories"],
        security=None,
        ctx_factory=_make_object_context,
    )

    @router("GET", "/{memory_id}", status=status.HTTP_200_OK)
    async def fetch_memory(
        context: object,
        *,
        memory_id: PathParam[UUID],
    ) -> MemoryOut:
        """Fetch one Memory."""
        return MemoryOut(memory_id=memory_id, state=MemoryState.LOOSE)

    mount = ApiMount(routes={"/api": [router]})
    schema: Any = mount.openapi_schema(title="Tether API", version="0.1.0")

    assert_eq(
        sorted(schema["components"]["schemas"]),
        ["ErrorOutOutput", "MemoryOutOutput", "MemoryState"],
    )


@test(mark="fast")
def non_api_model_response_fails_at_build_time() -> None:
    """Pydantic DTOs in API schemas must use the shared API base class."""

    class PersistenceMemory(BaseModel):
        content: str

    router = ApiRouter(
        prefix="/memories",
        tags=["Memories"],
        security=None,
        ctx_factory=_make_object_context,
    )

    @router("GET", "/memory", status=status.HTTP_200_OK)
    async def fetch_memory(context: object) -> PersistenceMemory:
        """Fetch one Memory."""
        return PersistenceMemory(content="nope")

    mount = ApiMount(routes={"/api": [router]})
    with assert_raises(ApiContractError):
        mount.build_routes(title="Tether API", version="0.1.0")


@test(mark="fast")
def duplicate_operation_ids_fail_at_build_time() -> None:
    """Handler names are operation IDs and must be unique."""

    router = ApiRouter(
        prefix="/status",
        tags=["Status"],
        security=None,
        ctx_factory=_make_object_context,
    )

    @router("GET", "/one", status=status.HTTP_204_NO_CONTENT)
    async def ping_one(context: object) -> None:
        """Ping one."""

    @router("GET", "/two", status=status.HTTP_204_NO_CONTENT)
    async def ping_two(context: object) -> None:
        """Ping two."""

    ping_two.__name__ = ping_one.__name__

    mount = ApiMount(routes={"/api": [router]})
    with assert_raises(ApiContractError):
        mount.build_routes(title="Tether API", version="0.1.0")


@test(mark="fast")
def missing_handler_docstring_fails_at_build_time() -> None:
    """Operations require docstrings for summaries and descriptions."""

    router = ApiRouter(
        prefix="/status",
        tags=["Status"],
        security=None,
        ctx_factory=_make_object_context,
    )

    @router("GET", "/ping", status=status.HTTP_204_NO_CONTENT)
    async def ping(context: object) -> None:
        pass

    mount = ApiMount(routes={"/api": [router]})
    with assert_raises(ApiContractError):
        mount.build_routes(title="Tether API", version="0.1.0")


@test(mark="fast")
def empty_routers_fail_at_build_time() -> None:
    """A router with no registered handlers is a mistake."""

    router = ApiRouter(
        prefix="/status",
        tags=["Status"],
        security=None,
        ctx_factory=_make_object_context,
    )

    mount = ApiMount(routes={"/api": [router]})
    with assert_raises(ApiContractError):
        mount.build_routes(title="Tether API", version="0.1.0")
