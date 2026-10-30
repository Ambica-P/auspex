"""Extraction of a typed VendorAdvisory from untrusted bulletin text (spec 12).

The text is data, never instructions: control characters are stripped, it is truncated, and
only the typed, validated fields can influence a plan. Validation is the same whoever
extracts:
- the advisory must be about the fault code that was asked for (else it is dropped);
- recovery time between 0 and 7 days; meaning bounded; versions are identifiers.

`BulletinExtractor` is deterministic: it reads the labelled fields vendor bulletins carry
("Fault code", "Meaning", "Expected recovery", "Manual reset required", "Affected versions").
It is the extractor for LLM_PROVIDER=fake and the fallback when the model fails; Nemotron
Nano extraction (M10) implements the same `Extractor` protocol for bulletins in free form.
"""

from __future__ import annotations

import re
from typing import Protocol

from pydantic import ValidationError

from auspex.core.logging import get_logger
from auspex.core.models import VendorAdvisory, strip_control

log = get_logger(__name__)

MAX_TEXT = 20_000
MAX_RECOVERY_S = 7 * 24 * 3600
_UNITS = {"second": 1, "sec": 1, "s": 1, "minute": 60, "min": 60, "hour": 3600, "h": 3600}


def sanitize(text: str) -> str:
    return strip_control(text)[:MAX_TEXT]


class Extractor(Protocol):
    def extract(self, text: str, fault_code: str) -> VendorAdvisory | None: ...


LABELS = (
    "Fault code",
    "Meaning",
    "Expected recovery",
    "Manual reset required",
    "Affected versions",
)


def normalize_layout(text: str) -> str:
    """Put each bulletin label at the start of its own line, whatever the page became after
    conversion: Tavily's markdown or text can glue a definition list together
    ("Fault codeE-17Meaning...") or wrap it in tables, bullets or bold."""
    text = re.sub(r"[*`#>|]+", " ", text)
    for label in LABELS:
        text = re.sub(rf"{label}\s*[:\-]?[ \t]*", f"\n{label}: ", text)
    return text


def _field(text: str, label: str) -> str | None:
    m = re.search(rf"(?im)^[ \t]*{label}\b[ \t]*[:\-]?\s*(\S.*)$", text)
    return m.group(1).strip() if m else None


def parse_recovery_s(value: str | None) -> int | None:
    if not value:
        return None
    if re.match(r"(?i)^\s*(none|never|no automatic)", value):
        return None
    m = re.search(r"(?i)(\d+(?:\.\d+)?)\s*(seconds?|secs?|s\b|minutes?|mins?|hours?|h\b)", value)
    if not m:
        return None
    unit = m.group(2).lower().rstrip("s") or "s"
    factor = _UNITS.get(unit, _UNITS.get(unit[:3], 0))
    seconds = int(float(m.group(1)) * factor)
    return seconds if 0 <= seconds <= MAX_RECOVERY_S else None


def parse_bool(value: str | None) -> bool | None:
    if not value:
        return None
    v = value.strip().lower()
    if v.startswith(("yes", "required", "true")):
        return True
    if v.startswith(("no", "not required", "false")):
        return False
    return None


def validated(raw: dict[str, object], fault_code: str) -> VendorAdvisory | None:
    """Shared validation for every extractor's output."""
    try:
        adv = VendorAdvisory.model_validate(raw)
    except ValidationError as exc:
        log.warning("extract.invalid", fault_code=fault_code, error=str(exc.errors()[0]["msg"]))
        return None
    if adv.fault_code.upper() != fault_code.upper():
        log.info("extract.other_fault", wanted=fault_code, found=adv.fault_code)
        return None
    if adv.expected_recovery_s is not None and adv.expected_recovery_s > MAX_RECOVERY_S:
        return None
    return adv


class BulletinExtractor:
    def extract(self, text: str, fault_code: str) -> VendorAdvisory | None:
        text = normalize_layout(sanitize(text))
        code = _field(text, "Fault code")
        if code is None:
            log.info("extract.no_fault_code_label", fault_code=fault_code, chars=len(text))
            return None
        versions = _field(text, "Affected versions") or ""
        raw: dict[str, object] = {
            "fault_code": code.split()[0],
            "meaning": (_field(text, "Meaning") or "")[:500],
            "expected_recovery_s": parse_recovery_s(_field(text, "Expected recovery")),
            "requires_manual_reset": parse_bool(_field(text, "Manual reset required")),
            "affected_versions": [
                v.strip().replace(" ", "-")
                for v in versions.split(",")
                if re.fullmatch(r"[A-Za-z0-9._ \-]{1,60}", v.strip())
            ][:20],
        }
        return validated(raw, fault_code)
