"""`python -m hospital_mock replay <scenario.yaml> [--speed N]` (scripts/run-mock)."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from auspex.core.config import get_settings
from auspex.core.errors import AuspexError
from auspex.core.logging import configure_logging
from hospital_mock.replay import load_scenario, replay


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m hospital_mock")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay")
    r.add_argument("scenario", type=Path)
    r.add_argument("--speed", type=float, default=1.0, help="scenario seconds per real second")
    r.add_argument(
        "--t0", type=float, default=None, help="wall time (epoch s) of the scenario clock start"
    )
    args = p.parse_args()
    try:
        settings = get_settings()
        configure_logging(settings.log_level, settings.log_json)
        scenario = load_scenario(args.scenario)
        t0 = datetime.fromtimestamp(args.t0, UTC) if args.t0 is not None else None
        return replay(
            scenario, settings.core_url, settings.admin_token.get_secret_value(), args.speed, t0
        )
    except AuspexError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
