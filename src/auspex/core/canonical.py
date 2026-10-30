"""Canonical JSON: one byte sequence per value, so hashes are stable.

Rules: keys sorted, no whitespace, UTF-8, datetimes as UTC ISO-8601 with `Z`,
enums by value, Pydantic models by their JSON dump, finite floats only.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from auspex.core.errors import CanonicalJsonError


def _normalize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return _normalize(value.value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise CanonicalJsonError("naive datetime is not allowed", value=str(value))
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalJsonError("non-finite float is not allowed")
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalJsonError("object keys must be strings", key=repr(k))
            out[k] = _normalize(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    raise CanonicalJsonError("unsupported type", type=type(value).__name__)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
