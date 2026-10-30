"""Data for the judge site in web/ (spec 14.5).

Usage: python -m auspex.replay.build [--repo URL] [--sandbox URL]

The Next.js site reads its data from web/public/: the recorded runs (web/public/replays/*.json,
written by scripts/record-replays), the run index (replays/index.json, written here) and the
site settings with the benchmark table (site.json, written here). Recordings made before the
move to Next.js (ui/replays/) are moved over first. Every file under web/public/ is then
scanned for secrets: the values of every secret in .env and key-shaped strings. Any hit
deletes the written files and fails the build; nothing should be committed or deployed.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from pydantic import SecretStr, ValidationError

from auspex.core.config import Settings
from auspex.core.logging import configure_logging, get_logger

log = get_logger(__name__)

REPO = Path(__file__).resolve().parents[3]
PUBLIC = REPO / "web" / "public"
DEFAULT_REPO = "https://github.com/ambica-p/auspex"
REPLAYS = PUBLIC / "replays"
LEGACY_REPLAYS = REPO / "ui" / "replays"
TABLE = REPO / "bench" / "results" / "table.json"
KEY_SHAPES = [
    re.compile(r"tvly-[A-Za-z0-9_\-]{12,}"),  # Tavily keys
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^\s\"']*:[^\s\"'@]+@"),  # URL with a password
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
MIN_SECRET_LEN = 8


class SecretFound(RuntimeError):
    pass


def secret_values() -> list[str]:
    try:
        settings = Settings()
    except ValidationError as exc:
        raise SystemExit(
            f"FAIL .env incomplete, cannot check the bundle for secrets: {exc}"
        ) from None
    values: list[str] = []
    for name in type(settings).model_fields:
        v = getattr(settings, name)
        if isinstance(v, SecretStr):
            s = v.get_secret_value()
            if len(s) >= MIN_SECRET_LEN:
                values.append(s)
                # passwords also appear inside URLs
                m = re.match(r"^[a-z+]+://[^:/]+:([^@]+)@", s)
                if m and len(m.group(1)) >= MIN_SECRET_LEN:
                    values.append(m.group(1))
    env = REPO / ".env"
    if env.is_file():  # also secrets the core does not read (e.g. the sandbox's own key)
        for line in env.read_text("utf-8").splitlines():
            key, sep, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if (
                sep
                and re.search(r"KEY|TOKEN|PASSWORD|SECRET", key)
                and len(value) >= MIN_SECRET_LEN
            ):
                values.append(value)
    return values


def scan(root: Path, secrets: list[str]) -> list[str]:
    hits: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text("utf-8", errors="replace")
        rel = path.relative_to(root).as_posix()
        for s in secrets:
            if s in text:
                hits.append(f"{rel}: contains the value of a secret from .env")
        for pat in KEY_SHAPES:
            if pat.search(text):
                hits.append(f"{rel}: contains a key-shaped string ({pat.pattern[:24]}...)")
    return hits


def run_index(files: list[Path]) -> list[dict[str, Any]]:
    runs = []
    for f in files:
        data = json.loads(f.read_text("utf-8"))
        meta = data["meta"]
        runs.append(
            {
                "file": f"replays/{f.name}",
                "scenario": meta["scenario"],
                "config": meta["config"],
                "branch": meta.get("branch", "approve"),
                "description": meta.get("description", ""),
                "models": meta.get("models"),
                "llm_provider": meta.get("llm_provider"),
                "search_provider": meta.get("search_provider"),
                "passed": all(c["ok"] for c in meta.get("checks", [])),
                "recorded_at": meta.get("recorded_at"),
                "size_kb": f.stat().st_size // 1024,
            }
        )
    return runs


def migrate_legacy() -> int:
    """Move recordings from ui/replays/ (before the Next.js site) to web/public/replays/."""
    if not LEGACY_REPLAYS.is_dir():
        return 0
    REPLAYS.mkdir(parents=True, exist_ok=True)
    moved = 0
    for f in sorted(LEGACY_REPLAYS.glob("*.json")):
        if f.name == "index.json":
            f.unlink()
            continue
        shutil.move(str(f), REPLAYS / f.name)
        moved += 1
    if not any(LEGACY_REPLAYS.iterdir()):
        LEGACY_REPLAYS.rmdir()
    log.info("replay.legacy_moved", count=moved)
    return moved


def build(repo: str | None, sandbox: str | None = None, public: Path = PUBLIC) -> Path:
    replays = public / "replays"
    files = sorted(f for f in replays.glob("*.json") if f.name != "index.json")
    if not files:
        raise SystemExit(
            f"FAIL no recorded runs in {replays.relative_to(REPO)}/: "
            "run scripts/record-replays first"
        )
    runs = run_index(files)
    index = replays / "index.json"
    site_file = public / "site.json"
    index.write_text(json.dumps(runs, indent=1), "utf-8")
    table = json.loads(TABLE.read_text("utf-8")) if TABLE.is_file() else None
    # sandbox_url is informational only: the browser calls /api/playground, and the Next.js
    # server forwards to SANDBOX_URL (a Vercel environment variable).
    site = {"repo_url": repo, "sandbox_url": sandbox, "table": table}
    site_file.write_text(json.dumps(site, indent=1), "utf-8")
    hits = scan(public, secret_values())
    if hits:
        index.unlink(missing_ok=True)
        site_file.unlink(missing_ok=True)
        raise SecretFound("; ".join(hits[:10]))
    log.info("replay.web_data_built", out=str(public), runs=len(runs), table=table is not None)
    for r in runs:
        print(
            f"  {r['scenario']:<7} {r['config']} {r['branch']:<9} "
            f"{'PASS' if r['passed'] else 'FAIL'} {r['size_kb']} KB  ({r['llm_provider']})"
        )
    if table is None:
        print("  WARN no bench/results/table.json: run python -m bench.report first")
    return public


def main() -> int:
    configure_logging()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--repo", default=DEFAULT_REPO, help=f"public repository URL (default {DEFAULT_REPO})"
    )
    p.add_argument(
        "--sandbox", help="sandbox URL (informational; the site proxies via SANDBOX_URL)"
    )
    args = p.parse_args()
    if args.repo and not re.fullmatch(r"https://[\w.\-]+/[\w.\-/]+", args.repo):
        print(f"FAIL --repo must be an https URL: {args.repo}")
        return 1
    for name in ("repo", "sandbox"):
        value = getattr(args, name)
        if value and not re.fullmatch(r"https?://[\w.\-:]+(/[\w.\-/]*)?", value):
            print(f"FAIL --{name} must be a plain http(s) URL: {value}")
            return 1
    moved = migrate_legacy()
    if moved:
        print(f"  moved {moved} recordings from ui/replays/ to web/public/replays/")
    try:
        out = build(args.repo, sandbox=args.sandbox)
    except SecretFound as exc:
        print(f"FAIL {exc}")
        return 1
    print(f"PASS site data written to {out.relative_to(REPO)} (no secrets found)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
