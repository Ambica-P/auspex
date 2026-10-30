"""Load and validate hospital policy, and apply its deterministic parts.

- `map_priority`: source priority → precedence class (first matching entry wins).
- `rank`: position of a job in the hospital's precedence order (0 is highest).

Rule evaluation (the gate) is milestone 8; here rules are only loaded and validated, so a
typo in a rule file fails at start-up instead of silently never matching.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Protocol, Self

import yaml
from pydantic import ConfigDict, Field, ValidationError, model_validator

from auspex.core.errors import PolicyError, UnmappedPriority
from auspex.core.logging import configure_logging, get_logger
from auspex.core.models import (
    ACTION_NAMES,
    ClaimBasis,
    Identifier,
    PrecedenceClass,
    StrictModel,
    Verdict,
)

log = get_logger(__name__)

DEFAULT_POLICY_DIR = Path(__file__).resolve().parents[3] / "policy"


class _PolicyModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class PriorityMappingEntry(_PolicyModel):
    source_priority: Annotated[str, Field(min_length=1)]
    precedence_class: PrecedenceClass = Field(alias="class")
    source: Identifier | None = None
    destination_type: Identifier | None = None

    def matches(self, source: str, source_priority: str, destination_type: str | None) -> bool:
        if self.source_priority.casefold() != source_priority.strip().casefold():
            return False
        if self.source is not None and self.source != source:
            return False
        return self.destination_type is None or self.destination_type == destination_type


class PrecedenceEntry(_PolicyModel):
    precedence_class: PrecedenceClass = Field(alias="class")
    destination_type: Identifier | None = None
    payload: Identifier | None = None

    def matches(
        self, cls: PrecedenceClass, destination_type: str | None, payload: str | None
    ) -> bool:
        if self.precedence_class is not cls:
            return False
        if self.destination_type is not None and self.destination_type != destination_type:
            return False
        return self.payload is None or self.payload == payload


class PlanEffect(StrEnum):
    DELAYS_JOB = "delays_job"
    ORDERS_SAME_CLASS_JOBS = "orders_same_class_jobs"


class RuleScope(_PolicyModel):
    plan_effect: PlanEffect | None = None
    job_class: list[PrecedenceClass] | None = None
    action: list[str] | None = None
    payload: Identifier | None = None
    claim_basis: ClaimBasis | None = None

    @model_validator(mode="after")
    def _known_actions(self) -> Self:
        unknown = sorted(set(self.action or []) - ACTION_NAMES)
        if unknown:
            raise ValueError(f"unknown actions: {unknown}")
        return self


class RuleRequirement(_PolicyModel):
    job_class_at_or_below: PrecedenceClass | None = None
    fleet_certified_for: Identifier | None = None
    max_closed_segments: int | None = Field(default=None, ge=0)
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class Rule(_PolicyModel):
    id: Identifier
    applies_to: RuleScope
    require: RuleRequirement | None = None
    on_fail: Verdict

    @model_validator(mode="after")
    def _not_allow(self) -> Self:
        if self.on_fail is Verdict.ALLOW:
            raise ValueError("on_fail must be REVIEW or BLOCK")
        return self


class Ownership(_PolicyModel):
    """Which Open-RMF tasks Auspex may change (spec 7: only tasks it owns)."""

    # Sources that delegate their robot tasks to Auspex: a task linked to a job from one of
    # these sources may be interrupted, resumed or cancelled. Tasks Auspex submitted itself
    # are always owned.
    delegated_sources: list[Identifier] = Field(default_factory=list)
    requester: Identifier = "auspex"  # the requester name on tasks Auspex submits


class JobLike(Protocol):
    @property
    def precedence(self) -> PrecedenceClass: ...
    @property
    def destination_type(self) -> str | None: ...
    @property
    def payload(self) -> str | None: ...


class Stakes(_PolicyModel):
    """When a stalled or slipping robot job opens an incident (spec 5.2)."""

    incident_classes: list[PrecedenceClass]
    min_slack_s: float = Field(ge=0)


class Policy(_PolicyModel):
    priority_mapping: list[PriorityMappingEntry] = Field(min_length=1)
    precedence: list[PrecedenceEntry] = Field(min_length=1)
    rules: list[Rule] = Field(default_factory=list)
    stakes: Stakes
    # Classes whose route must be kept clear of lower-precedence claims even on shared
    # (non-exclusive) segments (conflict kind required_clear).
    require_clear_classes: list[PrecedenceClass] = Field(default_factory=list)
    ownership: Ownership

    @model_validator(mode="after")
    def _complete(self) -> Self:
        ids = [r.id for r in self.rules]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate rule ids: {dupes}")
        # Every class must have a catch-all precedence entry, so every job has a rank.
        catch_all = {
            e.precedence_class
            for e in self.precedence
            if e.destination_type is None and e.payload is None
        }
        missing = sorted(c.value for c in PrecedenceClass if c not in catch_all)
        if missing:
            raise ValueError(f"precedence has no catch-all entry for classes: {missing}")
        return self

    def map_priority(
        self, source: str, source_priority: str, destination_type: str | None = None
    ) -> PrecedenceClass:
        for entry in self.priority_mapping:
            if entry.matches(source, source_priority, destination_type):
                return entry.precedence_class
        raise UnmappedPriority(
            "no priority mapping for this source priority",
            source=source,
            source_priority=source_priority,
            destination_type=destination_type,
        )

    def rank(self, job: JobLike) -> int:
        """0 is the highest precedence. Lower rank goes first."""
        for i, entry in enumerate(self.precedence):
            if entry.matches(job.precedence, job.destination_type, job.payload):
                return i
        raise PolicyError("job matches no precedence entry", cls=job.precedence.value)

    def class_rank(self, cls: PrecedenceClass) -> int:
        """Rank of a class's catch-all entry (no destination type or payload)."""
        for i, entry in enumerate(self.precedence):
            if (
                entry.precedence_class is cls
                and entry.destination_type is None
                and entry.payload is None
            ):
                return i
        raise PolicyError("class has no catch-all precedence entry", cls=cls.value)

    def order(self, jobs: Sequence[JobLike]) -> list[JobLike]:
        """Stable sort by precedence. Jobs with equal rank keep their input order (a tie)."""
        return sorted(jobs, key=self.rank)

    def rule(self, rule_id: str) -> Rule:
        for r in self.rules:
            if r.id == rule_id:
                return r
        raise PolicyError("unknown rule id", rule_id=rule_id)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError("cannot read policy file", path=str(path), detail=str(exc)) from None
    if not isinstance(data, dict):
        raise PolicyError("policy file must be a mapping", path=str(path))
    return data


