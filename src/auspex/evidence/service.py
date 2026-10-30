"""Evidence service (spec 12): vendor advisories for a fault code, as typed, recorded Evidence.

Pipeline: query (vendor, product, fault code) -> cache (by query hash, TTL) or search
(restricted to the vendor's advisory domain, then to its advisory pages) -> each hit:
sanitize, hash, extract a typed VendorAdvisory, store (evidence table, by content hash) ->
ledger EVIDENCE (hash, URL, summary, typed fields; never the full text). No hits: an
EVIDENCE entry saying so; nothing is ever made up. At most `max_searches` searches per
decision (SearchBudget); cache hits do not count.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import yaml
from pydantic import Field, ValidationError
from sqlalchemy import Engine, text

from auspex.core.canonical import sha256_hex
from auspex.core.config import SearchProvider, Settings
from auspex.core.errors import AuspexError, PolicyError
from auspex.core.logging import get_logger
from auspex.core.models import Evidence, EvidenceSource, Identifier, StrictModel
from auspex.core.ports import SearchHit, SearchPort, SearchQuery
from auspex.evidence.extract import BulletinExtractor, Extractor, sanitize
from auspex.evidence.search import (
    LOCAL_HOST,
    FixtureSearch,
    LocalSearch,
    SearchUnavailable,
    TavilySearch,
    query_key,
)
from auspex.ledger.store import LedgerKind, append

log = get_logger(__name__)

VENDORS_FILE = Path(__file__).resolve().parents[3] / "site" / "vendors.yaml"
SUMMARY_MAX = 1000


class BudgetExceeded(AuspexError):
    code = "search_budget_exceeded"


class Vendor(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    advisory_path: str = Field(pattern=r"^[a-z0-9\-]+/$")
    products: list[Identifier] = Field(min_length=1)


class VendorsFile(StrictModel):
    vendors: dict[str, Vendor]


def load_vendors(path: Path = VENDORS_FILE) -> dict[str, Vendor]:
    try:
        return VendorsFile.model_validate(yaml.safe_load(path.read_text("utf-8"))).vendors
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise PolicyError("invalid vendors file", path=str(path), error=str(exc)[:300]) from None


@dataclass
class SearchBudget:
    """Searches allowed for one decision (spec 6.3: at most 3 Tavily searches)."""

    max_searches: int = 3
    used: int = 0

    def take(self) -> None:
        if self.used >= self.max_searches:
            raise BudgetExceeded("search budget for this decision used up", max=self.max_searches)
        self.used += 1


@dataclass
class AdvisoryResult:
    query: str
    evidence: list[Evidence] = field(default_factory=list)
    cached: bool = False
    provider: str = ""

    @property
    def best(self) -> Evidence | None:
        return next((e for e in self.evidence if e.extracted is not None), None)


def make_search(settings: Settings) -> SearchPort:
    if settings.search_provider is SearchProvider.TAVILY:
        assert settings.tavily_api_key is not None  # enforced by Settings
        return TavilySearch(settings.tavily_api_key.get_secret_value(), settings.tavily_record)
    if settings.search_provider is SearchProvider.FIXTURES:
        return FixtureSearch()
    return LocalSearch()


class EvidenceService:
    def __init__(
        self,
        *,
        engine: Engine,
        search: SearchPort,
        provider: SearchProvider,
        vendor_site_url: str | None,
        ttl_s: int,
        extractor: Extractor | None = None,
        vendors: dict[str, Vendor] | None = None,
    ) -> None:
        self.engine = engine
        self.search_port = search
        self.provider = provider
        self.ttl_s = ttl_s
        self.extractor: Extractor = extractor or BulletinExtractor()
        self.vendors = vendors if vendors is not None else load_vendors()
        uses_site = provider is not SearchProvider.LOCAL and vendor_site_url
        self.base = str(vendor_site_url).rstrip("/") if uses_site else f"https://{LOCAL_HOST}"

    def _domain(self) -> str:
        return urlparse(self.base).hostname or LOCAL_HOST

    def _source(self) -> EvidenceSource:
        if self.provider is SearchProvider.LOCAL:
            return EvidenceSource.SYSTEM  # the local mock site, not the web
        return EvidenceSource.TAVILY  # live, or recorded from live

    async def vendor_advisories(
        self, vendor_urn: str, product: str, fault_code: str, budget: SearchBudget
    ) -> AdvisoryResult:
        vendor = self.vendors.get(vendor_urn)
        if vendor is None:
            raise AuspexError("unknown vendor", vendor=vendor_urn)
        q = SearchQuery(
            query=f"{vendor.name} {product} fault code {fault_code} service bulletin",
            include_domains=[self._domain()],
            max_results=3,
        )
        prefix = f"{self.base}/{vendor.advisory_path}"
        result = AdvisoryResult(query=q.query, provider=self.provider.value)
        hits = self._cached(q)
        if hits is not None:
            result.cached = True
        else:
            budget.take()
            try:
                hits = await self.search_port.search(q)
            except SearchUnavailable as exc:
                log.error("evidence.search_failed", query=q.query, error=str(exc))
                self._record_none(q, f"search unavailable: {exc.message}")
                return result
        mine = [h for h in hits if h.url.startswith(prefix)]
        if len(mine) < len(hits):
            log.info("evidence.off_vendor_hits_dropped", dropped=len(hits) - len(mine))
        if not result.cached:
            if not any(fault_code.lower() in h.content.lower() for h in mine):
                mine += await self._from_index(prefix, fault_code)
            self._store_cache(q, mine)
        now = datetime.now(UTC)
        for h in mine:
            result.evidence.append(self._evidence(q, h, fault_code, now))
        if not result.evidence:
            self._record_none(q, "no bulletin found")
        elif result.best is None:
            self._record_none(q, f"no bulletin about {fault_code} among the pages found")
        log.info(
            "evidence.advisories",
            vendor=vendor_urn,
            fault_code=fault_code,
            cached=result.cached,
            hits=len(result.evidence),
            extracted=[e.extracted.model_dump() for e in result.evidence if e.extracted],
        )
        return result

    # ------------------------------------------------------------------ internals

    async def _from_index(self, prefix: str, fault_code: str) -> list[SearchHit]:
        """Search found nothing about the code (a new site may not be indexed yet): read the
        vendor's bulletin index and fetch the bulletins whose link names the fault code."""
        fetch = getattr(self.search_port, "fetch", None)
        if fetch is None:
            return []
        index_url = f"{prefix}index.html"
        try:
            pages = await fetch([index_url])
        except SearchUnavailable as exc:
            log.warning("evidence.index_unavailable", url=index_url, error=str(exc))
            return []
        links: list[str] = []
        for page in pages:
            for text_, href in re.findall(r"\[([^\]]+)\]\(([^)\s]+)\)", page.content):
                url = urljoin(index_url, href)
                if fault_code.lower() in text_.lower() and url.startswith(prefix):
                    links.append(url)
        if not links:
            log.info("evidence.index_no_match", url=index_url, fault_code=fault_code)
            return []
        try:
            found: list[SearchHit] = await fetch(links[:2])
        except SearchUnavailable as exc:
            log.warning("evidence.bulletin_unavailable", urls=links[:2], error=str(exc))
            return []
        log.info("evidence.from_index", fault_code=fault_code, urls=[h.url for h in found])
        return [h for h in found if h.url.startswith(prefix)]

    def _evidence(self, q: SearchQuery, h: SearchHit, fault_code: str, now: datetime) -> Evidence:
        content = sanitize(h.content)
        digest = sha256_hex(content.encode("utf-8"))
        extracted = self.extractor.extract(content, fault_code)
        title = sanitize(h.title)
        body = content[len(title) :] if content.startswith(title) else content
        summary = f"{title}: {' '.join(body.split())[:600]}"  # independent of the fault code
        ev = Evidence(
            source=self._source(),
            url=h.url,
            retrieved_at=now,
            content_sha256=digest,
            summary=summary[:SUMMARY_MAX],
            extracted=extracted,
        )
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO evidence (content_sha256, source, url, retrieved_at, query, data,"
                    " content) VALUES (:h, :src, :url, :at, :q, CAST(:data AS jsonb), :content)"
                    " ON CONFLICT (content_sha256) DO UPDATE SET data = EXCLUDED.data,"
                    " query = EXCLUDED.query, retrieved_at = EXCLUDED.retrieved_at"
                ),
                {
                    "h": digest,
                    "src": ev.source.value,
                    "url": h.url,
                    "at": now,
                    "q": q.query,
                    "data": ev.model_dump_json(),
                    "content": content,
                },
            )
            append(
                conn,
                LedgerKind.EVIDENCE,
                {"query": q.query, "provider": self.provider.value, "evidence": ev},
            )
        return ev

    def _record_none(self, q: SearchQuery, reason: str) -> None:
        with self.engine.begin() as conn:
            append(
                conn,
                LedgerKind.EVIDENCE,
                {"query": q.query, "provider": self.provider.value, "no_evidence": reason},
            )
        log.warning("evidence.none", query=q.query, reason=reason)

    def _cached(self, q: SearchQuery) -> list[SearchHit] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT hits FROM search_cache WHERE query_hash = :k AND provider = :p"
                    " AND fetched_at > now() - make_interval(secs => :ttl)"
                ),
                {"k": query_key(q), "p": self.provider.value, "ttl": self.ttl_s},
            ).first()
        if row is None:
            return None
        return [SearchHit.model_validate(h) for h in row.hits]

    def _store_cache(self, q: SearchQuery, hits: list[SearchHit]) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO search_cache (query_hash, provider, query, hits) VALUES"
                    " (:k, :p, CAST(:q AS jsonb), CAST(:h AS jsonb)) ON CONFLICT (query_hash)"
                    " DO UPDATE SET provider = EXCLUDED.provider, hits = EXCLUDED.hits,"
                    " fetched_at = now()"
                ),
                {
                    "k": query_key(q),
                    "p": self.provider.value,
                    "q": q.model_dump_json(),
                    "h": json.dumps([h.model_dump() for h in hits]),
                },
            )
