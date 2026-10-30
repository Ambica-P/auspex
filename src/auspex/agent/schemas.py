"""What Nemotron may answer (spec 6.3): typed, validated, never an action by itself.

Each task's answer is a Pydantic model; its JSON schema is sent as the structured output
format. Validation against the site, the options offered and the evidence offered happens in
agent/agent.py; an answer that fails it gets one repair attempt, then the agent fails closed.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from auspex.core.models import (
    Confidence,
    Identifier,
    LongText,
    Sha256Hex,
    ShortText,
    StrictModel,
    VendorAdvisory,
)
from auspex.core.urns import Urn

Message = Annotated[str, Field(min_length=1, max_length=300)]


class Interpretation(StrictModel):
    """Nano: the transport job's free text read against the site's zones (S1)."""

    destination_zone: Urn | None  # one of the zones offered, or null when unsure
    confidence: Confidence
    route_constraints: list[ShortText] = Field(default_factory=list, max_length=5)
    rationale: ShortText


class AdvisoryExtraction(StrictModel):
    """Nano: the typed fields of one vendor bulletin (or null if it is about another fault)."""

    advisory: VendorAdvisory | None


class OutageEstimate(StrictModel):
    """Super: how long the outage will last, from the evidence offered (S2)."""

    expected_outage_s: int | None = Field(ge=0, le=86_400)  # null: cannot tell
    confidence: Confidence
    needs_technician: bool | None
    evidence_refs: list[Sha256Hex] = Field(default_factory=list, max_length=10)
    rationale: ShortText


class OptionChoice(StrictModel):
    """Super: which evaluated option to apply, and the explanation a person will read."""

    option_id: Identifier
    rationale: LongText
    evidence_refs: list[Sha256Hex] = Field(default_factory=list, max_length=10)


class NotifySpec(StrictModel):
    recipient_urn: Urn
    message: Message


class CompositeProposal(StrictModel):
    """Super: a composite option built from the parts offered (S3)."""

    option_id: Identifier
    handover_from_zone: Urn | None = None  # a supply zone offered for the robot job's payload
    people_sequence: list[Urn] = Field(default_factory=list, max_length=5)  # suggested order
    notify: list[NotifySpec] = Field(default_factory=list, max_length=3)
    rationale: ShortText


class TradeOff(StrictModel):
    proposals: list[CompositeProposal] = Field(min_length=1, max_length=3)
    chosen: Identifier
    rationale: LongText  # cites notes and evidence; read by the person who approves


class CauseDraft(StrictModel):
    description: ShortText
    evidence_refs: list[Sha256Hex] = Field(min_length=1, max_length=10)


class IncidentReportDraft(StrictModel):
    """Nano: possible causes after an incident, each tied to ledger evidence."""

    summary: LongText
    causes: list[CauseDraft] = Field(max_length=10)
