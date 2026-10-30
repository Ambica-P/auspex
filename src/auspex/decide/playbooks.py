"""Fast-lane playbooks (playbooks/fast_lane.yaml): validated, closed vocabulary, no code."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, ValidationError

from auspex.core.errors import PolicyError
from auspex.core.models import Identifier, PrecedenceClass, StrictModel

DEFAULT_PLAYBOOKS = Path(__file__).resolve().parents[3] / "playbooks" / "fast_lane.yaml"


class Playbook(StrictModel):
    id: Identifier
    trigger: Literal["lift_unavailable", "conflict", "incident"]
    human_classes: list[PrecedenceClass] = Field(default_factory=list)
    close_lanes_for: Literal["every_fleet", "robot_fleet"] | None = None
    pad_s: float = Field(default=0.0, ge=0.0, le=600.0)
    replan: Literal["robots_routed_through", "robot"] | None = None
    if_no_route: Literal["hold"] | None = None
    notify: Literal["requesters_of_affected_jobs", "requester"] | None = None
    reopen_when: Literal["lift_available"] | None = None


class Playbooks(StrictModel):
    playbooks: list[Playbook]

    def for_trigger(self, trigger: str) -> list[Playbook]:
        return [p for p in self.playbooks if p.trigger == trigger]


def load_playbooks(path: Path = DEFAULT_PLAYBOOKS) -> Playbooks:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return Playbooks.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise PolicyError("invalid playbooks", path=str(path), error=str(exc)[:300]) from None
