"""Build the sandbox's S1 snapshot from the recorded S1 run (configuration C) and the scenario
script: python -m sandbox.snapshot [web/public/replays/s1-C.json] -> sandbox/snapshots/s1.json.

The snapshot is the world at 10:28:00 on the scenario clock, when job 4471 goes Responding:
where each robot was and which task it was doing (from the recording), the lifts' modes and
closed lanes, and the source records of Alex's oxygen request and of job 4471 (from the
scenario script). The sandbox recomputes everything else (Robot A's route, every claim, the
conflict, the options, the gate) with the core's own code."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
DEFAULT_REPLAY = REPO / "web" / "public" / "replays" / "s1-C.json"
SCENARIO = REPO / "sim" / "scenarios" / "s1.yaml"
OUT = Path(__file__).resolve().parent / "snapshots" / "s1.json"
AT = "10:28:00"  # job 4471 goes Responding


def clock_s(value: str) -> int:
    parts = [int(p) for p in str(value).split(":")]
    while len(parts) < 3:
        parts.append(0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def build(replay_path: Path = DEFAULT_REPLAY) -> dict[str, Any]:
    replay = json.loads(replay_path.read_text("utf-8"))
    scenario = yaml.safe_load(SCENARIO.read_text("utf-8"))
    meta = replay["meta"]
    if meta["scenario"] != "s1":
        raise SystemExit(f"FAIL {replay_path} is a recording of {meta['scenario']}, not s1")
    t_ms = (clock_s(AT) - clock_s(meta["clock_start"])) * 1000
    frames = [f for f in replay["robots"] if f[0] <= t_ms]
    if not frames:
        raise SystemExit(f"FAIL no robot positions recorded before {AT}")
    _, robots = frames[-1]
    fleets: dict[str, Any] = {}
    for fleet, name, level, x, y, task in robots:
        fleets.setdefault(fleet, {"robots": []})["robots"].append(
            {"name": name, "level": level, "x": x, "y": y, "task_id": task}
        )
    states = [s for s in replay["map_state"] if s["t"] <= t_ms] or replay["map_state"][:1]
    state = states[-1]["v"] if states else {"closed": {}, "lifts": {}}
    records = {e["record"]["system"]: e["record"] for e in scenario["events"] if "record" in e}
    task_spec = scenario["robot_tasks"][0]
    robot = next(
        (
            r
            for r in fleets.get(task_spec["fleet"], {}).get("robots", [])
            if r["name"] == task_spec["robot"]
        ),
        None,
    )
    if robot is None or not robot["task_id"]:
        raise SystemExit(f"FAIL {task_spec['robot']} had no task at {AT} in this recording")
    return {
        "scenario": "s1",
        "date": str(meta["t0"])[:10],
        "clock_start": meta["clock_start"],
        "now": AT,
        "source": {"replay": replay_path.name, "recorded_at": meta.get("recorded_at")},
        "fleets": fleets,
        "closed": state.get("closed", {}),
        "lifts": state.get("lifts", {}),
        "robot_job": {
            "record": records["delivery_requests"],
            "fleet": task_spec["fleet"],
            "robot": task_spec["robot"],
            "task_id": robot["task_id"],
            "status": "Assigned",
            "status_at": task_spec["at"],
            "visited_origin": False,  # at 10:28 Robot A is still on its way to the O2 store
        },
        "transport": {"record": records["transport_system"], "status": "Responding"},
    }


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_REPLAY
    if not path.is_file():
        print(f"FAIL no recording at {path}: record S1 in C first (scripts/record-replays)")
        return 1
    snap = build(path)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snap, indent=1) + "\n", "utf-8")
    robots = [f"{r['name']}@{r['level']}" for f in snap["fleets"].values() for r in f["robots"]]
    print(
        f"PASS snapshot written: {OUT.relative_to(REPO)} (robots {robots}; "
        f"Robot A task {snap['robot_job']['task_id']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
