"""Benchmark table (spec 15): A/B/C results on the edge-case scenario set, with run counts.

python -m bench.report            # reads bench/results/<config>/*.json, writes table.md
                                  # and table.json (the replay site shows the same table)

Measured values only. Configuration C is labelled by the model that actually answered
(from the reasoning trace): runs on the deterministic stand-in ("fake-deterministic") are
shown as "C (stand-in model)" and are never reported as Nemotron.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "bench" / "results"
SCENARIOS = REPO / "sim" / "scenarios"


def _s(clock: str | None) -> int | None:
    if not clock:
        return None
    parts = [int(p) for p in str(clock).split(":")]
    while len(parts) < 3:
        parts.append(0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _median(xs: list[float]) -> str:
    return f"{statistics.median(xs):.0f}" if xs else "n/a"


def label(run: dict[str, Any]) -> str:
    config = str(run["config"])
    if config != "C":
        return config
    obs = run["observations"]
    models = sorted(
        {m for p in obs.get("core_plans", []) for m in (p.get("llm") or {}).get("models", [])}
        | {m for i in obs.get("core_interpretations", []) for m in i.get("models", [])}
    )
    provider = (obs.get("core_providers") or {}).get("llm_provider")
    if "fake-deterministic" in models or provider == "fake":
        return "C (stand-in model)"
    if models:
        return f"C ({', '.join(models)})"
    return f"C ({provider or 'model not recorded'})"


def metrics(run: dict[str, Any], truth: dict[str, Any]) -> dict[str, Any]:
    obs = run["observations"]
    tasks = list(obs.get("robot_tasks", {}).values())
    on_time = any(t.get("on_time") for t in tasks)
    margins = [
        _s(t["due"]) - _s(t["delivered"])  # type: ignore[operator]
        for t in tasks
        if t.get("delivered") and t.get("due")
    ]
    porter = [p.get("delay_s") or 0 for p in obs.get("porters", {}).values()]
    waits = [w["wait_s"] for t in tasks for w in t.get("lift_waits", [])]
    conflict = truth.get("conflict") or {}
    detected = any(
        c["segment_urn"] == conflict.get("segment")
        and all(any(u.endswith(j) for u in c["job_urns"]) for j in conflict.get("jobs", []))
        for c in obs.get("core_conflicts", [])
    )
    plans = [p for p in obs.get("core_plans", []) if p.get("status") == "completed"]
    latency: dict[str, list[float]] = defaultdict(list)
    for p in obs.get("core_plans", []):
        if p.get("decision_latency_ms") is not None:
            latency[p["producer"]].append(float(p["decision_latency_ms"]))
    tokens = sum((p.get("llm") or {}).get("tokens", 0) for p in obs.get("core_plans", [])) + sum(
        i.get("tokens", 0) for i in obs.get("core_interpretations", [])
    )
    return {
        "passed": all(c["ok"] for c in run.get("checks", [])),
        "on_time": on_time,
        "margin_s": min(margins) if margins else None,
        "porter_delay_s": max(porter) if porter else 0,
        "robot_wait_s": max(waits) if waits else 0,
        "detected": detected,
        "applied": len(plans),
        "latency": latency,
        "tokens": tokens,
    }


def main() -> int:
    runs = []
    for path in sorted(RESULTS.glob("*/*.json")):
        try:
            runs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"skip {path.name}: {exc}", file=sys.stderr)
    if not runs:
        print("No results yet: run scripts/run-bench (or scripts/run-scenario) first.")
        return 1
    truths = {
        p.stem: (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("ground_truth", {})
        for p in SCENARIOS.glob("*.yaml")
    }
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        groups[(r["scenario"], label(r))].append(metrics(r, truths.get(r["scenario"], {})))
    order = {"A": 0, "B": 1}
    head = (
        "| Scenario | Config | Runs | All checks pass | O2 on time | Deadline margin s "
        "(median) | Max porter delay s (median) | Max robot lift wait s (median) | "
        "Conflict detected | Plans applied (median) | Decision latency ms (median, by tier) "
        "| LLM tokens per run (median) |"
    )
    lines = [
        "# Auspex benchmark: results on the edge-case scenario set",
        "",
        "Scenarios S1-S3 and variants are deliberately weighted to edge cases where the right",
        "action cannot be computed from structured fields alone (spec 8.1). These are results",
        "on that set, not general performance. A: Open-RMF alone (Auspex observes). B: Auspex",
        "deterministic (playbooks, evaluator). C: Auspex with Nemotron; the model is named.",
        f"Generated {datetime.now().isoformat(timespec='minutes')} from {len(runs)} runs.",
        "",
        head,
        "|" + "---|" * 12,
    ]
    rows: list[list[str]] = []
    for (scenario, cfg), ms in sorted(
        groups.items(), key=lambda kv: (kv[0][0], order.get(kv[0][1], 2), kv[0][1])
    ):
        n = len(ms)
        tiers: dict[str, list[float]] = defaultdict(list)
        for m in ms:
            for tier, xs in m["latency"].items():
                tiers[tier] += xs
        tier_txt = ", ".join(f"{t} {_median(v)}" for t, v in sorted(tiers.items())) or "n/a"
        margins = [m["margin_s"] for m in ms if m["margin_s"] is not None]
        tokens = [m["tokens"] for m in ms if m["tokens"]]
        row = [
            scenario,
            cfg,
            str(n),
            f"{sum(m['passed'] for m in ms)}/{n}",
            f"{sum(m['on_time'] for m in ms)}/{n}",
            _median(margins),
            _median([m["porter_delay_s"] for m in ms]),
            _median([m["robot_wait_s"] for m in ms]),
            f"{sum(m['detected'] for m in ms)}/{n}",
            _median([m["applied"] for m in ms]),
            tier_txt,
            _median(tokens),
        ]
        rows.append(row)
        lines.append("| " + " | ".join(row) + " |")
    note = (
        "Deadline margin: seconds the oxygen arrived before its deadline (negative: late; "
        "n/a: not delivered in the run). Porter delay: request to lift granted. Decision "
        "latency: trigger to plan submitted (fast_lane budget 2000 ms; agent within "
        "AGENT_BUDGET_S). Checks: each scenario's ground truth for that configuration."
    )
    (RESULTS / "table.json").write_text(
        json.dumps(
            {
                "title": "Benchmark on the edge-case scenario suite",
                "generated": datetime.now().isoformat(timespec="minutes"),
                "runs": len(runs),
                "intro": " ".join(lines[2:7]),
                "headers": [c.strip() for c in head.strip("|").split("|")],
                "rows": rows,
                "note": note,
            },
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    lines += [
        "",
        "Deadline margin: seconds the oxygen arrived before its deadline (negative: late; "
        "n/a: not delivered in the run). Porter delay: request to lift granted. Decision "
        "latency: trigger to plan submitted (fast_lane budget 2000 ms; agent within "
        "AGENT_BUDGET_S). Checks: each scenario's ground truth for that configuration.",
    ]
    out = RESULTS / "table.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
