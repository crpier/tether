"""Process-wide structured logging configuration and sink ownership."""

from __future__ import annotations

import logging
import sys
from collections.abc import Collection
from pathlib import Path
from typing import cast

import structlog

from tether.logging_processors import shared_logging_processors

type Logger = structlog.stdlib.BoundLogger

QUIET_LOGGERS = ("watchfiles.main", "uvicorn", "uvicorn.error")
"""Development-server namespaces restricted to warning and error output."""
SILENCED_LOGGERS = ("uvicorn.access",)
"""Namespaces disabled because Tether owns equivalent structured events."""


class _NamespaceFilter(logging.Filter):
    """Apply quiet and silenced namespace policy at each owned sink."""

    def __init__(
        self,
        *,
        quiet_loggers: Collection[str],
        silenced_loggers: Collection[str],
    ) -> None:
        super().__init__()
        self.quiet_loggers: tuple[str, ...] = tuple(quiet_loggers)
        self.silenced_loggers: tuple[str, ...] = tuple(silenced_loggers)

    def filter(self, record: logging.LogRecord) -> bool:
        """Reject silenced records and quiet records below warning."""
        if any(
            record.name == namespace or record.name.startswith(f"{namespace}.")
            for namespace in self.silenced_loggers
        ):
            return False
        return record.levelno >= logging.WARNING or not any(
            record.name == namespace or record.name.startswith(f"{namespace}.")
            for namespace in self.quiet_loggers
        )


def _clear_handlers(logger: logging.Logger) -> None:
    """Remove and close handlers before process-wide reconfiguration."""
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def _configure_dependency_loggers(
    *,
    quiet_loggers: Collection[str],
    silenced_loggers: Collection[str],
) -> None:
    """Route quiet namespaces through root and fully disable silenced ones."""
    for logger_name in quiet_loggers:
        logger = logging.getLogger(logger_name)
        _clear_handlers(logger)
        logger.setLevel(logging.WARNING)
        logger.propagate = True
        logger.disabled = False
    for logger_name in silenced_loggers:
        logger = logging.getLogger(logger_name)
        _clear_handlers(logger)
        logger.propagate = False
        logger.disabled = True


def _processor_formatter(
    *,
    shared_processors: list[structlog.types.Processor],
    renderer: structlog.types.Processor,
) -> structlog.stdlib.ProcessorFormatter:
    """Build a formatter that sends stdlib records through the shared pipeline."""
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        pass_foreign_args=True,
        use_get_message=False,
    )


def configure_logging(
    log_level: str = "INFO",
    *,
    force_tty: bool | None = None,
    log_file: str | Path | None = None,
    quiet_loggers: Collection[str] = QUIET_LOGGERS,
    silenced_loggers: Collection[str] = SILENCED_LOGGERS,
) -> Logger:
    """Own structlog, root handlers, optional JSON file output, and namespaces.

    ```python
    logger = configure_logging("DEBUG", force_tty=False)
    logger.info("Server starting")
    ```
    """
    is_tty = sys.stdout.isatty() if force_tty is None else force_tty
    console_renderer: structlog.types.Processor
    if is_tty:
        console_renderer = structlog.dev.ConsoleRenderer(colors=True)
    else:
        console_renderer = structlog.processors.JSONRenderer()
    processors = shared_logging_processors(
        format_exceptions=(not is_tty) or log_file is not None
    )
    structlog.configure(
        processors=[
            *processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    namespace_filter = _NamespaceFilter(
        quiet_loggers=quiet_loggers,
        silenced_loggers=silenced_loggers,
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.addFilter(namespace_filter)
    console_handler.setFormatter(
        _processor_formatter(
            shared_processors=processors,
            renderer=console_renderer,
        )
    )
    handlers: list[logging.Handler] = [console_handler]
    if log_file is not None:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
        file_handler.addFilter(namespace_filter)
        file_handler.setFormatter(
            _processor_formatter(
                shared_processors=processors,
                renderer=structlog.processors.JSONRenderer(),
            )
        )
        handlers.append(file_handler)

    root_logger = logging.getLogger()
    _clear_handlers(root_logger)
    for handler in handlers:
        root_logger.addHandler(handler)
    root_logger.setLevel(log_level)
    _configure_dependency_loggers(
        quiet_loggers=quiet_loggers,
        silenced_loggers=silenced_loggers,
    )
    return cast("Logger", structlog.wrap_logger(logging.getLogger()))
