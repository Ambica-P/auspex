"""Deterministic stand-in for Nemotron (LLM_PROVIDER=fake): development and tests, no paid calls.

It answers the same tasks with the same JSON schemas using simple rules, so every path
(validation, repair, fail-closed, gate, approval) runs without a model. It is NOT Nemotron:
benchmark configuration C is reported only from Token Factory runs, and the model id
"fake-deterministic" is recorded on every call so results can never be confused.
"""

from __future__ import annotations

import json
import re
from typing import Any

from auspex.core.ports import LLMRequest, LLMResponse
from auspex.evidence.extract import BulletinExtractor

MODEL_ID = "fake-deterministic"


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


class FakeLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.script: list[str] = []  # tests: canned raw answers, consumed first

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        user = request.messages[-1].content
        if self.script:
            content = self.script.pop(0)
        else:
            env = json.loads(next(m.content for m in request.messages if m.role == "user"))
            content = json.dumps(getattr(self, f"_{env['task']}")(env))
        return LLMResponse(
            model_id=MODEL_ID,
            content=content,
            prompt_tokens=len(user) // 4,
            completion_tokens=len(content) // 4,
            latency_ms=1,
        )

    # ------------------------------------------------------------------ tasks

    def _interpret(self, env: dict[str, Any]) -> dict[str, Any]:
        text = _norm(env["untrusted_data"].get("destination_text") or "")
        notes = (env["untrusted_data"].get("notes") or "").lower()
        best: tuple[float, str | None] = (0.0, None)
        for z in env["context"]["zones"]:
            names = [_norm(a) for a in [z["name"], *z["aliases"]]]
            if text and text in names:
                best = max(best, (0.9, z["urn"]))
            elif text and any(text in n or n in text for n in names if len(n) > 2):
                best = max(best, (0.65, z["urn"]))
        constraints = []
        if "large car" in notes or "bariatric" in notes:
            constraints.append("needs a large, bariatric-capable lift car")
        if "vent" in notes:
            constraints.append("on a portable ventilator")
        return {
            "destination_zone": best[1],
            "confidence": best[0],
            "route_constraints": constraints,
            "rationale": "Destination text matched a zone alias." if best[1] else "No zone fits.",
        }

    def _extract(self, env: dict[str, Any]) -> dict[str, Any]:
        page, code = env["untrusted_data"]["page"], env["context"]["fault_code"]
        adv = BulletinExtractor().extract(page, code)
        return {"advisory": adv.model_dump() if adv else None}

    def _outage(self, env: dict[str, Any]) -> dict[str, Any]:
        for e in env["context"]["evidence"]:
            x = e.get("extracted")
            if x:
                recovery = x.get("expected_recovery_s")
                return {
                    "expected_outage_s": recovery,
                    "confidence": 0.8,
                    "needs_technician": x.get("requires_manual_reset"),
                    "evidence_refs": [e["ref"]],
                    "rationale": f"Vendor bulletin for {x['fault_code']}: {x['meaning'][:150]}",
                }
        return {
            "expected_outage_s": None,
            "confidence": 0.2,
            "needs_technician": None,
            "evidence_refs": [],
            "rationale": "No vendor evidence for this fault code.",
        }

    def _choose(self, env: dict[str, Any]) -> dict[str, Any]:
        ctx = env["context"]
        tech = bool((ctx.get("outage") or {}).get("needs_technician"))
        options = [o for o in ctx["options"] if not (tech and o["template"] == "do_nothing")]
        pick = options[0] if options else ctx["options"][0]
        refs = (ctx.get("outage") or {}).get("evidence_refs", [])
        return {
            "option_id": pick["option_id"],
            "rationale": f"{pick['template']} gives the best evaluated outcome "
            f"(score {pick['score']}); the outage estimate is "
            f"{(ctx.get('outage') or {}).get('expected_outage_s')} s.",
            "evidence_refs": refs,
        }

    def _tradeoff(self, env: dict[str, Any]) -> dict[str, Any]:
        ctx = env["context"]
        zones = sorted(
            ctx.get("supply_zones", []), key=lambda z: (z["lateness_s"], z["handover_eta"])
        )
        order = ctx.get("policy_order", [])
        desk = sorted(ctx.get("recipients", []), key=lambda r: not r.startswith("staff/"))
        notify = (
            [{"recipient_urn": desk[0], "message": "Suggest lift_1 order: " + ", ".join(order)}]
            if desk and order
            else []
        )
        proposals = []
        if zones:
            proposals.append(
                {
                    "option_id": "handover-and-sequence",
                    "handover_from_zone": zones[0]["urn"],
                    "people_sequence": order,
                    "notify": notify,
                    "rationale": "Another robot delivers from a store that needs no lift.",
                }
            )
        proposals.append(
            {
                "option_id": "sequence-only",
                "handover_from_zone": None,
                "people_sequence": order,
                "notify": notify,
                "rationale": "Keep the robot held; suggest the policy order to staff.",
            }
        )
        return {
            "proposals": proposals,
            "chosen": proposals[0]["option_id"],
            "rationale": "Policy puts the ED transport first; the Imaging notes indicate slack. "
            "Ordering two STAT patients needs a person's approval.",
        }

    def _report(self, env: dict[str, Any]) -> dict[str, Any]:
        ev = env["context"]["evidence"]
        causes = [
            {"description": f"Possible: {e['what'][:200]}", "evidence_refs": [e["ref"]]}
            for e in ev[:5]
        ]
        return {
            "summary": f"Robot {env['context']['robot']} stalled; "
            f"{len(causes)} possible causes found in the ledger.",
            "causes": causes,
        }
