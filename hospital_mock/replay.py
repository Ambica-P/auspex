"""Replay a scenario script (sim/scenarios/*.yaml) into the core, like a hospital feed would.

Scenario times are wall-clock style "HH:MM" (or "HH:MM:SS") on the scenario's own clock,
which starts at `clock_start`. At replay, scenario time T maps to
    now + (T - clock_start) / speed
both for when an event is sent and for timestamps inside records (needed_by, status_at), so
deadlines keep their meaning at any speed.

Each event is either a full source record (`record`, posted to /api/v1/jobs, as feeds resend
whole records) or a status change (`status`, posted to /api/v1/jobs/<id>/status).
`expect_reject: true` marks deliberately bad data the core must refuse with 422.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import httpx
import yaml
from pydantic import Field, ValidationError, model_validator

from auspex.core.errors import AuspexError
from auspex.core.logging import get_logger
from auspex.core.models import StrictModel

log = get_logger(__name__)

_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
TIME_FIELDS = ("needed_by", "status_at")
HTTP_TIMEOUT_S = 5.0
RETRIES = 3


class ReplayError(AuspexError):
    code = "replay_error"


def clock_seconds(value: str) -> int:
    m = _CLOCK.fullmatch(value.strip())
    if m is None:
        raise ReplayError("scenario time must be HH:MM or HH:MM:SS", value=value)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3) or 0)


class ScenarioEvent(StrictModel):
    at: str
    record: dict[str, Any] | None = None
    status: dict[str, Any] | None = None
    expect_reject: bool = False
    note: Annotated[str, Field(max_length=300)] | None = None

    @model_validator(mode="after")
    def _one(self) -> ScenarioEvent:
        if (self.record is None) == (self.status is None):
            raise ValueError("each event has exactly one of record or status")
        clock_seconds(self.at)
        return self


class Scenario(StrictModel):
    id: str
    description: str
    clock_start: str
    events: list[ScenarioEvent] = Field(min_length=1)
    # Sections for the scenario controller (sim/scenario_controller.py); the mock ignores them.
    ground_truth: dict[str, Any] | None = None
    robot_starts: dict[str, str] | None = None
    robot_tasks: list[dict[str, Any]] | None = None
    porters: list[dict[str, Any]] | None = None
    faults: list[dict[str, Any]] | None = None
    end_at: str | None = None


def load_scenario(path: Path) -> Scenario:
    try:
        return Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as exc:
        raise ReplayError("cannot read scenario", path=str(path), detail=str(exc)) from None
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ReplayError(
            "invalid scenario",
            path=str(path),
            detail=f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}",
        ) from None


class Clock:
    def __init__(self, clock_start: str, speed: float, start: datetime | None = None) -> None:
        self.origin = clock_seconds(clock_start)
        self.speed = speed
        self.start = start or datetime.now(UTC)

    def wall(self, scenario_time: str) -> datetime:
        delta = (clock_seconds(scenario_time) - self.origin) / self.speed
        return self.start + timedelta(seconds=delta)

    def convert(self, body: dict[str, Any]) -> dict[str, Any]:
        out = dict(body)
        for key in TIME_FIELDS:
            value = out.get(key)
            if isinstance(value, str) and _CLOCK.fullmatch(value.strip()):
                out[key] = self.wall(value).isoformat()
        return out


def _post(client: httpx.Client, path: str, body: dict[str, Any]) -> httpx.Response:
    for attempt in range(1, RETRIES + 1):
        try:
            return client.post(path, json=body)
        except httpx.TransportError as exc:
            if attempt == RETRIES:
                raise ReplayError("core unreachable", path=path, detail=str(exc)) from None
            time.sleep(1.0 * attempt)
    raise AssertionError("unreachable")


def replay(
    scenario: Scenario, core_url: str, token: str, speed: float, t0: datetime | None = None
) -> int:
    clock = Clock(scenario.clock_start, speed, t0)
    print(
        f"replay {scenario.id}: {len(scenario.events)} events, speed x{speed:g}, "
        f"scenario {scenario.clock_start} = {clock.start.isoformat(timespec='seconds')}"
    )
    failures = 0
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=core_url, headers=headers, timeout=HTTP_TIMEOUT_S) as client:
        for event in sorted(scenario.events, key=lambda e: clock_seconds(e.at)):
            wait = (clock.wall(event.at) - datetime.now(UTC)).total_seconds()
            if wait > 0:
                time.sleep(wait)
            if event.record is not None:
                body = clock.convert(event.record)
                path = "/api/v1/jobs"
                label = f"record {body.get('system')}/{body.get('request_id')} {body.get('status')}"
            else:
                assert event.status is not None
                body = clock.convert(event.status)
                job_id = f"{body.pop('system')}-{body.pop('request_id')}"
                path = f"/api/v1/jobs/{job_id}/status"
                label = f"status {job_id} -> {body.get('status')}"
            resp = _post(client, path, body)
            ok = resp.status_code == (422 if event.expect_reject else 202)
            failures += 0 if ok else 1
            is_json = resp.headers.get("content-type", "").startswith("application/json")
            detail = resp.json() if is_json else resp.text[:200]
            expected = " (expected reject)" if event.expect_reject else ""
            print(
                f"[{event.at}] {label}: HTTP {resp.status_code}{expected} "
                f"{'OK' if ok else 'UNEXPECTED'} {detail}"
            )
            log.info("replay.sent", at=event.at, path=path, http=resp.status_code, ok=ok)
    verdict = "PASS" if failures == 0 else "FAIL"
    print(f"{verdict} replay {scenario.id}: {failures} unexpected responses")
    return 0 if failures == 0 else 1
