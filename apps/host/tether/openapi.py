"""Pydantic request validation and OpenAPI generation for the Tether HTTP API.

A thin layer over Starlette with two jobs. `endpoint` decorates a handler so
its request body or query string is validated with Pydantic before the handler
runs, and records the request/response models on the handler. `build_openapi`
walks the route table and reads those records to assemble an OpenAPI 3.1
document — schemas come straight from `model_json_schema` so they never drift
from the models actually used at runtime.

>>> from pydantic import BaseModel
>>> from starlette.responses import JSONResponse
>>> class CaptureBody(BaseModel):
...     text: str
>>> @endpoint(request_body=CaptureBody, response=CaptureBody, status=201)
... async def capture(request: Request, body: CaptureBody) -> Response:
...     return JSONResponse(body.model_dump(), status_code=201)
>>> capture.spec.status
201
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, overload

from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route, request_response


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """The OpenAPI-relevant facts a handler declares through `endpoint`.

    A handler validates at most one of `query` or `request_body` — query
    strings carry filters, bodies carry payloads, and no Tether route needs
    both — so the injected model is unambiguous.
    """

    query: type[BaseModel] | None
    request_body: type[BaseModel] | None
    response: type[BaseModel] | None
    response_is_list: bool
    status: int


def _unprocessable(error: ValidationError) -> JSONResponse:
    """Render a Pydantic validation failure as HTTP 422 with per-field detail.

    `ValidationError.json` is used rather than `errors()` because the latter
    can carry non-JSON-serialisable context objects.
    """
    return JSONResponse(
        {"detail": json.loads(error.json(include_url=False))},
        status_code=422,
    )


class _Endpoint:
    """A Starlette handler that validates input and carries its OpenAPI spec.

    Wrapping the handler in a callable object, rather than a decorated function,
    lets the spec ride along as a real attribute — the type checker forbids
    setting attributes on a function. Mount it with `EndpointRoute`, which
    adapts the `(request) -> Response` call into an ASGI app; Starlette's own
    `Route` only does that for plain functions, not callable instances.
    """

    def __init__(
        self,
        handler: Callable[..., Awaitable[Response]],
        spec: OperationSpec,
    ) -> None:
        self.handler: Callable[..., Awaitable[Response]] = handler
        self.spec: OperationSpec = spec

    async def __call__(self, request: Request) -> Response:
        if self.spec.request_body is not None:
            try:
                submitted = await request.json()
            except json.JSONDecodeError:
                return JSONResponse(
                    {"detail": "request body is not valid JSON"},
                    status_code=400,
                )
            try:
                body = self.spec.request_body.model_validate(submitted)
            except ValidationError as error:
                return _unprocessable(error)
            return await self.handler(request, body)
        if self.spec.query is not None:
            try:
                query = self.spec.query.model_validate(dict(request.query_params))
            except ValidationError as error:
                return _unprocessable(error)
            return await self.handler(request, query)
        return await self.handler(request)


class EndpointRoute(Route):
    """A `Route` that mounts an `_Endpoint` as a proper ASGI app.

    Starlette wraps plain functions with `request_response` but treats a
    callable instance as a raw ASGI app, calling it `(scope, receive, send)`.
    Wrapping the `_Endpoint` here restores the `(request) -> Response` contract
    while leaving `self.endpoint` pointing at the `_Endpoint`, so the OpenAPI
    builder can still read its spec.
    """

    def __init__(
        self,
        path: str,
        endpoint: _Endpoint,
        *,
        methods: list[str] | None = None,
    ) -> None:
        super().__init__(path, endpoint, methods=methods)
        self.app = request_response(endpoint)


def _component_definition(name: str, definition: dict[str, Any]) -> dict[str, Any]:
    """Keep arbitrary JSON data useful to generated clients.

    Recursive JSON aliases are valid OpenAPI, but not every client generator can
    express them. An unconstrained schema still states the wire contract: any
    JSON value is accepted in these positions.
    """
    if name == "JsonValue":
        return {}
    return definition


def _register(model: type[BaseModel], components: dict[str, Any]) -> str:
    """Add a model and its nested definitions to `components` and name the ref.

    Pydantic emits nested models under `$defs`; those move into the shared
    components map while the model's own schema is registered under its class
    name, so every operation can reference `#/components/schemas/<Name>`.
    """
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    for name, definition in schema.pop("$defs", {}).items():
        components.setdefault(name, _component_definition(name, definition))
    components[model.__name__] = schema
    return model.__name__


def _summary(endpoint: object) -> str:
    """First line of a handler's docstring, used as the operation summary."""
    handler = endpoint.handler if isinstance(endpoint, _Endpoint) else endpoint
    docstring = (getattr(handler, "__doc__", None) or "").strip()
    return docstring.splitlines()[0] if docstring else ""


