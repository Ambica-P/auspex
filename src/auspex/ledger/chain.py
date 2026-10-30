"""Hash chain: entry_hash = sha256(prev_hash || body), body = canonical_json(envelope).

The envelope holds the entry's sequence number, id, kind, time and payload, so changing
any of them breaks the chain. The canonical body is stored verbatim (JSONB would not
preserve number formatting); the `payload` column is a queryable copy and is checked
against the body during verification. The first entry chains from GENESIS.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from auspex.core.canonical import canonical_json, sha256_hex
from auspex.core.errors import LedgerChainBroken
from auspex.core.models import StrictModel

GENESIS = "0" * 64


class LedgerEntry(StrictModel):
    seq: int
    entry_id: str
    kind: str
    recorded_at: datetime
    payload: dict[str, Any]
    body: str
    prev_hash: str
    entry_hash: str


def make_body(
    seq: int, entry_id: str, kind: str, recorded_at: datetime, payload: Mapping[str, Any]
) -> str:
    env = {
        "seq": seq,
        "entry_id": entry_id,
        "kind": kind,
        "recorded_at": recorded_at,
        "payload": dict(payload),
    }
    return canonical_json(env).decode("utf-8")


def compute_hash(prev_hash: str, body: str) -> str:
    return sha256_hex(prev_hash.encode("ascii") + body.encode("utf-8"))


def verify_entries(entries: Iterable[LedgerEntry]) -> int:
    """Check every link; return the entry count. Raise LedgerChainBroken at the first bad one."""
    prev = GENESIS
    expected_seq = 1
    count = 0
    for e in entries:
        if e.seq != expected_seq:
            raise LedgerChainBroken("sequence gap", seq=e.seq, expected=expected_seq)
        if e.prev_hash != prev:
            raise LedgerChainBroken("prev_hash does not match previous entry", seq=e.seq)
        if compute_hash(e.prev_hash, e.body) != e.entry_hash:
            raise LedgerChainBroken("entry_hash does not match body", seq=e.seq)
        env = json.loads(e.body)
        if (env["seq"], env["entry_id"], env["kind"]) != (e.seq, e.entry_id, e.kind):
            raise LedgerChainBroken("columns do not match body", seq=e.seq)
        if env["payload"] != e.payload:
            raise LedgerChainBroken("payload column does not match body", seq=e.seq)
        prev = e.entry_hash
        expected_seq += 1
        count += 1
    return count