def load_policy(policy_dir: Path = DEFAULT_POLICY_DIR) -> Policy:
    mapping = _read_yaml(policy_dir / "priority_mapping.yaml")
    precedence = _read_yaml(policy_dir / "precedence.yaml")
    stakes = _read_yaml(policy_dir / "stakes.yaml")
    ownership = _read_yaml(policy_dir / "ownership.yaml")
    rules: list[Any] = []
    rule_files = sorted((policy_dir / "rules").glob("*.yaml"))
    for path in rule_files:
        rules.extend(_read_yaml(path).get("rules") or [])
    try:
        policy = Policy.model_validate(
            {
                "priority_mapping": mapping.get("priority_mapping"),
                "precedence": precedence.get("precedence"),
                "rules": rules,
                "stakes": stakes.get("stakes"),
                "require_clear_classes": stakes.get("require_clear_classes") or [],
                "ownership": ownership.get("ownership"),
            }
        )
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise PolicyError(f"invalid policy: {problems}", path=str(policy_dir)) from None
    log.info(
        "policy.loaded",
        path=str(policy_dir),
        mappings=len(policy.priority_mapping),
        precedence_entries=len(policy.precedence),
        rules=len(policy.rules),
        rule_files=len(rule_files),
    )
    return policy


def main() -> int:
    configure_logging()
    try:
        policy = load_policy()
    except PolicyError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    order = " > ".join(
        e.precedence_class.value
        + "".join(
            f"[{k}={v}]" for k, v in (("dest", e.destination_type), ("payload", e.payload)) if v
        )
        for e in policy.precedence
    )
    print(f"OK policy valid: {len(policy.rules)} rules")
    print(f"   precedence: {order}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
