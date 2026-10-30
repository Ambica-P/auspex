"""Spec 18.4 test 5: the app database role cannot update or delete ledger rows."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from auspex.ledger.store import LedgerKind, append
from tests.conftest import DbHandles


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ledger SET kind = 'x' WHERE seq = 1",
        "DELETE FROM ledger WHERE seq = 1",
        "TRUNCATE ledger",
    ],
)
def test_app_role_cannot_modify_ledger(test_db: DbHandles, statement: str) -> None:
    with test_db.app.begin() as conn:
        append(conn, LedgerKind.SYSTEM, {"note": "role test"})
    with pytest.raises(DBAPIError) as info, test_db.app.begin() as conn:
        conn.execute(text(statement))
    assert "permission denied" in str(info.value) or "append-only" in str(info.value)


def test_trigger_blocks_even_the_owner(test_db: DbHandles) -> None:
    with pytest.raises(DBAPIError, match="append-only"), test_db.admin.begin() as conn:
        conn.execute(text("DELETE FROM ledger"))
