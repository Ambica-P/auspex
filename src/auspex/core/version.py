"""Fingerprint of the code a process is running: sha256 over every .py file of the auspex
package (path and contents, sorted). The core reports it in /api/v1/health; the scenario
controller computes the same over the repository and refuses to run against a core that is
still running older code (extracted a new version but did not restart run-core)."""

from __future__ import annotations

import hashlib
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]


def code_fingerprint(root: Path = PACKAGE) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        h.update(path.relative_to(root).as_posix().encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:16]


CODE_FINGERPRINT = code_fingerprint()