def _path_parameter_names(path: str) -> list[str]:
    """Extract `{name}` placeholders from a Starlette path template."""
    return re.findall(r"{([^}:]+)", path)


def _to_openapi_path(path: str) -> str:
    """Strip Starlette converter suffixes so `{id:int}` reads as `{id}`."""
    return re.sub(r"{([^}:]+)(?::[^}]+)?}", r"{\1}", path)


def _path_parameter(name: str) -> dict[str, Any]:
    """Describe a required path parameter; Starlette path values are strings."""
    return {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}


def _query_parameters(
    model: type[BaseModel], components: dict[str, Any]
) -> list[dict[str, Any]]:
    """Turn a query model's fields into OpenAPI query parameters.

    Query models are flattened into parameters rather than registered as one
    object schema, but Pydantic can still emit shared `$defs` for field types.
    Those definitions must live under document-level components so parameter
    `$ref`s resolve from anywhere in the OpenAPI document.
    """
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    for name, definition in schema.pop("$defs", {}).items():
        components.setdefault(name, _component_definition(name, definition))
    required = set(schema.get("required", []))
    return [
        {
            "name": name,
            "in": "query",
            "required": name in required,
            "schema": field_schema,
        }
        for name, field_schema in schema.get("properties", {}).items()
    ]


def _request_body(model: type[BaseModel], components: dict[str, Any]) -> dict[str, Any]:
    """Describe a required JSON request body referencing a registered schema."""
    reference = {"$ref": f"#/components/schemas/{_register(model, components)}"}
    return {"required": True, "content": {"application/json": {"schema": reference}}}


def _responses(spec: OperationSpec, components: dict[str, Any]) -> dict[str, Any]:
    """Describe the single declared response, wrapping list returns in an array."""
    status = str(spec.status)
    if spec.response is None:
        return {status: {"description": "OK"}}
    reference: dict[str, Any] = {
        "$ref": f"#/components/schemas/{_register(spec.response, components)}"
    }
    schema = (
        {"type": "array", "items": reference} if spec.response_is_list else reference
    )
    return {
        status: {
            "description": "OK",
            "content": {"application/json": {"schema": schema}},
        }
    }


def _operation(route: Route, components: dict[str, Any]) -> dict[str, Any]:
    """Build the OpenAPI operation object for one route.

    Path parameters are derived from the path template regardless of whether the
    handler opted into validation, so even undecorated routes are described.
    """
    parameters = [_path_parameter(name) for name in _path_parameter_names(route.path)]
    operation: dict[str, Any] = {"summary": _summary(route.endpoint)}
    if isinstance(route.endpoint, _Endpoint):
        spec = route.endpoint.spec
        if spec.query is not None:
            parameters.extend(_query_parameters(spec.query, components))
        if spec.request_body is not None:
            operation["requestBody"] = _request_body(spec.request_body, components)
        operation["responses"] = _responses(spec, components)
    else:
        operation["responses"] = {"200": {"description": "OK"}}
    if parameters:
        operation["parameters"] = parameters
    return operation


