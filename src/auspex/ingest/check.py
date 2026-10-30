"""M4 end-to-end check (scripts/check-ingest): core, bridge, consumer, intake, ledger.

python -m auspex.ingest.check            # live picture only
python -m auspex.ingest.check --smoke    # plus the expectations of sim/scenarios/smoke.yaml
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import httpx

from auspex.core.config import get_settings
from auspex.core.errors import ConfigError
from auspex.core.models import Job
from auspex.policy.loader import load_policy

_results: list[bool] = []


def check(ok: bool, msg: str) -> None:
    _results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {msg}")


def info(msg: str) -> None:
    print(f"INFO {msg}")


def _smoke(client: httpx.Client) -> None:
    jobs = {j["job"]["urn"]: j for j in client.get("/api/v1/jobs").json()}
    t = jobs.get("job/transport_system-4471", {}).get("job")
    check(t is not None, "job 4471 stored")
    if t:
        check(t["precedence"] == "stat", f"4471 precedence stat (got {t['precedence']})")
        check(t["origin"] == "zone/ward-2b", f"4471 origin from code L2-W2B (got {t['origin']})")
        check(
            t["destination"] is None and t["destination_text"] == "CT 2",
            "4471 destination left unresolved; free text 'CT 2' kept for interpretation "
            f"(destination={t['destination']}, text={t['destination_text']!r})",
        )
        check(t["bed_type"] == "bariatric" and t["transport_mode"] == "bed", "4471 bed, bariatric")
        check(
            t["status"] == "responding",
            f"4471 status responding; the late older 'Assigned' was ignored (got {t['status']})",
        )
    o = jobs.get("job/delivery_requests-O2-0007", {}).get("job")
    check(o is not None, "oxygen request O2-0007 stored")
    if o:
        check(
            o["precedence"] == "critical" and o["payload"] == "oxygen", "O2-0007 critical, oxygen"
        )
        check(
            o["destination"] == "zone/ward-1a-bed-12", f"O2-0007 to bed 12 (got {o['destination']})"
        )
        check(o["receiver_urn"] == "device/rohan-arm-1", "O2-0007 receiver is Rohan's arm")
    e = jobs.get("job/transport_system-4480", {}).get("job")
    check(e is not None, "ED transport 4480 stored")
    if e and t and o:
        check(e["destination_type"] == "emergency_department", "4480 destination type ED (coded)")
        policy = load_policy()

        def rank(d: dict[str, Any]) -> int:
            return policy.rank(Job.model_validate(d))

        order = sorted([("4471", t), ("O2-0007", o), ("4480", e)], key=lambda x: rank(x[1]))
        names = [n for n, _ in order]
        check(
            names == ["4480", "4471", "O2-0007"], f"precedence order ED stat > stat > O2 ({names})"
        )
    check(
        "job/transport_system-4490" not in jobs and "job/transport_system-4491" not in jobs,
        "records with an unknown code or unmapped priority were not stored",
    )


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m auspex.ingest.check")
    p.add_argument("--smoke", action="store_true")
    args = p.parse_args()
    try:
        s = get_settings()
    except ConfigError as exc:
        print(f"FAIL {exc}")
        return 1
    headers = {"Authorization": f"Bearer {s.admin_token.get_secret_value()}"}
    with httpx.Client(base_url=s.core_url, headers=headers, timeout=10.0) as client:
        try:
            h = client.get("/api/v1/health").json()
        except httpx.HTTPError as exc:
            print(f"FAIL core not reachable at {s.core_url}: {exc} (scripts/run-core running?)")
            return 1
        ros = h["ros"]
        info(f"consumer processed={h['consumer']['processed']} errors={h['consumer']['errors']}")
        info(f"bridge topic counts={ros['bridge_topics']}")
        for fleet, status in sorted(ros["graphs"].items()):
            check(
                status == "match", f"live nav graph for {fleet} matches the site capture ({status})"
            )
        for fleet in ("deliveryRobot", "tinyRobot"):
            robots = ros["fleets"].get(fleet, [])
            check(bool(robots), f"bridge -> core: {fleet} robots {robots}")
        quiet = [t for t in ("/lift_states", "/door_states") if not ros["bridge_topics"].get(t)]
        if quiet:
            info(f"quiet topics (open item, needed for S2/S3): {quiet}")
        else:
            info(f"lifts {ros['lifts']}; {len(ros['doors'])} doors reporting")
        check(
            h["events"]["failed"] == 0,
            f"no failed events (failed={h['events']['failed']}, "
            f"last={h['consumer']['last_error']})",
        )
        check(h["events"]["pending"] < 500, f"queue keeps up (pending={h['events']['pending']})")
        if args.smoke:
            _smoke(client)
        v = client.get("/api/v1/ledger/verify").json()
        check(bool(v.get("ok")), f"ledger chain verifies ({v})")
    ok = all(_results)
    print("PASS ingest checks" if ok else "FAIL ingest checks")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
