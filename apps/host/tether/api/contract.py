"""Typed route contracts and Starlette adapters for JSON REST APIs."""

import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import (
    Annotated,
    Any,
    Concatenate,
    Literal,
    TypeGuard,
    get_args,
    get_origin,
    get_type_hints,
)

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError
from starlette import status
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

RouteMethod = Literal["DELETE", "GET", "PATCH", "POST", "PUT"]
SecurityScheme = Literal["human_session", "tool_secret"] | None
ParamSource = Literal["body", "path", "query"]

Handler = Callable[..., Awaitable[object]]
ContextFactory = Callable[..., Awaitable[object]]

_CONTRACT_ATTRIBUTE = "__tether_route_contract__"
_AUTO_ERROR_STATUSES = frozenset(
    {
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    }
)
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ParamMarker:
    source: ParamSource


type PathParam[_ParamType] = Annotated[_ParamType, _ParamMarker("path")]
type QueryParam[_ParamType] = Annotated[_ParamType, _ParamMarker("query")]
type BodyParam[_ParamType] = Annotated[_ParamType, _ParamMarker("body")]


class ApiContractError(Exception):
    """Raised when route contract declarations are invalid."""


class ApiError(Exception):
    """Expected API failure that route contracts may expose as `ErrorOut`.

    ```python
    raise ApiError(status.HTTP_404_NOT_FOUND, "missing", "Memory not found")
    ```
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code: str = code
        self.details: dict[str, object] = dict(details or {})
        self.message: str = message
        self.status_code: int = status_code


class ApiModel(BaseModel):
    """Base class for DTOs that are part of the JSON API contract.

    ```python
    class MemoryOut(ApiModel):
        content: str
    ```
    """

    model_config = ConfigDict(extra="forbid")


class ErrorOut(ApiModel):
    """Standard JSON error response for route-layer and expected API errors."""

    code: str
    details: dict[str, object]
    message: str


@dataclass(frozen=True, slots=True)
class RouteContract:
    """The method, path, status, and expected errors declared for a handler."""

    errors: tuple[int, ...]
    method: RouteMethod
    path: str
    status_code: int


@dataclass(frozen=True, slots=True)
class _RouteGroup:
    """Router-level config shared by every route declared on one ApiRouter."""

    auth_errors: tuple[int, ...]
    ctx_factory: ContextFactory
    prefix: str
    security: SecurityScheme
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ValidatedParameter:
    adapter: TypeAdapter[Any]
    annotation: object
    default: object
    name: str
    required: bool
    source: ParamSource


@dataclass(frozen=True, slots=True)
class _CompiledRoute:
    body_parameter: _ValidatedParameter | None
    contract: RouteContract
    declared_error_statuses: frozenset[int]
    group: _RouteGroup
    handler: Handler
    operation_id: str
    ctx_factory_accepts_request: bool
    path_parameters: tuple[_ValidatedParameter, ...]
    query_parameters: tuple[_ValidatedParameter, ...]
    response_adapter: TypeAdapter[Any] | None
    response_annotation: object | None


@dataclass(frozen=True, slots=True)
class _OpenApiRoute:
    compiled_route: _CompiledRoute
    full_path: str


def _validate_path_shape(path: str, *, label: str, allow_root: bool) -> None:
    if not path.startswith("/"):
        msg = f"{label} must start with '/'"
        raise ApiContractError(msg)
    if path != "/" and path.endswith("/"):
        msg = f"{label} must not end with '/'"
        raise ApiContractError(msg)
    if not allow_root and path == "/":
        msg = f"{label} must not be '/'"
        raise ApiContractError(msg)


def _join_paths(prefix: str, path: str) -> str:
    if path == "/":
        return prefix
    if prefix == "/":
        return path
    return f"{prefix}{path}"


def _extract_path_names(path: str) -> set[str]:
    path_names: set[str] = set()
    for segment in path.split("/"):
        if not segment.startswith("{") and not segment.endswith("}"):
            continue
        if not (segment.startswith("{") and segment.endswith("}")):
            msg = "path params must occupy a whole path segment"
            raise ApiContractError(msg)
        path_name = segment[1:-1]
        if ":" in path_name:
            msg = "path params must not use Starlette converters"
            raise ApiContractError(msg)
        path_names.add(path_name)
    return path_names


def _validate_api_model_types(annotation: object) -> None:
    if isinstance(annotation, type):
        if issubclass(annotation, BaseModel) and not issubclass(annotation, ApiModel):
            msg = "API body and response models must subclass ApiModel"
            raise ApiContractError(msg)
        if issubclass(annotation, ApiModel):
            for field_info in annotation.model_fields.values():
                _validate_api_model_types(field_info.annotation)
    for argument in get_args(annotation):
        if not isinstance(argument, _ParamMarker):
            _validate_api_model_types(argument)


def _extract_marker(annotation: object) -> tuple[ParamSource, object]:
    origin = get_origin(annotation)
    if origin is PathParam:
        return "path", get_args(annotation)[0]
    if origin is QueryParam:
        return "query", get_args(annotation)[0]
    if origin is BodyParam:
        return "body", get_args(annotation)[0]
    if origin is Annotated:
        inner_type, *metadata = get_args(annotation)
        markers = [item for item in metadata if isinstance(item, _ParamMarker)]
        if len(markers) == 1:
            return markers[0].source, inner_type
    msg = "handler request parameters must use exactly one route param alias"
    raise ApiContractError(msg)


def _sanitize_pydantic_errors(
    source: ParamSource,
    validation_error: ValidationError,
    *,
    name: str | None = None,
) -> list[dict[str, object]]:
    errors: list[dict[str, object]] = []
    for pydantic_error in validation_error.errors(include_input=False):
        loc = pydantic_error.get("loc", ())
        if not isinstance(loc, tuple):
            loc = (loc,)
        errors.append(
            {
                "loc": [source, *([name] if name is not None else []), *loc],
                "msg": pydantic_error.get("msg", "Invalid value"),
                "type": pydantic_error.get("type", "value_error"),
            }
        )
    return errors


def _validation_response(errors: Sequence[Mapping[str, object]]) -> JSONResponse:
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        code="validation_error",
        message="Request validation failed.",
        details={"errors": list(errors)},
    )


def _error_response(
    status_code: int,
    *,
    code: str,
    message: str,
    details: Mapping[str, object] | None = None,
) -> JSONResponse:
    return JSONResponse(
        ErrorOut(
            code=code,
            details=dict(details or {}),
            message=message,
        ).model_dump(mode="json"),
        status_code=status_code,
    )


def _compile_route(handler: Handler, group: _RouteGroup) -> _CompiledRoute:
    contract = route_contract(handler)
    signature = inspect.signature(handler)
    parameters = tuple(signature.parameters.values())
    if not inspect.iscoroutinefunction(handler):
        msg = "route handlers must be async"
        raise ApiContractError(msg)
    if len(parameters) == 0:
        msg = "route handlers must accept a context parameter"
        raise ApiContractError(msg)

    type_hints = get_type_hints(handler, include_extras=True)
    path_parameters: list[_ValidatedParameter] = []
    query_parameters: list[_ValidatedParameter] = []
    body_parameters: list[_ValidatedParameter] = []
    for parameter in parameters[1:]:
        if parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
            msg = "route request parameters must be keyword-only"
            raise ApiContractError(msg)
        source, inner_type = _extract_marker(type_hints[parameter.name])
        if source == "body":
            _validate_api_model_types(inner_type)
        if source == "path" and parameter.default is not inspect.Parameter.empty:
            msg = "path params may not have defaults"
            raise ApiContractError(msg)
        if source == "body" and parameter.default is not inspect.Parameter.empty:
            msg = "body params may not have defaults"
            raise ApiContractError(msg)
        validated_parameter = _ValidatedParameter(
            adapter=TypeAdapter(inner_type),
            annotation=inner_type,
            default=parameter.default,
            name=parameter.name,
            required=parameter.default is inspect.Parameter.empty,
            source=source,
        )
        if source == "path":
            path_parameters.append(validated_parameter)
        elif source == "query":
            query_parameters.append(validated_parameter)
        else:
            body_parameters.append(validated_parameter)

    if len(body_parameters) > 1:
        msg = "routes may declare at most one body parameter"
        raise ApiContractError(msg)

    path_names = _extract_path_names(contract.path)
    declared_path_names = {parameter.name for parameter in path_parameters}
    if path_names != declared_path_names:
        msg = "path template params must match handler PathParam names"
        raise ApiContractError(msg)

    auto_owned_errors = _AUTO_ERROR_STATUSES.intersection(contract.errors)
    if auto_owned_errors:
        msg = "route-declared errors must not include route-layer statuses"
        raise ApiContractError(msg)

    return_annotation = type_hints.get("return")
    response_adapter = None
    if return_annotation not in (None, type(None)):
        _validate_api_model_types(return_annotation)
        response_adapter = TypeAdapter(return_annotation)
    return _CompiledRoute(
        body_parameter=body_parameters[0] if body_parameters else None,
        contract=contract,
        declared_error_statuses=frozenset(contract.errors),
        group=group,
        handler=handler,
        operation_id=handler.__name__,
        ctx_factory_accepts_request=bool(
            inspect.signature(group.ctx_factory).parameters
        ),
        path_parameters=tuple(path_parameters),
        query_parameters=tuple(query_parameters),
        response_adapter=response_adapter,
        response_annotation=None if response_adapter is None else return_annotation,
    )


def _validate_path_and_query(
    request: Request,
    compiled_route: _CompiledRoute,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    validated_params: dict[str, object] = {}
    errors: list[dict[str, object]] = []

    for parameter in compiled_route.path_parameters:
        try:
            validated_params[parameter.name] = parameter.adapter.validate_python(
                request.path_params[parameter.name]
            )
        except ValidationError as e:
            errors.extend(_sanitize_pydantic_errors("path", e, name=parameter.name))

    expected_query_names = {
        parameter.name for parameter in compiled_route.query_parameters
    }
    for query_name in request.query_params:
        if query_name not in expected_query_names:
            errors.append(
                {
                    "loc": ["query", query_name],
                    "msg": "Unknown query parameter.",
                    "type": "unknown_query_parameter",
                }
            )

    for parameter in compiled_route.query_parameters:
        query_values = request.query_params.getlist(parameter.name)
        if len(query_values) > 1:
            errors.append(
                {
                    "loc": ["query", parameter.name],
                    "msg": "Repeated query parameter.",
                    "type": "repeated_query_parameter",
                }
            )
            continue
        if not query_values:
            if parameter.required:
                errors.append(
                    {
                        "loc": ["query", parameter.name],
                        "msg": "Field required.",
                        "type": "missing",
                    }
                )
            else:
                validated_params[parameter.name] = parameter.default
            continue
        try:
            validated_params[parameter.name] = parameter.adapter.validate_python(
                query_values[0]
            )
        except ValidationError as e:
            errors.extend(_sanitize_pydantic_errors("query", e, name=parameter.name))

    return validated_params, errors


async def _validate_body(
    request: Request,
    compiled_route: _CompiledRoute,
) -> tuple[object, list[dict[str, object]] | JSONResponse]:
    body_parameter = compiled_route.body_parameter
    if body_parameter is None:
        return None, []

    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        return None, _error_response(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            code="unsupported_media_type",
            message="Request body must be application/json.",
        )

    try:
        request_json = await request.json()
    except json.JSONDecodeError:
        return None, [
            {
                "loc": ["body"],
                "msg": "Malformed JSON.",
                "type": "json_invalid",
            }
        ]

    try:
        return body_parameter.adapter.validate_python(request_json), []
    except ValidationError as e:
        return None, _sanitize_pydantic_errors("body", e)


async def _call_context_factory(
    request: Request,
    compiled_route: _CompiledRoute,
) -> object:
    if compiled_route.ctx_factory_accepts_request:
        return await compiled_route.group.ctx_factory(request)
    return await compiled_route.group.ctx_factory()


def _handler_error_response(
    e: ApiError, compiled_route: _CompiledRoute
) -> JSONResponse:
    if e.status_code not in compiled_route.declared_error_statuses:
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            message="Internal server error.",
        )
    return _error_response(
        e.status_code,
        code=e.code,
        message=e.message,
        details=e.details,
    )


def _context_error_response(
    e: ApiError, compiled_route: _CompiledRoute
) -> JSONResponse:
    if e.status_code not in compiled_route.group.auth_errors:
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            message="Internal server error.",
        )
    return _error_response(
        e.status_code,
        code=e.code,
        message=e.message,
        details=e.details,
    )


def _starlette_endpoint(
    compiled_route: _CompiledRoute,
) -> Callable[[Request], Awaitable[Response]]:
    async def endpoint(request: Request) -> Response:
        validated_params, errors = _validate_path_and_query(request, compiled_route)
        if errors:
            return _validation_response(errors)

        body_value, body_errors = await _validate_body(request, compiled_route)
        if isinstance(body_errors, JSONResponse):
            return body_errors
        if body_errors:
            return _validation_response(body_errors)
        if compiled_route.body_parameter is not None:
            validated_params[compiled_route.body_parameter.name] = body_value

        try:
            context = await _call_context_factory(request, compiled_route)
        except ApiError as e:
            return _context_error_response(e, compiled_route)
        except Exception:
            LOGGER.exception(
                "Unexpected context factory failure.",
                extra={"operation_id": compiled_route.operation_id},
            )
            return _error_response(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                code="internal_error",
                message="Internal server error.",
            )

        try:
            handler_result = await compiled_route.handler(context, **validated_params)
        except ApiError as e:
            return _handler_error_response(e, compiled_route)
        except Exception:
            LOGGER.exception(
                "Unexpected route handler failure.",
                extra={"operation_id": compiled_route.operation_id},
            )
            return _error_response(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                code="internal_error",
                message="Internal server error.",
            )

        if compiled_route.response_adapter is None:
            return Response(status_code=compiled_route.contract.status_code)
        return JSONResponse(
            compiled_route.response_adapter.dump_python(
                handler_result,
                mode="json",
                warnings=False,
            ),
            status_code=compiled_route.contract.status_code,
        )

    return endpoint


def _swagger_html() -> str:
    return """
