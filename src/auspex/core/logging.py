"""structlog configuration shared by every process. Secrets are redacted before rendering."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_SECRET_MARKERS = ("password", "secret", "token", "api_key", "apikey", "authorization")
_REDACTED = "***"


def _redact(value: Any, key: str = "") -> Any:
    if any(m in key.lower() for m in _SECRET_MARKERS):
        return _REDACTED
    if isinstance(value, MutableMapping):
        return {k: _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, str) and "://" in value and "@" in value:
        # Connection strings: drop the userinfo part.
        scheme, rest = value.split("://", 1)
        return f"{scheme}://{_REDACTED}@{rest.split('@', 1)[1]}"
    return value


def redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict.keys()):
        if key == "event":
            continue
        event_dict[key] = _redact(event_dict[key], key)
    return event_dict


def configure_logging(level: str = "INFO", json: bool = False) -> None:
    """Configure structlog once per process. JSON output for files, console for terminals."""
    lvl = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(name)
    return logger
