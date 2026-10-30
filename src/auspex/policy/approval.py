"""Approval router (spec 9) and the only door to the executor.

A plan is stored with the gate's decision. BLOCK: it stops. REVIEW: it waits for a person
(approve or reject; unanswered after APPROVAL_TIMEOUT_S it expires). ALLOW on the automatic
route (from M8): it is authorised at once. Authorising mints an `Authorization` bound to the
plan's exact content (sha256 of its canonical JSON); `Executor.execute_authorized` refuses
anything else. This module is the only caller of `execute_authorized` and the only place an
`Authorization` is made (tests/test_gate_required.py checks both).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, text

from auspex.core.canonical import canonical_json, sha256_hex
from auspex.core.errors import AuspexError
from auspex.core.logging import get_logger
from auspex.core.models import ApprovalRoute, Decision, Plan, Verdict
from auspex.ledger.store import LedgerKind, append
from auspex.policy.gate import GateInputs, evaluate
from auspex.policy.loader import Policy
from auspex.site.model import Site

if TYPE_CHECKING:
    from auspex.execute.executor import Executor

log = get_logger(__name__)

_MINT_KEY = object()


class ApprovalError(AuspexError):
    code = "approval_error"


def plan_sha256(plan: Plan) -> str:
    return sha256_hex(canonical_json(plan))


@dataclass(frozen=True)
class Authorization:
    plan_id: str
    plan_sha256: str
    approved_by: str
    approved_at: datetime
    key: object

    def valid_for(self, plan: Plan) -> bool:
        return (
            self.key is _MINT_KEY
            and self.plan_id == plan.plan_id
            and self.plan_sha256 == plan_sha256(plan)
        )


def _mint(plan: Plan, approved_by: str) -> Authorization:
    return Authorization(
        plan_id=plan.plan_id,
        plan_sha256=plan_sha256(plan),
        approved_by=approved_by,
        approved_at=datetime.now(UTC),
        key=_MINT_KEY,
    )


class Approvals:
    def __init__(
        self,
        *,
        engine: Engine,
        site: Site,
        policy: Policy,
        executor: Executor,
        timeout_s: float,
    ) -> None:
        self.engine = engine
        self.site = site
        self.policy = policy
        self.executor = executor
        self.timeout_s = timeout_s
        # Called with the plan id when a plan expires unapproved or a person rejects it (the
        # decision engine then applies its fallback: the evaluator's best gate-passing option,
        # or the playbook's safe default). The fallback goes through the gate like any plan.
        self.on_expired: Callable[[str], None] | None = None

    # ------------------------------------------------------------------ submit

    def submit(self, plan: Plan, inputs: GateInputs) -> Decision:
        decision = evaluate(plan, self.site, self.policy, inputs)
        if decision.verdict is Verdict.BLOCK:
            status = "blocked"
        elif (
            decision.verdict is Verdict.ALLOW and decision.approval_route is ApprovalRoute.AUTOMATIC
        ):
            status = "executing"
        else:
            status = "awaiting_approval"
        subject = plan.conflict_id or plan.incident_id or plan.event_id
        with self.engine.begin() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM plans WHERE plan_id = :id"), {"id": plan.plan_id}
            ).first()
            if exists is not None:
                raise ApprovalError("plan id already used", plan_id=plan.plan_id)
            conn.execute(
                text(
                    "INSERT INTO plans (plan_id, status, producer, subject, plan, decision)"
                    " VALUES (:id, :status, :producer, :subject, CAST(:plan AS jsonb),"
                    " CAST(:decision AS jsonb))"
                ),
                {
                    "id": plan.plan_id,
                    "status": "awaiting_approval" if status == "executing" else status,
                    "producer": plan.producer.value,
                    "subject": subject,
                    "plan": canonical_json(plan).decode(),
                    "decision": canonical_json(decision).decode(),
                },
            )
            append(conn, LedgerKind.PLAN, {"plan": plan, "sha256": plan_sha256(plan)})
            append(conn, LedgerKind.GATE_DECISION, {"decision": decision})
        log.info("plan.submitted", plan=plan.plan_id, status=status, producer=plan.producer.value)
        if status == "executing":
            self._authorize(plan, "policy:automatic")
        return decision

    # ------------------------------------------------------------------ approve / reject

    def approve(self, plan_id: str, approver: str) -> None:
        plan, status, created = self._load(plan_id)
        if status != "awaiting_approval":
            raise ApprovalError("plan is not awaiting approval", plan_id=plan_id, status=status)
        if (datetime.now(UTC) - created).total_seconds() > self.timeout_s:
            self._expire(plan_id)
            raise ApprovalError("approval window has passed", plan_id=plan_id)
        self._authorize(plan, f"person:{approver}")

    def reject(self, plan_id: str, approver: str, reason: str) -> None:
        _, status, _ = self._load(plan_id)
        if status != "awaiting_approval":
            raise ApprovalError("plan is not awaiting approval", plan_id=plan_id, status=status)
        record = {"plan_id": plan_id, "approved": False, "by": approver, "reason": reason[:300]}
        with self.engine.begin() as conn:
            self._set(conn, plan_id, "rejected", approval=record)
            append(conn, LedgerKind.APPROVAL, record)
        log.info("plan.rejected", plan=plan_id, by=approver)
        if self.on_expired is not None:
            self.on_expired(plan_id)

    def tick(self) -> None:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT plan_id FROM plans WHERE status = 'awaiting_approval'"
                    " AND created_at < now() - make_interval(secs => :s)"
                ),
                {"s": self.timeout_s},
            ).all()
        for r in rows:
            self._expire(str(r.plan_id))

    # ------------------------------------------------------------------ internals

    def _authorize(self, plan: Plan, approver: str) -> None:
        auth = _mint(plan, approver)
        record = {
            "plan_id": plan.plan_id,
            "approved": True,
            "by": approver,
            "plan_sha256": auth.plan_sha256,
            "at": auth.approved_at,
        }
        with self.engine.begin() as conn:
            self._set(conn, plan.plan_id, "executing", approval=record)
            append(conn, LedgerKind.APPROVAL, record)
        log.info("plan.authorized", plan=plan.plan_id, by=approver)
        self.executor.execute_authorized(plan, auth)

    def _expire(self, plan_id: str) -> None:
        record = {"plan_id": plan_id, "approved": False, "by": "timeout", "reason": "expired"}
        with self.engine.begin() as conn:
            self._set(conn, plan_id, "expired", approval=record)
            append(conn, LedgerKind.APPROVAL, record)
        log.warning("plan.expired", plan=plan_id, timeout_s=self.timeout_s)
        if self.on_expired is not None:
            self.on_expired(plan_id)

    def _load(self, plan_id: str) -> tuple[Plan, str, datetime]:
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT plan, status, created_at FROM plans WHERE plan_id = :id"),
                {"id": plan_id},
            ).first()
        if row is None:
            raise ApprovalError("unknown plan", plan_id=plan_id)
        return Plan.model_validate(dict(row.plan)), str(row.status), row.created_at

    @staticmethod
    def _set(conn: Any, plan_id: str, status: str, approval: dict[str, Any]) -> None:
        conn.execute(
            text(
                "UPDATE plans SET status = :status, approval = CAST(:approval AS jsonb),"
                " updated_at = now() WHERE plan_id = :id"
            ),
            {"id": plan_id, "status": status, "approval": json.dumps(approval, default=str)},
        )