<!doctype html>
<html>
  <head><title>Tether API docs</title></head>
  <body>
    <div id="swagger-ui"></div>
    <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist/swagger-ui-bundle.js"></script>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist/swagger-ui.css">
    <script>
      SwaggerUIBundle({url: '/openapi.json', dom_id: '#swagger-ui'});
    </script>
  </body>
</html>
""".strip()


def _rewrite_schema_refs(schema: object, component_names: Mapping[str, str]) -> object:
    if isinstance(schema, dict):
        rewritten: dict[str, object] = {}
        for key, item in schema.items():
            if key == "$ref" and isinstance(item, str):
                rewritten[key] = item
                for original_name, component_name in component_names.items():
                    if item == f"#/components/schemas/{original_name}":
                        rewritten[key] = f"#/components/schemas/{component_name}"
                        break
            else:
                rewritten[key] = _rewrite_schema_refs(item, component_names)
        return rewritten
    if isinstance(schema, list):
        return [_rewrite_schema_refs(item, component_names) for item in schema]
    return schema


def _schema_component_name(name: str, suffix: str, enum_names: set[str]) -> str:
    if name in enum_names:
        return name
    return f"{name}{suffix}"


def _is_api_model_annotation(annotation: object) -> TypeGuard[type[ApiModel]]:
    return isinstance(annotation, type) and issubclass(annotation, ApiModel)


def _collect_enum_names(annotation: object) -> set[str]:
    enum_names: set[str] = set()
    if isinstance(annotation, type):
        if issubclass(annotation, Enum):
            enum_names.add(annotation.__name__)
        if issubclass(annotation, ApiModel):
            for field_info in annotation.model_fields.values():
                enum_names.update(_collect_enum_names(field_info.annotation))
    for argument in get_args(annotation):
        if not isinstance(argument, _ParamMarker):
            enum_names.update(_collect_enum_names(argument))
    return enum_names


def _openapi_schema_for_annotation(
    annotation: object,
    *,
    components: dict[str, object],
    mode: Literal["serialization", "validation"],
) -> dict[str, object]:
    suffix = "Output" if mode == "serialization" else "Input"
    enum_names = _collect_enum_names(annotation)
    schema = TypeAdapter(annotation).json_schema(
        mode=mode,
        ref_template="#/components/schemas/{model}",
    )
    defs = schema.pop("$defs", {})
    if not isinstance(defs, dict):
        msg = "Pydantic $defs must be an object"
        raise ApiContractError(msg)
    component_names = {
        name: _schema_component_name(name, suffix, enum_names) for name in defs
    }
    for name, definition in defs.items():
        components[component_names[name]] = _rewrite_schema_refs(
            definition,
            component_names,
        )
    if _is_api_model_annotation(annotation):
        component_name = _schema_component_name(
            annotation.__name__,
            suffix,
            enum_names,
        )
        components[component_name] = _rewrite_schema_refs(schema, component_names)
        return {"$ref": f"#/components/schemas/{component_name}"}
    rewritten_schema = _rewrite_schema_refs(schema, component_names)
    if not isinstance(rewritten_schema, dict):
        msg = "OpenAPI schema must be an object"
        raise ApiContractError(msg)
    return rewritten_schema


def _response_schema(
    compiled_route: _CompiledRoute,
    components: dict[str, object],
) -> dict[str, object] | None:
    if compiled_route.response_annotation is None:
        return None
    return _openapi_schema_for_annotation(
        compiled_route.response_annotation,
        components=components,
        mode="serialization",
    )


def _request_schema(
    annotation: object,
    components: dict[str, object],
) -> dict[str, object]:
    return _openapi_schema_for_annotation(
        annotation,
        components=components,
        mode="validation",
    )


def _openapi_parameters(
    compiled_route: _CompiledRoute,
    components: dict[str, object],
) -> list[dict[str, object]]:
    parameters: list[dict[str, object]] = []
    for path_parameter in compiled_route.path_parameters:
        parameters.append(
            {
                "in": "path",
                "name": path_parameter.name,
                "required": True,
                "schema": _request_schema(path_parameter.annotation, components),
            }
        )
    for query_parameter in compiled_route.query_parameters:
        parameters.append(
            {
                "in": "query",
                "name": query_parameter.name,
                "required": query_parameter.required,
                "schema": _request_schema(query_parameter.annotation, components),
            }
        )
    return parameters


def _openapi_responses(
    compiled_route: _CompiledRoute,
    components: dict[str, object],
) -> dict[str, object]:
    responses: dict[str, object] = {}
    success_response: dict[str, object] = {"description": "Successful response."}
    success_schema = _response_schema(compiled_route, components)
    if success_schema is not None:
        success_response["content"] = {"application/json": {"schema": success_schema}}
    responses[str(compiled_route.contract.status_code)] = success_response

    error_schema = _openapi_schema_for_annotation(
        ErrorOut,
        components=components,
        mode="serialization",
    )
    for error_status in compiled_route.contract.errors:
        responses[str(error_status)] = {
            "content": {"application/json": {"schema": error_schema}},
            "description": "Expected error.",
        }
    for error_status in compiled_route.group.auth_errors:
        responses.setdefault(
            str(error_status),
            {
                "content": {"application/json": {"schema": error_schema}},
                "description": "Authentication error.",
            },
        )
    if compiled_route.body_parameter is not None:
        responses[str(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)] = {
            "content": {"application/json": {"schema": error_schema}},
            "description": "Unsupported media type.",
        }
    if (
        compiled_route.path_parameters
        or compiled_route.query_parameters
        or compiled_route.body_parameter is not None
    ):
        responses[str(status.HTTP_422_UNPROCESSABLE_CONTENT)] = {
            "content": {"application/json": {"schema": error_schema}},
            "description": "Validation error.",
        }
    responses[str(status.HTTP_500_INTERNAL_SERVER_ERROR)] = {
        "content": {"application/json": {"schema": error_schema}},
        "description": "Internal error.",
    }
    return responses


def _security_requirement(security: SecurityScheme) -> list[dict[str, list[object]]]:
    if security == "human_session":
        return [{"human_session": []}]
    if security == "tool_secret":
        return [{"tool_secret": []}]
    return []


def _build_openapi(
    *,
    title: str,
    version: str,
    openapi_routes: Sequence[_OpenApiRoute],
) -> dict[str, object]:
    components: dict[str, object] = {}
    paths: dict[str, object] = {}
    tag_names: list[str] = []
    seen_tag_names: set[str] = set()
    for openapi_route in openapi_routes:
        compiled_route = openapi_route.compiled_route
        docstring = inspect.getdoc(compiled_route.handler)
        if not docstring:
            msg = "route handlers must have docstrings"
            raise ApiContractError(msg)
        for tag_name in compiled_route.group.tags:
            if tag_name not in seen_tag_names:
                seen_tag_names.add(tag_name)
                tag_names.append(tag_name)
        operation: dict[str, object] = {
            "description": docstring,
            "operationId": compiled_route.operation_id,
            "responses": _openapi_responses(compiled_route, components),
            "summary": docstring.splitlines()[0],
            "tags": list(compiled_route.group.tags),
        }
        parameters = _openapi_parameters(compiled_route, components)
        if parameters:
            operation["parameters"] = parameters
        if compiled_route.body_parameter is not None:
            operation["requestBody"] = {
                "content": {
                    "application/json": {
                        "schema": _request_schema(
                            compiled_route.body_parameter.annotation,
                            components,
                        )
                    }
                },
                "required": True,
            }
        security_requirement = _security_requirement(compiled_route.group.security)
        if security_requirement:
            operation["security"] = security_requirement
        path_item = paths.setdefault(openapi_route.full_path, {})
        if not isinstance(path_item, dict):
            msg = "OpenAPI path item must be an object"
            raise ApiContractError(msg)
        path_item[compiled_route.contract.method.lower()] = operation

    return {
        "components": {
            "schemas": dict(sorted(components.items())),
            "securitySchemes": {
                "human_session": {
                    "in": "cookie",
                    "name": "tether_session",
                    "type": "apiKey",
                },
                "tool_secret": {
                    "scheme": "bearer",
                    "type": "http",
                },
            },
        },
        "info": {"title": title, "version": version},
        "openapi": "3.1.0",
        "paths": paths,
        "tags": [{"name": tag_name} for tag_name in tag_names],
    }


def route_contract(handler: Callable[..., object]) -> RouteContract:
    """Return the route contract attached to a handler."""

    contract = getattr(handler, _CONTRACT_ATTRIBUTE, None)
    if not isinstance(contract, RouteContract):
        msg = "handler has no route contract"
        raise ApiContractError(msg)
    return contract


class ApiRouter[CtxT]:
    """Collects typed JSON handlers under a shared resource prefix.

    Handlers are registered by decorating them with the router instance, which
    attaches immutable route metadata and returns the original callable:

    ```python
    router = ApiRouter(
        prefix="/memories",
        tags=["Memories"],
        security=None,
        ctx_factory=make_ctx,
    )

    @router("GET", "/{memory_id}", status=status.HTTP_200_OK)
    async def fetch_memory(ctx: Ctx, *, memory_id: PathParam[UUID]) -> MemoryOut:
        ...
    ```
    """

    def __init__(
        self,
        *,
        prefix: str,
        tags: Sequence[str],
        security: SecurityScheme,
        ctx_factory: Callable[..., Awaitable[CtxT]],
        auth_errors: Sequence[int] = (),
    ) -> None:
        _validate_path_shape(prefix, label="router prefix", allow_root=True)
        if _extract_path_names(prefix):
            msg = "router prefixes must not contain path params"
            raise ApiContractError(msg)
        if security is None and auth_errors:
            msg = "auth errors require a security scheme"
            raise ApiContractError(msg)
        if not inspect.iscoroutinefunction(ctx_factory):
            msg = "context factories must be async"
            raise ApiContractError(msg)
        self._group = _RouteGroup(
            auth_errors=tuple(auth_errors),
            ctx_factory=ctx_factory,
            prefix=prefix,
            security=security,
            tags=tuple(tags),
        )
        self._handlers: list[Handler] = []

    @property
    def prefix(self) -> str:
        return self._group.prefix

    @property
    def group(self) -> _RouteGroup:
        return self._group

    @property
    def handlers(self) -> tuple[Handler, ...]:
        return tuple(self._handlers)

    def __call__[**P, R](
        self,
        method: RouteMethod,
        path: str,
        *,
        status: int,
        errors: Sequence[int] = (),
    ) -> Callable[
        [Callable[Concatenate[CtxT, P], Awaitable[R]]],
        Callable[Concatenate[CtxT, P], Awaitable[R]],
    ]:
        """Register a handler and attach immutable route metadata to it.

        The handler's first parameter must match this router's context type
        (the return type of ``ctx_factory``); a mismatch is a type error.
        """

        _validate_path_shape(path, label="route path", allow_root=True)

        def decorate(
            handler: Callable[Concatenate[CtxT, P], Awaitable[R]],
        ) -> Callable[Concatenate[CtxT, P], Awaitable[R]]:
            if hasattr(handler, _CONTRACT_ATTRIBUTE):
                msg = "handler already has a route contract"
                raise ApiContractError(msg)
            setattr(
                handler,
                _CONTRACT_ATTRIBUTE,
                RouteContract(
                    errors=tuple(errors),
                    method=method,
                    path=path,
                    status_code=status,
                ),
            )
            self._handlers.append(handler)
            return handler

        return decorate


class ApiMount:
    """Mounts routers under runtime surface prefixes and builds the API.

    ```python
    mount = ApiMount(
        routes={"/api": [memory_router], "/internal": [tool_router]},
    )
    app = Starlette(
        routes=mount.build_routes(title="Tether API", version="0.1.0"),
    )
    ```
    """

    def __init__(self, *, routes: Mapping[str, Sequence[ApiRouter]]) -> None:
        for surface_prefix in routes:
            _validate_path_shape(
                surface_prefix, label="mount prefix", allow_root=False
            )
            if _extract_path_names(surface_prefix):
                msg = "mount prefixes must not contain path params"
                raise ApiContractError(msg)
        self._routes: dict[str, tuple[ApiRouter, ...]] = {
            surface_prefix: tuple(routers)
            for surface_prefix, routers in routes.items()
        }

    def _compile(
        self,
        *,
        title: str,
        version: str,
    ) -> tuple[list[Route], dict[str, object]]:
        routes: list[Route] = []
        openapi_routes: list[_OpenApiRoute] = []
        operation_ids: set[str] = set()
        for surface_prefix, routers in self._routes.items():
            for router in routers:
                if not router.handlers:
                    msg = "routers must declare at least one route"
                    raise ApiContractError(msg)
                for handler in router.handlers:
                    compiled_route = _compile_route(handler, router.group)
                    if compiled_route.operation_id in operation_ids:
                        msg = "operation IDs must be unique"
                        raise ApiContractError(msg)
                    operation_ids.add(compiled_route.operation_id)
                    full_path = _join_paths(
                        surface_prefix,
                        _join_paths(router.prefix, compiled_route.contract.path),
                    )
                    routes.append(
                        Route(
                            full_path,
                            endpoint=_starlette_endpoint(compiled_route),
                            methods=[compiled_route.contract.method],
                            name=compiled_route.operation_id,
                        )
                    )
                    openapi_routes.append(
                        _OpenApiRoute(
                            compiled_route=compiled_route, full_path=full_path
                        )
                    )

        openapi_schema = _build_openapi(
            title=title,
            version=version,
            openapi_routes=openapi_routes,
        )
        return routes, openapi_schema

    def openapi_schema(self, *, title: str, version: str) -> dict[str, object]:
        """Build the combined OpenAPI 3.1 document for every mounted route."""

        _, openapi_schema = self._compile(title=title, version=version)
        return openapi_schema

    def build_routes(self, *, title: str, version: str) -> list[Route]:
        """Build Starlette routes plus the `/openapi.json` and `/docs` routes."""

        routes, openapi_schema = self._compile(title=title, version=version)

        async def openapi(_: Request) -> JSONResponse:
            return JSONResponse(openapi_schema)

        async def docs(_: Request) -> HTMLResponse:
            return HTMLResponse(_swagger_html())

        return [
            *routes,
            Route("/openapi.json", endpoint=openapi, methods=["GET"]),
            Route("/docs", endpoint=docs, methods=["GET"]),
        ]
