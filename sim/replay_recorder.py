"""Replay recorder (spec 14.5): what the ops console saw during one scenario run, as one JSON
file the static replay site plays back. Standard library only (runs inside the scenario
controller, under system Python with ROS sourced).

It polls the core's console API on the run's clock:
- map (robots, closed lanes, lift modes) every 0.5 s; the nav graphs once;
- timeline, conflicts, plans, incidents every 1 s, kept only when they change;
- a plan's full decision context (jobs as received and interpreted, options and scores,
  reasoning trace, evidence) whenever the plan's status changes, and once at the end;
- at the end, the ledger entries written during the run, with their canonical bodies and
  hashes, so the browser can recompute the chain itself.

Nothing secret is recorded: the console API never returns keys, and the recorder refuses to
run when the core logs full prompts (LLM_DEBUG). The site builder scans the result again.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAP_EVERY_S = 0.5
STATE_EVERY_S = 1.0
TAIL_S = 20.0  # keep recording after the scenario ends: verification, incident report
FORMAT = 1

Core = Callable[..., Any]


def _now() -> datetime:
    return datetime.now(UTC)


def _ms(t: datetime, t0: datetime) -> int:
    return int((t - t0).total_seconds() * 1000)


def _key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class RecorderRefused(RuntimeError):
    pass


class Recorder:
    def __init__(self, core: Core, out: Path, meta: dict[str, Any]) -> None:
        self.core = core
        self.out = out
        self.meta = meta
        self.t0 = _now()
        self.stop_at: float | None = None
        self.robots: list[list[Any]] = []
        self.map_state: list[dict[str, Any]] = []
        self.streams: dict[str, list[dict[str, Any]]] = {
            "timeline": [],
            "conflicts": [],
            "plans": [],
            "incidents": [],
        }
        self.contexts: list[dict[str, Any]] = []
        self._last: dict[str, str] = {}
        self._plan_versions: dict[str, str] = {}
        self._thread: threading.Thread | None = None
        self.errors = 0
        health = core("GET", "/api/v1/health")
        if health.get("llm_debug"):
            raise RecorderRefused("LLM_DEBUG is on: replays must hold prompt hashes only")
        self.health = health
        latest = core("GET", "/api/v1/ledger?limit=1")
        self.after_seq = int(latest[0]["seq"]) if latest else 0
        m = core("GET", "/api/v1/map")
        self.graph = {"fleets": {}, "segments": m["segments"]}
        for name, f in m["fleets"].items():
            self.graph["fleets"][name] = {"vertices": f["vertices"], "lanes": f["lanes"]}

    # ------------------------------------------------------------------ lifecycle

    def start(self, t0: datetime) -> None:
        """t0: the scenario clock's start (the run's zero on the replay's time axis)."""
        self.t0 = t0
        self._thread = threading.Thread(target=self._loop, name="replay-recorder", daemon=True)
        self._thread.start()

    def finish(self, checks: list[dict[str, Any]], ended: datetime) -> Path:
        self.stop_at = time.monotonic() + TAIL_S
        if self._thread is not None:
            self._thread.join(timeout=TAIL_S + 30)
        for plan_id in list(self._plan_versions):
            self._context(plan_id)
        ledger = self._ledger()
        data = {
            "format": FORMAT,
            "meta": {
                **self.meta,
                "t0": self.t0.isoformat(),
                "ended": ended.isoformat(),
                "duration_ms": max(_ms(_now(), self.t0), 1),
                "recorded_at": _now().isoformat(timespec="seconds"),
                "llm_provider": self.health.get("llm_provider"),
                "search_provider": self.health.get("search_provider"),
                "models": self.health.get("models"),
                "checks": checks,
                "poll_errors": self.errors,
            },
            "graph": self.graph,
            "robots": self.robots,
            "map_state": self.map_state,
            **self.streams,
            "contexts": self.contexts,
            "ledger": ledger,
        }
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.out.write_text(json.dumps(data, separators=(",", ":"), default=str), "utf-8")
        return self.out

    # ------------------------------------------------------------------ polling

    def _loop(self) -> None:
        last_state = 0.0
        while self.stop_at is None or time.monotonic() < self.stop_at:
            tick = time.monotonic()
            try:
                self._map()
                if tick - last_state >= STATE_EVERY_S:
                    self._state()
                    last_state = tick
            except Exception as exc:  # keep recording; the run must not fail on a poll
                self.errors += 1
                if self.errors <= 5:
                    print(f"  replay: poll failed: {exc}", flush=True)
            time.sleep(max(0.05, MAP_EVERY_S - (time.monotonic() - tick)))

    def _map(self) -> None:
        m = self.core("GET", "/api/v1/map")
        t = _ms(_now(), self.t0)
        self.robots.append(
            [
                t,
                [
                    [r["fleet"], r["name"], r["level"], r["x"], r["y"], r["task_id"]]
                    for r in m["robots"]
                ],
            ]
        )
        state = {
            "closed": {name: f["closed"] for name, f in m["fleets"].items()},
            "lifts": m["lifts"],
        }
        self._keep("map_state", state, t, self.map_state)

    def _state(self) -> None:
        t = _ms(_now(), self.t0)
        since = self.t0.isoformat()
        tl = self.core("GET", "/api/v1/timeline")
        tl.pop("robot_routes", None)
        self._keep("timeline", tl, t, self.streams["timeline"], ignore=("at", "real_time_factor"))
        self._keep("conflicts", self.core("GET", "/api/v1/conflicts"), t, self.streams["conflicts"])
        plans = [p for p in self.core("GET", "/api/v1/plans?limit=40") if p["created_at"] >= since]
        self._keep("plans", plans, t, self.streams["plans"])
        inc = self.core("GET", "/api/v1/incidents?limit=20")
        inc["recent"] = [i for i in inc["recent"] if str(i.get("detected_at", "")) >= since]
        self._keep("incidents", inc, t, self.streams["incidents"])
        for p in plans:
            version = f"{p['status']}|{p['updated_at']}"
            if self._plan_versions.get(p["plan_id"]) != version:
                self._plan_versions[p["plan_id"]] = version
                self._context(p["plan_id"])

    def _keep(
        self,
        name: str,
        value: Any,
        t: int,
        stream: list[dict[str, Any]],
        ignore: tuple[str, ...] = (),
    ) -> None:
        compare = {k: v for k, v in value.items() if k not in ignore} if ignore else value
        k = _key(compare)
        if self._last.get(name) == k:
            return
        self._last[name] = k
        stream.append({"t": t, "v": value})

    def _context(self, plan_id: str) -> None:
        try:
            ctx = self.core("GET", f"/api/v1/plans/{plan_id}/context")
        except Exception as exc:
            self.errors += 1
            print(f"  replay: context of {plan_id} failed: {exc}", flush=True)
            return
        self.contexts.append({"t": _ms(_now(), self.t0), "plan_id": plan_id, "v": ctx})

    def _ledger(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        after = self.after_seq
        while True:
            page = self.core("GET", f"/api/v1/ledger/export?after_seq={after}&limit=1000")
            out += page
            if len(page) < 1000:
                return out
            after = int(page[-1]["seq"])
