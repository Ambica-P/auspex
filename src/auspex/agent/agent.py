"""The Nemotron agent (spec 6.3): asks, validates, repairs once, fails closed. Never acts.

Every call sends a fixed system prompt and one JSON user message {task, context,
untrusted_data}; untrusted text has control characters stripped and is labelled as data.
The answer must parse as the task's schema AND pass the task's checks against what Auspex
offered (zones, options, evidence hashes, supply zones, people). One repair attempt with the
error; then AgentFailed, and the caller falls back to the evaluator or the playbook.

Observability (spec 6.3): each call records model id, prompt and response sha256, latency,
tokens and outcome in the trace (the trace goes to the ledger with the decision). Full text
is logged only with LLM_DEBUG=true.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import ValidationError

from auspex.agent import prompts
from auspex.agent.schemas import (
    AdvisoryExtraction,
    IncidentReportDraft,
    Interpretation,
    OptionChoice,
    OutageEstimate,
    TradeOff,
)
from auspex.core.canonical import canonical_json, sha256_hex
from auspex.core.errors import AuspexError
from auspex.core.logging import get_logger
from auspex.core.models import StrictModel, VendorAdvisory, strip_control
from auspex.core.ports import ChatMessage, LLMPort, LLMRequest, ModelRole
from auspex.evidence.extract import sanitize, validated

log = get_logger(__name__)

M = TypeVar("M", bound=StrictModel)
UNTRUSTED_MAX = 8_000


class AgentFailed(AuspexError):
    code = "agent_failed"


@dataclass
class Trace:
    """The reasoning trace shown to people: tool calls with results, and model calls."""

    steps: list[dict[str, Any]] = field(default_factory=list)

    def tool(self, name: str, args: dict[str, Any], result: Any) -> None:
        self.steps.append(
            {
                "kind": "tool",
                "tool": name,
                "args": args,
                "result": json.loads(canonical_json(result)),
            }
        )

    def llm(self, record: dict[str, Any]) -> None:
        self.steps.append({"kind": "llm", **record})

    def models(self) -> list[str]:
        return sorted({str(s["model_id"]) for s in self.steps if s["kind"] == "llm"})


def parse_json(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


class Agent:
    def __init__(self, llm: LLMPort, *, debug: bool = False, call_timeout_s: float = 25.0) -> None:
        self.llm = llm
        self.debug = debug
        self.call_timeout_s = call_timeout_s

    async def ask(
        self,
        *,
        task: str,
        role: ModelRole,
        system: str,
        context: dict[str, Any],
        untrusted: dict[str, str],
        model: type[M],
        check: Callable[[M], None],
        trace: Trace,
        max_tokens: int = 4000,
    ) -> M:
        envelope = {
            "task": task,
            "context": json.loads(canonical_json(context)),
            "untrusted_data": {k: strip_control(v)[:UNTRUSTED_MAX] for k, v in untrusted.items()},
        }
        messages = [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=json.dumps(envelope, ensure_ascii=False)),
        ]
        schema = model.model_json_schema()
        error = ""
        for attempt in (1, 2):
            req = LLMRequest(
                role=role,
                messages=messages,
                json_schema=schema,
                max_tokens=max_tokens,
                temperature=0.0,
                timeout_s=self.call_timeout_s,
            )
            resp = await self.llm.complete(req)
            record = {
                "task": task,
                "role": role.value,
                "attempt": attempt,
                "model_id": resp.model_id,
                "prompt_sha256": sha256_hex("".join(m.content for m in messages).encode()),
                "response_sha256": sha256_hex(resp.content.encode()),
                "latency_ms": resp.latency_ms,
                "prompt_tokens": resp.prompt_tokens,
                "completion_tokens": resp.completion_tokens,
            }
            if self.debug:
                log.debug("llm.text", task=task, prompt=messages[-1].content, answer=resp.content)
            try:
                answer = model.model_validate(parse_json(resp.content))
                check(answer)
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                error = str(exc).splitlines()[0][:400]
                trace.llm({**record, "outcome": "invalid", "error": error})
                log.warning("agent.invalid_answer", task=task, attempt=attempt, error=error)
                messages = [
                    *messages,
                    ChatMessage(role="assistant", content=resp.content[:4000] or "(empty)"),
                    ChatMessage(
                        role="user",
                        content=f"That answer was invalid: {error}. Reply with only one JSON "
                        "object matching the schema (no reasoning, no other text), using only "
                        "identifiers from the context.",
                    ),
                ]
                continue
            trace.llm({**record, "outcome": "ok" if attempt == 1 else "repaired"})
            log.info(
                "agent.answer",
                task=task,
                model=resp.model_id,
                latency_ms=resp.latency_ms,
                usage=resp.prompt_tokens + resp.completion_tokens,
                attempt=attempt,
            )
            return answer
        raise AgentFailed("no valid answer after one repair", task=task, error=error)

    # ------------------------------------------------------------------ tasks

    async def interpret(
        self,
        *,
        job: dict[str, Any],
        zones: list[dict[str, Any]],
        destination_text: str,
        notes: str,
        trace: Trace,
    ) -> Interpretation:
        known = {z["urn"] for z in zones}

        def check(a: Interpretation) -> None:
            if a.destination_zone is not None and a.destination_zone not in known:
                raise ValueError(f"destination_zone {a.destination_zone} is not one of the zones")

        return await self.ask(
            task="interpret",
            role=ModelRole.TRIAGE,
            system=prompts.INTERPRET,
            context={"job": job, "zones": zones},
            untrusted={"destination_text": destination_text, "notes": notes},
            model=Interpretation,
            check=check,
            trace=trace,
        )

    async def extract(self, *, page: str, fault_code: str, trace: Trace) -> VendorAdvisory | None:
        def check(a: AdvisoryExtraction) -> None:
            if a.advisory is not None and validated(a.advisory.model_dump(), fault_code) is None:
                raise ValueError(f"advisory is not a valid bulletin for {fault_code}")

        answer = await self.ask(
            task="extract",
            role=ModelRole.TRIAGE,
            system=prompts.EXTRACT,
            context={"fault_code": fault_code},
            untrusted={"page": sanitize(page)},
            model=AdvisoryExtraction,
            check=check,
            trace=trace,
        )
        return answer.advisory

    async def outage(
        self, *, context: dict[str, Any], evidence_refs: set[str], trace: Trace
    ) -> OutageEstimate:
        def check(a: OutageEstimate) -> None:
            bad = set(a.evidence_refs) - evidence_refs
            if bad:
                raise ValueError(f"evidence_refs not offered: {sorted(bad)}")

        return await self.ask(
            task="outage",
            role=ModelRole.REASON,
            system=prompts.OUTAGE,
            context=context,
            untrusted={},
            model=OutageEstimate,
            check=check,
            trace=trace,
        )

    async def choose(
        self,
        *,
        context: dict[str, Any],
        option_ids: set[str],
        evidence_refs: set[str],
        trace: Trace,
    ) -> OptionChoice:
        def check(a: OptionChoice) -> None:
            if a.option_id not in option_ids:
                raise ValueError(f"option_id {a.option_id} is not one of {sorted(option_ids)}")
            bad = set(a.evidence_refs) - evidence_refs
            if bad:
                raise ValueError(f"evidence_refs not offered: {sorted(bad)}")

        return await self.ask(
            task="choose",
            role=ModelRole.REASON,
            system=prompts.CHOOSE,
            context=context,
            untrusted={},
            model=OptionChoice,
            check=check,
            trace=trace,
        )

    async def tradeoff(
        self,
        *,
        context: dict[str, Any],
        untrusted: dict[str, str],
        supply_zones: set[str],
        people: set[str],
        recipients: set[str],
        policy_order: list[str],
        trace: Trace,
    ) -> TradeOff:
        def check(a: TradeOff) -> None:
            ids = [p.option_id for p in a.proposals]
            if len(set(ids)) != len(ids) or a.chosen not in ids:
                raise ValueError("option ids must be unique and 'chosen' one of them")
            for p in a.proposals:
                if p.handover_from_zone is not None and p.handover_from_zone not in supply_zones:
                    raise ValueError(f"{p.handover_from_zone} is not an offered supply zone")
                if not set(p.people_sequence) <= people:
                    raise ValueError("people_sequence must use the offered transport jobs")
                seq = [u for u in policy_order if u in p.people_sequence]
                if p.people_sequence and p.people_sequence != seq:
                    raise ValueError("people_sequence must not reverse the policy order")
                for n in p.notify:
                    if n.recipient_urn not in recipients:
                        raise ValueError(f"{n.recipient_urn} is not an offered recipient")

        return await self.ask(
            task="tradeoff",
            role=ModelRole.REASON,
            system=prompts.TRADEOFF,
            context=context,
            untrusted=untrusted,
            model=TradeOff,
            check=check,
            trace=trace,
            max_tokens=4000,
        )

    async def report(
        self, *, context: dict[str, Any], refs: set[str], trace: Trace
    ) -> IncidentReportDraft:
        def check(a: IncidentReportDraft) -> None:
            for c in a.causes:
                bad = set(c.evidence_refs) - refs
                if bad:
                    raise ValueError(f"evidence_refs not offered: {sorted(bad)}")

        return await self.ask(
            task="report",
            role=ModelRole.TRIAGE,
            system=prompts.REPORT,
            context=context,
            untrusted={},
            model=IncidentReportDraft,
            check=check,
            trace=trace,
        )
