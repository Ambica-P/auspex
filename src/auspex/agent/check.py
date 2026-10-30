"""M10 check (scripts/check-llm): the configured LLM answers both agent roles, typed.

python -m auspex.agent.check                  # LLM_PROVIDER from .env (fake by default)

Two small calls (a few hundred tokens each): Nemotron Nano interprets S1's "CT 2" against the
site's zones; Nemotron Super estimates an outage from a typed E-42 advisory. Prints the model
id, latency, tokens and whether JSON-schema structured output was accepted (else JSON mode).
With LLM_PROVIDER=tokenfactory this is a paid call: run it once to confirm NEBIUS_API_BASE,
the key and the model ids, not in a loop.
"""

from __future__ import annotations

import asyncio
import sys

from auspex.agent.agent import Agent, AgentFailed, Trace
from auspex.agent.llm import LLMUnavailable, OpenAICompatLLM, make_llm
from auspex.core.config import get_settings
from auspex.core.errors import ConfigError
from auspex.site.model import DEFAULT_SITE_DIR, build_site
from auspex.site.navgraph import load_captures


async def run() -> int:
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"FAIL configuration: {exc}")
        return 1
    llm = make_llm(settings)
    agent = Agent(llm, debug=settings.llm_debug)
    site = build_site(DEFAULT_SITE_DIR, load_captures(DEFAULT_SITE_DIR / "nav_graphs"))
    zones = [
        {"urn": z.urn, "name": z.name, "aliases": z.aliases, "level": z.level}
        for z in site.zones.values()
    ]
    print(f"INFO provider {settings.llm_provider.value}")
    ok = True
    trace = Trace()
    try:
        interp = await agent.interpret(
            job={"urn": "job/check", "kind": "human_transport", "bed_type": "bariatric"},
            zones=zones,
            destination_text="CT 2",
            notes="on portable vent, needs large car",
            trace=trace,
        )
        good = interp.destination_zone == "zone/imaging-L1"
        ok &= good
        print(
            f"{'PASS' if good else 'FAIL'} Nano interprets 'CT 2' -> {interp.destination_zone} "
            f"(confidence {interp.confidence}; constraints {interp.route_constraints})"
        )
        ref = "0" * 64
        est = await agent.outage(
            context={
                "lift": "lift_1",
                "mode": "OFFLINE",
                "fault_code": "E-42",
                "evidence": [
                    {
                        "ref": ref,
                        "url": "https://vendor-site.local/ascendia-lifts/bulletins/al-sb-2026-042.html",
                        "extracted": {
                            "fault_code": "E-42",
                            "meaning": "Drive inverter overcurrent trip; locked out.",
                            "expected_recovery_s": 5400,
                            "requires_manual_reset": True,
                            "affected_versions": ["AL-300"],
                        },
                    }
                ],
                "history": [],
            },
            evidence_refs={ref},
            trace=trace,
        )
        good = est.needs_technician is True and (est.expected_outage_s or 0) >= 1800
        ok &= good
        print(
            f"{'PASS' if good else 'FAIL'} Super estimates the E-42 outage: "
            f"{est.expected_outage_s} s, technician {est.needs_technician} ({est.rationale})"
        )
    except (AgentFailed, LLMUnavailable) as exc:
        print(f"FAIL {exc}")
        ok = False
    for st in trace.steps:
        if st["kind"] == "llm":
            print(
                f"INFO {st['task']}: model {st['model_id']}, {st['latency_ms']} ms, "
                f"{st['prompt_tokens']}+{st['completion_tokens']} tokens, {st['outcome']}"
            )
    if isinstance(llm, OpenAICompatLLM):
        mode = "json_schema" if llm.json_schema_ok else "json_object (schema not accepted)"
        print(f"INFO structured output: {mode}")
    print(f"{'PASS' if ok else 'FAIL'} check-llm")
    return 0 if ok else 1


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
