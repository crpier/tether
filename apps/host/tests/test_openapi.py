"""Behavior tests for request validation and OpenAPI generation.

Two seams are exercised without a live server. `endpoint` is driven by calling
the wrapped handler with a hand-built Starlette `Request` and asserting on the
returned `Response` — validation failures surface as HTTP status codes, valid
input reaches the handler as a parsed model. `build_openapi` is a pure function
over a list of routes, so it is asserted on directly.
"""

import json
from typing import Literal

from pydantic import BaseModel
from snektest import assert_eq, assert_in, assert_not_in, test
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tether.openapi import build_openapi, endpoint


def post_request(payload: object) -> Request:
    """A JSON POST request whose body is `payload`."""
    encoded = json.dumps(payload).encode()

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": encoded, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )


def get_request(query_string: bytes) -> Request:
    """A GET request carrying `query_string` and no body."""

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": query_string,
            "headers": [],
        },
        receive,
    )


class Capture(BaseModel):
    """A request body with a single required `text` field."""

    text: str


class Filter(BaseModel):
    """A query model with a required integer `limit`."""

    limit: int


type QueryState = Literal["open", "closed"]


class StateFilter(BaseModel):
    """A query model whose field schema is emitted through `$defs`."""

    state: QueryState


def echo_routes() -> list[Route]:
    """A POST-with-body and a GET-with-path-param route for spec assertions."""

    @endpoint(request_body=Capture, response=Capture, status=201)
    async def create(request: Request, body: Capture) -> Response: ...

    @endpoint(response=Capture, response_is_list=True)
    async def read(request: Request) -> Response: ...

    return [
        Route("/things", create, methods=["POST"]),
        Route("/things/{thing_id}", read, methods=["GET"]),
    ]


@test()
async def endpoint_rejects_a_body_that_fails_validation() -> None:
    """A request body that violates the model yields HTTP 422."""

    @endpoint(request_body=Capture)
    async def handler(_request: Request, body: Capture) -> Response:
        return JSONResponse({"text": body.text})

    response = await handler(post_request({"wrong": "field"}))

    assert_eq(response.status_code, 422)


@test()
async def endpoint_passes_a_valid_body_to_the_handler() -> None:
    """A valid body is parsed into the model and handed to the handler."""

    @endpoint(request_body=Capture, status=201)
    async def handler(_request: Request, body: Capture) -> Response:
        return JSONResponse({"echo": body.text}, status_code=201)

    response = await handler(post_request({"text": "hello"}))

    assert_eq(json.loads(bytes(response.body)), {"echo": "hello"})


@test()
async def endpoint_rejects_malformed_json_with_400() -> None:
    """A body that is not JSON at all is a client error distinct from 422."""

    @endpoint(request_body=Capture)
    async def handler(_request: Request, body: Capture) -> Response:
        return JSONResponse({"text": body.text})

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"not json", "more_body": False}

    request = Request(
        {"type": "http", "method": "POST", "path": "/", "headers": []},
        receive,
    )
    response = await handler(request)

    assert_eq(response.status_code, 400)


@test()
async def endpoint_rejects_query_params_that_fail_validation() -> None:
    """A query string that violates the query model yields HTTP 422."""

    @endpoint(query=Filter)
    async def handler(_request: Request, query: Filter) -> Response:
        return JSONResponse({"limit": query.limit})

    response = await handler(get_request(b"limit=not-an-int"))

    assert_eq(response.status_code, 422)


@test()
async def endpoint_passes_validated_query_to_the_handler() -> None:
    """A valid query string is parsed and coerced into the query model."""

    @endpoint(query=Filter)
    async def handler(_request: Request, query: Filter) -> Response:
        return JSONResponse({"limit": query.limit})

    response = await handler(get_request(b"limit=5"))

    assert_eq(json.loads(bytes(response.body)), {"limit": 5})


@test()
async def build_openapi_lists_registered_paths() -> None:
    """Every route's path and method appears in the document's paths."""
    document = build_openapi(echo_routes(), title="Things", version="1.0.0")

    assert_in("post", document["paths"]["/things"])


@test()
async def build_openapi_references_the_request_body_schema() -> None:
    """A body route points its requestBody at the registered component schema."""
    document = build_openapi(echo_routes(), title="Things", version="1.0.0")

    body_schema = document["paths"]["/things"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]

    assert_eq(body_schema, {"$ref": "#/components/schemas/Capture"})


@test()
async def build_openapi_registers_referenced_models_as_components() -> None:
    """Referenced models are emitted once under components/schemas."""
    document = build_openapi(echo_routes(), title="Things", version="1.0.0")

    assert_in("Capture", document["components"]["schemas"])


@test()
async def build_openapi_derives_path_parameters_from_the_template() -> None:
    """A `{thing_id}` placeholder becomes a required path parameter."""
    document = build_openapi(echo_routes(), title="Things", version="1.0.0")

    names = [
        parameter["name"]
        for parameter in document["paths"]["/things/{thing_id}"]["get"]["parameters"]
    ]

    assert_in("thing_id", names)


@test()
async def build_openapi_registers_query_parameter_reference_targets() -> None:
    """A query parameter `$ref` points at a document-level component."""

    @endpoint(query=StateFilter)
    async def read(request: Request, query: StateFilter) -> Response: ...

    document = build_openapi(
        [Route("/items", read, methods=["GET"])],
        title="Things",
        version="1.0.0",
    )

    parameter_schema = document["paths"]["/items"]["get"]["parameters"][0]["schema"]

    assert_eq(parameter_schema, {"$ref": "#/components/schemas/QueryState"})
    assert_in("QueryState", document["components"]["schemas"])


@test()
async def build_openapi_wraps_list_responses_in_an_array() -> None:
    """A `response_is_list` handler advertises an array response schema."""
    document = build_openapi(echo_routes(), title="Things", version="1.0.0")

    response_schema = document["paths"]["/things/{thing_id}"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]

    assert_eq(response_schema["type"], "array")


@test()
async def build_openapi_omits_implicit_head_methods() -> None:
    """Starlette adds HEAD for GET routes; the spec should not list it."""
    document = build_openapi(echo_routes(), title="Things", version="1.0.0")

    assert_not_in("head", document["paths"]["/things/{thing_id}"])
