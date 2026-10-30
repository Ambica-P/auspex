"""M9 check (scripts/check-evidence): vendor advisories end to end, without the simulation.

python -m auspex.evidence.check                     # SEARCH_PROVIDER from .env (default local)
python -m auspex.evidence.check --provider tavily   # live Tavily (spends a few credits)
python -m auspex.evidence.check --provider fixtures # replays recorded Tavily responses

Checks: E-17 (lift_1's vendor and product, from the site model) is a door-sensor fault that
recovers by itself in minutes; E-42 needs a technician (manual reset, long recovery); the
injected "instructions" in the E-42 bulletin change nothing (only typed fields come out); an
unknown fault code gives "no evidence", never a guess; the second identical query is a cache
hit; a fourth search in one decision is refused (budget 3); the ledger verifies.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from auspex.core.config import SearchProvider, get_settings
from auspex.core.db import make_engine
from auspex.core.errors import ConfigError
from auspex.evidence.service import (
    BudgetExceeded,
    EvidenceService,
    SearchBudget,
    make_search,
)
from auspex.ledger.store import verify_chain
from auspex.site.model import DEFAULT_SITE_DIR, build_site
from auspex.site.navgraph import load_captures

_results: list[bool] = []


def check(ok: bool, msg: str) -> bool:
    _results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {msg}", flush=True)
    return ok


async def run(provider: SearchProvider | None) -> int:
    try:
        settings = get_settings()
        if provider is not None:
            settings = settings.model_copy(update={"search_provider": provider})
            if provider is SearchProvider.TAVILY and (
                settings.tavily_api_key is None or settings.vendor_site_url is None
            ):
                print("FAIL --provider tavily needs TAVILY_API_KEY and VENDOR_SITE_URL in .env")
                return 1
    except ConfigError as exc:
        print(f"FAIL configuration: {exc}")
        return 1
    engine = make_engine(settings.database_url.get_secret_value())
    site = build_site(DEFAULT_SITE_DIR, load_captures(DEFAULT_SITE_DIR / "nav_graphs"))
    lift = site.segment("segment/lift_1")
    if lift.vendor_urn is None or lift.product is None:
        print("FAIL lift_1 has no vendor or product in site/segments.yaml")
        return 1
    svc = EvidenceService(
        engine=engine,
        search=make_search(settings),
        provider=settings.search_provider,
        vendor_site_url=str(settings.vendor_site_url) if settings.vendor_site_url else None,
        ttl_s=settings.tavily_cache_ttl_s,
    )
    print(
        f"INFO provider {settings.search_provider.value}; lift_1 = {lift.vendor_urn} "
        f"{lift.product}; advisory site {svc.base}"
    )
    budget = SearchBudget(max_searches=3)

    e17 = await svc.vendor_advisories(lift.vendor_urn, lift.product, "E-17", budget)
    a = e17.best.extracted if e17.best else None
    print(f"INFO E-17 query {e17.query!r}: {[e.url for e in e17.evidence]}")
    print(f"INFO E-17 extracted: {a.model_dump() if a else None}")
    check(
        a is not None
        and a.requires_manual_reset is False
        and a.expected_recovery_s is not None
        and a.expected_recovery_s <= 300,
        "E-17: door-sensor fault, recovers by itself within 5 minutes, no manual reset",
    )

    e42 = await svc.vendor_advisories(lift.vendor_urn, lift.product, "E-42", budget)
    b = e42.best.extracted if e42.best else None
    print(f"INFO E-42 extracted: {b.model_dump() if b else None}")
    check(
        b is not None
        and b.requires_manual_reset is True
        and (b.expected_recovery_s is None or b.expected_recovery_s >= 1800),
        "E-42: technician required (manual reset), long outage",
    )
    check(
        b is not None and (b.expected_recovery_s or 0) != 30,
        "E-42: the bulletin's injected 'instructions' changed nothing (only typed fields used)",
    )

    unknown = await svc.vendor_advisories(lift.vendor_urn, lift.product, "E-99", budget)
    check(
        all(e.extracted is None for e in unknown.evidence),
        f"E-99 (no bulletin): no advisory extracted, recorded as no evidence "
        f"({len(unknown.evidence)} unrelated pages kept as untrusted text only)",
    )

    again = await svc.vendor_advisories(lift.vendor_urn, lift.product, "E-17", budget)
    check(again.cached, "repeating the E-17 query is served from the cache (no search)")

    spent = SearchBudget(max_searches=3, used=3)
    try:
        await svc.vendor_advisories(lift.vendor_urn, lift.product, "E-18", spent)
        check(False, "a fourth search in one decision is refused")
    except BudgetExceeded:
        check(True, "a fourth search in one decision is refused (budget 3)")

    check(verify_chain(engine) > 0, "ledger chain verifies (evidence entries recorded)")
    engine.dispose()
    failed = _results.count(False)
    print(
        f"{'PASS' if not failed else 'FAIL'} check-evidence: {len(_results) - failed} passed,"
        f" {failed} failed"
    )
    return 0 if not failed else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--provider", choices=[x.value for x in SearchProvider])
    args = p.parse_args()
    return asyncio.run(run(SearchProvider(args.provider) if args.provider else None))


if __name__ == "__main__":
    sys.exit(main())
