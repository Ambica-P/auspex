"""Facility feed: lift status from the building's lift monitoring system (spec 5.2, S2/S3).

Open-RMF's LiftState carries a mode but no fault code, and the simulated lift always reports
AGV. Real buildings get lift faults (OFFLINE, E-17, ...) from the lift vendor's monitoring
system; this is that feed. A status other than AGV overrides the mode Auspex uses for the
lift until an AGV status clears it. The fault code is untrusted text: it is only matched
against vendor bulletins (M9), never interpreted as an instruction.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from auspex.core.models import StrictModel

LiftMode = Literal["AGV", "HUMAN", "OFFLINE", "FIRE", "EMERGENCY", "UNKNOWN"]


class LiftStatus(StrictModel):
    mode: LiftMode
    fault_code: Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{1,16}$")] | None = None
    status_at: AwareDatetime
