"""The picture loop: every tick, run the progress monitor, forecast claims, detect conflicts.

Inputs: active jobs (Postgres) and the latest Open-RMF state (RosState). Outputs: the
resource timeline (memory, served by the API); conflicts and incidents (Postgres + ledger
when they open or end); forecast-vs-actual records (ledger). Nothing here acts on the fleet;
deciding happens in the decision engine (on_change), acting in the executor.

Conflicts are debounced: a conflict opens after OPEN_TICKS consecutive ticks and clears after
CLEAR_TICKS ticks without it, so small ETA changes do not make it flicker.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, text

from auspex.core.canonical import canonical_json
from auspex.core.logging import get_logger
from auspex.core.models import Claim, Conflict, Incident, Job
from auspex.forecast.eta import EtaModel, RobotPath
from auspex.forecast.forecaster import detect_conflicts, forecast, in_horizon
from auspex.ledger.store import LedgerKind, append
from auspex.monitor.progress import MonitorEvent, ProgressMonitor
from auspex.picture.ros_state import LIFT_UNAVAILABLE, RosState
from auspex.policy.loader import Policy
from auspex.site.model import Site

log = get_logger(__name__)

OPEN_TICKS = 2
CLEAR_TICKS = 3


@dataclass
class Picture:
    at: datetime | None = None
    claims: list[Claim] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)  # open (debounced), live windows
    paths: dict[str, RobotPath] = field(default_factory=dict)
    incidents: list[tuple[Incident, dict[str, Any]]] = field(default_factory=list)
    rtf: dict[str, float] = field(default_factory=dict)
    tick_ms: float = 0.0


def load_active_jobs(conn: Connection) -> list[Job]:
    rows = conn.execute(
        text("SELECT data FROM jobs WHERE status NOT IN ('completed', 'cancelled')")
    ).all()
    return [Job.model_validate(dict(r.data)["job"]) for r in rows]


def _json(obj: Any) -> str:
    return canonical_json(obj).decode("utf-8")


def _window(c: Conflict) -> str:
    return (
        f"{c.window.start.isoformat(timespec='seconds')}"
        f"..{c.window.end.isoformat(timespec='seconds')}"
    )


class PictureLoop:
    def __init__(
        self,
        *,
        engine: Engine,
        site: Site,
        policy: Policy,
        ros: RosState,
        eta: EtaModel,
        monitor: ProgressMonitor,
        horizon_s: float,
        tick_s: float,
    ) -> None:
        self.engine = engine
        self.site = site
        self.policy = policy
        self.ros = ros
        self.eta = eta
        self.monitor = monitor
        self.horizon_s = horizon_s
        self.tick_s = tick_s
        self.picture = Picture()
        self._open: dict[str, Conflict] = {}  # recorded as open
        self._seen: dict[str, int] = {}  # consecutive ticks present (not yet open)
        self._missing: dict[str, int] = {}  # consecutive ticks absent (still open)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="picture-loop", daemon=True)
        # Called after each tick with the conflicts and incidents that opened (decision engine).
        self.on_change: Callable[[list[Conflict], list[Incident], list[Incident]], None] | None = (
            None
        )

    def start(self) -> None:
        self._thread.start()
        log.info("picture.started", tick_s=self.tick_s, horizon_s=self.horizon_s)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick(datetime.now(UTC))
            except Exception:  # keep the loop alive; the traceback is in the log
                log.exception("picture.tick_failed")
            self._stop.wait(self.tick_s)

    def snapshot(self) -> Picture:
        with self._lock:
            return self.picture

    def tick(self, now: datetime) -> Picture:
        t0 = datetime.now(UTC)
        with self.engine.connect() as conn:
            jobs = load_active_jobs(conn)
        state = self.ros.state()
        events = self.monitor.tick(
            now=now,
            fleets=state.fleets,
            lifts=state.lifts,
            lift_requests=state.lift_requests,
            closed=state.closed,
            jobs_by_task={j.rmf_task_id: j for j in jobs if j.rmf_task_id},
            rtf=state.rtf,
        )
        claims, paths = forecast(
            site=self.site,
            eta=self.eta,
            jobs=jobs,
            fleets=state.fleets,
            closed=state.closed,
            rtf=state.rtf,
            visited=self.monitor.visited(),
            now=now,
            unavailable=frozenset(
                f"segment/{name}"
                for name, lift in state.lifts.items()
                if int(lift.get("current_mode", 2)) in LIFT_UNAVAILABLE
            ),
        )
        claims = in_horizon(claims, now, self.horizon_s)
        detected = detect_conflicts(
            claims=claims,
            jobs={j.urn: j for j in jobs},
            site=self.site,
            policy=self.policy,
            now=now,
        )
        calibration, self.monitor.calibration = self.monitor.calibration, []
        opened, cleared, live = self._debounce(detected)
        if opened or cleared or events or calibration:
            self._record(now, opened, cleared, events, calibration)
        picture = Picture(
            at=now,
            claims=claims,
            conflicts=live,
            paths=paths,
            incidents=self.monitor.open_incidents(),
            rtf=dict(state.rtf),
            tick_ms=round((datetime.now(UTC) - t0).total_seconds() * 1000, 1),
        )
        with self._lock:
            self.picture = picture
        new_incidents = [e.incident for e in events if e.kind == "opened"]
        resolved = [e.incident for e in events if e.kind == "resolved"]
        if self.on_change is not None and (opened or new_incidents or resolved):
            self.on_change(opened, new_incidents, resolved)
        return picture

    def _debounce(
        self, detected: list[Conflict]
    ) -> tuple[list[Conflict], list[Conflict], list[Conflict]]:
        current = {c.conflict_id: c for c in detected}
        opened: list[Conflict] = []
        cleared: list[Conflict] = []
        for cid, c in current.items():
            self._missing.pop(cid, None)
            if cid in self._open:
                # keep the first detection time; refresh the live window
                self._open[cid] = c.model_copy(update={"detected_at": self._open[cid].detected_at})
                continue
            self._seen[cid] = self._seen.get(cid, 0) + 1
            if self._seen[cid] >= OPEN_TICKS:
                self._open[cid] = c
                self._seen.pop(cid)
                opened.append(c)
        for cid in [k for k in self._seen if k not in current]:
            self._seen.pop(cid)
        for cid in [k for k in self._open if k not in current]:
            self._missing[cid] = self._missing.get(cid, 0) + 1
            if self._missing[cid] >= CLEAR_TICKS:
                cleared.append(self._open.pop(cid))
                self._missing.pop(cid)
        return opened, cleared, sorted(self._open.values(), key=lambda c: c.window.start)

    # ------------------------------------------------------------------ persistence

    def _record(
        self,
        now: datetime,
        opened: list[Conflict],
        cleared: list[Conflict],
        events: list[MonitorEvent],
        calibration: list[dict[str, Any]],
    ) -> None:
        with self.engine.begin() as conn:
            for c in opened:
                conn.execute(
                    text(
                        "INSERT INTO conflicts (conflict_id, segment_urn, status, detected_at,"
                        " data) VALUES (:id, :seg, 'open', :at, CAST(:data AS jsonb))"
                        " ON CONFLICT (conflict_id) DO UPDATE SET status = 'open',"
                        " detected_at = EXCLUDED.detected_at, data = EXCLUDED.data,"
                        " updated_at = now()"
                    ),
                    {
                        "id": c.conflict_id,
                        "seg": c.segment_urn,
                        "at": c.detected_at,
                        "data": _json(c),
                    },
                )
                append(conn, LedgerKind.CONFLICT, {"event": "detected", "conflict": c})
                log.warning(
                    "conflict.detected",
                    conflict=c.conflict_id,
                    kind=c.kind.value,
                    segment=c.segment_urn,
                    jobs=c.job_urns,
                    window=_window(c),
                )
            for c in cleared:
                conn.execute(
                    text(
                        "UPDATE conflicts SET status = 'cleared', updated_at = now()"
                        " WHERE conflict_id = :id"
                    ),
                    {"id": c.conflict_id},
                )
                append(
                    conn,
                    LedgerKind.CONFLICT,
                    {"event": "cleared", "conflict_id": c.conflict_id, "at": now},
                )
                log.info("conflict.cleared", conflict=c.conflict_id, segment=c.segment_urn)
            for e in events:
                self._record_incident(conn, e.incident, e.kind)
            for record in calibration:
                append(conn, LedgerKind.FORECAST_VS_ACTUAL, record)

    def _record_incident(self, conn: Connection, inc: Incident, kind: str) -> None:
        conn.execute(
            text(
                "INSERT INTO incidents (incident_id, robot_urn, rmf_task_id, job_urn, segment_urn,"
                " trigger, status, detected_at, data) VALUES (:id, :robot, :task, :job, :seg,"
                " :trigger, :status, :at, CAST(:data AS jsonb)) ON CONFLICT (incident_id)"
                " DO UPDATE SET status = EXCLUDED.status, data = EXCLUDED.data, updated_at = now()"
            ),
            {
                "id": inc.incident_id,
                "robot": inc.robot_urn,
                "task": inc.rmf_task_id,
                "job": inc.job_urn,
                "seg": inc.segment_urn,
                "trigger": inc.trigger.value,
                "status": inc.status.value,
                "at": inc.detected_at,
                "data": _json(inc),
            },
        )
        append(conn, LedgerKind.INCIDENT, {"event": kind, "incident": inc})


def incidents_from_db(conn: Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        text("SELECT data FROM incidents ORDER BY detected_at DESC LIMIT :n"), {"n": limit}
    ).all()
    return [dict(r.data) for r in rows]
