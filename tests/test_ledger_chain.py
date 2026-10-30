"""Spec 18.4 test 2: the ledger chain verifies, and detects a modified entry."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from auspex.core.errors import LedgerChainBroken
from auspex.ledger.chain import GENESIS, LedgerEntry, compute_hash, make_body, verify_entries
from auspex.ledger.store import LedgerKind, append, verify_chain
from tests.conftest import DbHandles


def _chain(payloads: list[dict[str, Any]]) -> list[LedgerEntry]:
    entries: list[LedgerEntry] = []
    prev = GENESIS
    for i, p in enumerate(payloads, start=1):
        at = datetime(2026, 10, 26, 10, i, tzinfo=UTC)
        body = make_body(i, f"e{i}", "plan", at, p)
        h = compute_hash(prev, body)
        entries.append(
            LedgerEntry(
                seq=i,
                entry_id=f"e{i}",
                kind="plan",
                recorded_at=at,
                payload=p,
                body=body,
                prev_hash=prev,
                entry_hash=h,
            )
        )
        prev = h
    return entries


def test_chain_verifies_and_detects_modification() -> None:
    entries = _chain([{"plan_id": "p1", "eta_s": 120.5}, {"plan_id": "p2"}, {"plan_id": "p3"}])
    assert verify_entries(entries) == 3

    tampered_payload = list(entries)
    tampered_payload[1] = entries[1].model_copy(update={"payload": {"plan_id": "pX"}})
    with pytest.raises(LedgerChainBroken):
        verify_entries(tampered_payload)

    tampered_body = list(entries)
    tampered_body[1] = entries[1].model_copy(update={"body": entries[1].body.replace("p2", "pX")})
    with pytest.raises(LedgerChainBroken):
        verify_entries(tampered_body)

    with pytest.raises(LedgerChainBroken):
        verify_entries([entries[0], entries[2]])  # deleted entry


def test_chain_in_database_detects_modification(test_db: DbHandles) -> None:
    with test_db.app.begin() as conn:
        for i in range(3):
            append(conn, LedgerKind.SYSTEM, {"note": f"entry {i}", "n": i})
    assert verify_chain(test_db.app) >= 3

    # Tamper as the superuser, bypassing the append-only trigger.
    with test_db.admin.begin() as conn:
        conn.execute(text("ALTER TABLE ledger DISABLE TRIGGER ledger_no_update_delete"))
        conn.execute(  # this test's own entry (other tests may have written first)
            text(
                "UPDATE ledger SET body = replace(body, 'entry 1', 'entry X')"
                " WHERE kind = 'system' AND payload->>'note' = 'entry 1'"
            )
        )
        conn.execute(text("ALTER TABLE ledger ENABLE TRIGGER ledger_no_update_delete"))
    with pytest.raises(LedgerChainBroken):
        verify_chain(test_db.app)

    # Restore, so other tests see a valid chain.
    with test_db.admin.begin() as conn:
        conn.execute(text("ALTER TABLE ledger DISABLE TRIGGER ledger_no_update_delete"))
        conn.execute(  # this test's own entry (other tests may have written first)
            text(
                "UPDATE ledger SET body = replace(body, 'entry X', 'entry 1')"
                " WHERE kind = 'system' AND payload->>'note' = 'entry 1'"
            )
        )
        conn.execute(text("ALTER TABLE ledger ENABLE TRIGGER ledger_no_update_delete"))
    assert verify_chain(test_db.app) >= 3
