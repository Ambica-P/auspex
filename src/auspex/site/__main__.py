"""Check the site model against captured nav graphs: `python -m auspex.site [--graphs DIR]`.

Prints every segment with its lanes per fleet, every zone with its waypoints per fleet, and
the routes for S1's job (bariatric bed, Ward 2B to "CT 2"); exits 1 on any problem.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from auspex.core.errors import SiteError
from auspex.core.logging import configure_logging
from auspex.site.model import DEFAULT_SITE_DIR, build_site
from auspex.site.navgraph import load_captures


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m auspex.site")
    p.add_argument("--site", type=Path, default=DEFAULT_SITE_DIR)
    p.add_argument("--graphs", type=Path, default=DEFAULT_SITE_DIR / "nav_graphs")
    args = p.parse_args()
    configure_logging()
    try:
        graphs = load_captures(args.graphs)
        for g in graphs.values():
            print(
                f"graph {g.fleet}: {len(g.vertices)} vertices, {len(g.lanes)} lanes "
                f"(source {g.source}, captured {g.captured_at})"
            )
        site = build_site(args.site, graphs)
    except SiteError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1

    print("\nsegments (lanes per fleet):")
    for seg in sorted(site.segments.values(), key=lambda s: (s.kind, s.urn)):
        lanes = "  ".join(f"{f}={idx}" for f, idx in sorted(seg.rmf_lanes.items()))
        extra = ""
        if seg.attributes is not None:
            a = seg.attributes
            extra = f"  [bed={a.bed_capable} bariatric={a.bariatric_capable} car={a.car_size}]"
            moves = "  ".join(f"{f}={idx}" for f, idx in sorted(seg.internal_lanes.items()))
            extra += f"  car-move lanes (never closed): {moves}"
        excl = "exclusive" if seg.exclusive else "shared"
        print(f"  {seg.urn:<34} {seg.kind:<8} {seg.level or '-':<3} {excl:<9} {lanes}{extra}")

    print("\nzones (named waypoints per fleet):")
    for z in site.zones.values():
        wps = "  ".join(f"{f}={w}" for f, w in sorted(z.waypoints.items()))
        print(f"  {z.urn:<24} {z.level}  {z.name!r}  {wps}")

    print("\nalias checks:")
    for text in ("CT 2", "ct2", "Radiology Level 1", "A&E", "Ward 2B", "bed 12", "CT 3"):
        print(f"  {text!r:<22} -> {site.resolve_alias(text)}")

    print("\nS1 job 4471 (bed, bariatric) zone/ward-2b -> zone/imaging-L1:")
    choices = site.candidate_routes("zone/ward-2b", "zone/imaging-L1", "bed", "bariatric")
    for c in choices:
        state = "usable" if c.usable else "EXCLUDED: " + "; ".join(c.reasons)
        print(f"  {c.route.id:<36} {c.route.total_s:>5.0f} s  {state}")

    ok = True
    usable = [c.route.id for c in choices if c.usable]
    if usable != ["ward-2b_to_imaging_via_lift_25"]:
        print(f"FAIL S1 expects only the lift_25 route to be usable, got {usable}")
        ok = False
    if site.resolve_alias("CT 2") != "zone/imaging-L1":
        print("FAIL 'CT 2' does not resolve to zone/imaging-L1")
        ok = False
    for fleet in site.fleets:
        for lift in ("segment/lift_1", "segment/lift_25"):
            if not site.segment(lift).rmf_lanes.get(fleet):
                print(f"FAIL {lift} has no lanes for {fleet}")
                ok = False
    print("\nPASS site model valid" if ok else "\nFAIL site model checks failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
