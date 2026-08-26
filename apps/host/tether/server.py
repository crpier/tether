"""FastAPI server for the headless Tether capability host."""

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request

from tether.auth import CaptureAuthMiddleware, OpenWebUIToolAuthMiddleware
from tether.health_connect import router as health_connect_router
from tether.host_composition import HOST_QUIET_LOGGERS, app_lifespan
from tether.host_config import AppConfig, HostSettings
from tether.logging_config import configure_logging
from tether.open_webui import open_webui_tool_router
from tether.request_logging import ContextLoggerMiddleware
from tether.telemetry_middleware import TelemetryMiddleware
from tether.telemetry_model import TelemetrySettings

_validation_logger = structlog.stdlib.get_logger("tether.server")


class HostConfigurationError(Exception):
    """Raised when host credentials cannot enforce boundary isolation."""


async def _health() -> dict[str, str]:
    """Report that host composition completed."""
    return {"status": "ok"}


async def log_request_validation(request: Request, exc: Exception) -> JSONResponse:
    """Log validation field paths without logging sensitive input values."""
    if not isinstance(exc, RequestValidationError):
        raise exc
    _validation_logger.warning(
        "Request validation failed",
        method=request.method,
        path=request.url.path,
        errors=[
            {
                "loc": ".".join(str(part) for part in error["loc"]),
                "type": error["type"],
            }
            for error in exc.errors()
        ],
    )
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(exc.errors())},
    )


def create_app(
    *, config: AppConfig, telemetry_settings: TelemetrySettings | None = None
) -> FastAPI:
    """Construct the retained HTTP surface and deterministic lifespan."""
    if not config.api_token or not config.open_webui_token:
        message = "capture and Open WebUI bearer tokens must be configured"
        raise HostConfigurationError(message)
    if config.api_token == config.open_webui_token:
        message = "capture and Open WebUI bearer tokens must be distinct"
        raise HostConfigurationError(message)
    app = FastAPI(
        docs_url=None,
        openapi_url=None,
        redoc_url=None,
        title="Tether",
        version="0.1.0",
        lifespan=app_lifespan(
            config=config,
            telemetry_settings=telemetry_settings or TelemetrySettings(),
        ),
    )
    app.add_exception_handler(RequestValidationError, log_request_validation)
    app.add_api_route("/health", _health, methods=["GET"], include_in_schema=False)
    app.include_router(health_connect_router)
    app.include_router(open_webui_tool_router())
    app.add_middleware(ContextLoggerMiddleware)
    app.add_middleware(TelemetryMiddleware)
    app.add_middleware(CaptureAuthMiddleware, token=config.api_token)
    app.add_middleware(OpenWebUIToolAuthMiddleware, token=config.open_webui_token)
    return app


def _app_config_from_settings(settings: HostSettings) -> AppConfig:
    """Map flat environment settings to in-process host configuration."""
    return AppConfig(
        api_token=settings.api_token,
        database_path=settings.database_path,
        health_episode_sweep_seconds=settings.health_episode_sweep_seconds,
        log_file=settings.log_file,
        logging_level=settings.logging_level,
        open_webui_token=settings.open_webui_token,
        telemetry_database_path=settings.resolved_telemetry_database_path,
    )


def create_app_from_environment() -> FastAPI:
    """Create the ASGI application from `TETHER_` environment variables."""
    settings = HostSettings()
    return create_app(
        config=_app_config_from_settings(settings),
        telemetry_settings=settings.telemetry,
    )


def serve(settings: HostSettings | None = None) -> None:
    """Run the host with uvicorn."""
    configured = HostSettings() if settings is None else settings
    _ = configure_logging(
        configured.logging_level,
        log_file=configured.log_file,
        quiet_loggers=HOST_QUIET_LOGGERS,
    )
    uvicorn.run(
        "tether.server:create_app_from_environment",
        factory=True,
        host=configured.host,
        port=configured.port,
        reload=configured.reload,
        log_config=None,
        access_log=False,
    )


def main() -> None:
    """Console entrypoint for `python -m tether`."""
    serve()


__all__ = [
    "AppConfig",
    "HostConfigurationError",
    "HostSettings",
    "create_app",
    "create_app_from_environment",
    "main",
    "serve",
]