@overload
def endpoint[BodyModel: BaseModel](
    *,
    request_body: type[BodyModel],
    response: type[BaseModel] | None = None,
    response_is_list: bool = False,
    status: int = 200,
) -> Callable[[Callable[[Request, BodyModel], Awaitable[Response]]], _Endpoint]: ...


@overload
def endpoint[QueryModel: BaseModel](
    *,
    query: type[QueryModel],
    response: type[BaseModel] | None = None,
    response_is_list: bool = False,
    status: int = 200,
) -> Callable[[Callable[[Request, QueryModel], Awaitable[Response]]], _Endpoint]: ...


@overload
def endpoint(
    *,
    response: type[BaseModel] | None = None,
    response_is_list: bool = False,
    status: int = 200,
) -> Callable[[Callable[[Request], Awaitable[Response]]], _Endpoint]: ...


def endpoint(
    *,
    request_body: type[BaseModel] | None = None,
    query: type[BaseModel] | None = None,
    response: type[BaseModel] | None = None,
    response_is_list: bool = False,
    status: int = 200,
) -> Callable[[Any], _Endpoint]:
    """Validate a handler's input and record its request/response schema.

    Declaring `request_body` or `query` makes the validated model the
    handler's second argument; declaring neither leaves a plain `(request)`
    handler. `response` and `status` feed the generated OpenAPI document.
    """

    def decorator(handler: Any) -> _Endpoint:
        return _Endpoint(
            handler,
            OperationSpec(
                query=query,
                request_body=request_body,
                response=response,
                response_is_list=response_is_list,
                status=status,
            ),
        )

    return decorator


def build_openapi(routes: list[Route], *, title: str, version: str) -> dict[str, Any]:
    """Assemble an OpenAPI 3.1 document for `routes`.

    Routes sharing a path merge into one path item keyed by method. `HEAD` and
    `OPTIONS` (which Starlette adds implicitly) are omitted.

    >>> build_openapi([], title="Tether", version="0.1.0")["openapi"]
    '3.1.0'
    """
    components: dict[str, Any] = {}
    paths: dict[str, Any] = {}
    for route in routes:
        operation = _operation(route, components)
        path_item = paths.setdefault(_to_openapi_path(route.path), {})
        for method in route.methods or set():
            if method in {"HEAD", "OPTIONS"}:
                continue
            path_item[method.lower()] = operation
    document: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": title, "version": version},
        "paths": paths,
    }
    if components:
        document["components"] = {"schemas": components}
    return document


def openapi_routes(api_routes: list[Route], *, title: str, version: str) -> list[Route]:
    """Serve the generated spec at `/openapi.json` and Swagger UI at `/docs`.

    The document is built once from `api_routes` (which excludes these two
    routes, so the spec describes only the API) and reused on each request.
    """
    document = build_openapi(api_routes, title=title, version=version)

    def serve_openapi(_request: Request) -> Response:
        """Return the cached OpenAPI document as JSON."""
        return JSONResponse(document)

    def serve_docs(_request: Request) -> Response:
        """Return the Swagger UI, which fetches `/openapi.json`."""
        return HTMLResponse(_SWAGGER_UI_HTML)

    return [
        Route("/openapi.json", serve_openapi),
        Route("/docs", serve_docs),
    ]


_SWAGGER_UI_HTML = """<!doctype html>
<html>
  <head>
    <title>Tether API</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <link
      rel="stylesheet"
      href="https://cdn.jsdelivr.net/npm/swagger-ui-dist/swagger-ui.css"
    />
  </head>
  <body>
    <div id="swagger-ui"></div>
    <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist/swagger-ui-bundle.js"></script>
    <script>
      window.ui = SwaggerUIBundle({ url: "/openapi.json", dom_id: "#swagger-ui" });
    </script>
  </body>
</html>
"""
"""Swagger UI shell; loads the CSS and JS bundle from a CDN, so it needs network."""
