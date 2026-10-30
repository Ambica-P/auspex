"""Run the core: `python -m auspex.api` (scripts/run-core). Serves on CORE_HOST:CORE_PORT."""

from __future__ import annotations

import sys

import uvicorn

from auspex.core.config import get_settings
from auspex.core.errors import ConfigError
from auspex.core.logging import configure_logging


def main() -> int:
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    configure_logging(settings.log_level, settings.log_json)
    uvicorn.run(
        "auspex.api.app:app",
        host=settings.core_host,
        port=settings.core_port,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
