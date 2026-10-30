"""URNs: every entity is addressed as `kind/id`, for example `zone/imaging-L1`."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator

from auspex.core.errors import InvalidUrn

_KIND = r"[a-z][a-z0-9_]{0,31}"
_ID = r"[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}"
_URN_RE = re.compile(rf"^(?P<kind>{_KIND})/(?P<id>{_ID})$")
_KIND_RE = re.compile(rf"^{_KIND}$")
_ID_RE = re.compile(rf"^{_ID}$")


def make_urn(kind: str, ident: str) -> str:
    if not _KIND_RE.fullmatch(kind):
        raise InvalidUrn("invalid urn kind", kind=kind)
    if not _ID_RE.fullmatch(ident):
        raise InvalidUrn("invalid urn id", kind=kind, ident=ident)
    return f"{kind}/{ident}"


def parse_urn(urn: str) -> tuple[str, str]:
    m = _URN_RE.fullmatch(urn)
    if m is None:
        raise InvalidUrn("malformed urn", urn=urn)
    return m.group("kind"), m.group("id")


def urn_kind(urn: str) -> str:
    return parse_urn(urn)[0]


def _validate(value: str) -> str:
    try:
        parse_urn(value)
    except InvalidUrn as exc:
        raise ValueError(str(exc)) from exc
    return value


Urn = Annotated[str, AfterValidator(_validate)]
"""Pydantic field type for a validated URN."""
