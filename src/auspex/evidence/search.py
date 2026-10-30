"""SearchPort adapters (spec 12): local mock vendor site, recorded fixtures, live Tavily.

All three return `SearchHit`s whose content is untrusted text. The query is always restricted
to the vendor's advisory domain; nothing else is searched.

- LocalSearch: the files under sim/vendor_site/, ranked by query term overlap (no network).
- FixtureSearch: replays responses recorded from live Tavily (tests/fixtures/tavily/<hash>.json,
  keyed by the query's hash); a query never recorded returns no hits ("no evidence").
- TavilySearch: tavily-python's AsyncTavilyClient. `search(query, search_depth, max_results,
  include_domains, include_raw_content, timeout)` and `extract(urls, extract_depth, format,
  timeout)` as in tavily-python 0.8.5 (signatures checked in the installed package, M9). Hits
  with little content are fetched in full with extract. Bounded retries on timeouts and
  network errors; key or quota errors are not retried. With TAVILY_RECORD=true each response
  is written as a fixture.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from pathlib import Path
from typing import Any

from tavily import AsyncTavilyClient, errors

from auspex.core.canonical import canonical_json, sha256_hex
from auspex.core.errors import AuspexError
from auspex.core.logging import get_logger
from auspex.core.ports import SearchHit, SearchQuery

log = get_logger(__name__)

REPO = Path(__file__).resolve().parents[3]
VENDOR_SITE = REPO / "sim" / "vendor_site"
FIXTURES = REPO / "tests" / "fixtures" / "tavily"
LOCAL_HOST = "vendor-site.local"
CONTENT_MAX = 20_000
FULL_FETCH_BELOW = 400  # chars: shorter hits are fetched in full with extract
RETRIES = 2


class SearchUnavailable(AuspexError):
    code = "search_unavailable"


def query_key(q: SearchQuery) -> str:
    return sha256_hex(
        canonical_json(
            {"query": q.query, "include_domains": sorted(q.include_domains), "n": q.max_results}
        )
    )


def urls_key(urls: list[str]) -> str:
    return sha256_hex(canonical_json({"fetch": sorted(urls)}))


def _record(root: Path, key: str, payload: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{key}.json"
    path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    log.info("search.recorded", file=str(path.relative_to(REPO)))


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<\s*(br|/p|/dd|/dt|/li|/h[1-6]|/title)\s*>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


# --------------------------------------------------------------------------- local


class LocalSearch:
    def __init__(self, root: Path = VENDOR_SITE) -> None:
        self.root = root

    async def search(self, query: SearchQuery) -> list[SearchHit]:
        terms = {t.lower() for t in re.findall(r"[A-Za-z0-9-]{2,}", query.query)}
        scored: list[tuple[int, SearchHit]] = []
        if LOCAL_HOST not in query.include_domains:
            return []
        for path in sorted(self.root.rglob("*.html")):
            rel = path.relative_to(self.root).as_posix()
            url = f"https://{LOCAL_HOST}/{rel}"
            raw = path.read_text(encoding="utf-8")
            title_m = re.search(r"(?is)<title>(.*?)</title>", raw)
            title = html.unescape(title_m.group(1).strip()) if title_m else rel
            text = html_to_text(raw)
            words = {w.lower() for w in re.findall(r"[A-Za-z0-9-]{2,}", text)}
            score = len(terms & words)
            if score:
                hit = SearchHit(url=url, title=title[:500], content=text[:CONTENT_MAX])
                scored.append((score, hit))
        scored.sort(key=lambda x: -x[0])
        hits = [h for _, h in scored[: query.max_results]]
        log.info("search.local", query=query.query, hits=[h.url for h in hits])
        return hits


# --------------------------------------------------------------------------- fixtures


class FixtureSearch:
    def __init__(self, root: Path = FIXTURES) -> None:
        self.root = root

    async def search(self, query: SearchQuery) -> list[SearchHit]:
        path = self.root / f"{query_key(query)}.json"
        if not path.is_file():
            log.warning("search.fixture_missing", query=query.query, file=path.name)
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        hits = [SearchHit.model_validate(h) for h in data["hits"]]
        log.info("search.fixture", query=query.query, hits=[h.url for h in hits])
        return hits

    async def fetch(self, urls: list[str]) -> list[SearchHit]:
        path = self.root / f"{urls_key(urls)}.json"
        if not path.is_file():
            log.warning("search.fixture_missing", urls=urls, file=path.name)
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return [SearchHit.model_validate(h) for h in data["hits"]]


# --------------------------------------------------------------------------- tavily


class TavilySearch:
    def __init__(self, api_key: str, record: bool = False, fixtures: Path = FIXTURES) -> None:
        self.client = AsyncTavilyClient(api_key)
        self.record = record
        self.fixtures = fixtures

    async def _call(self, what: str, fn: Any, timeout_s: float, **kwargs: Any) -> dict[str, Any]:
        fatal = (
            errors.InvalidAPIKeyError,
            errors.UsageLimitExceededError,
            errors.ForbiddenError,
            errors.BadRequestError,
            errors.MissingAPIKeyError,
        )
        last = ""
        for attempt in range(1, RETRIES + 2):
            try:
                result = await asyncio.wait_for(fn(timeout=timeout_s, **kwargs), timeout_s + 5)
                return dict(result)
            except fatal as exc:
                raise SearchUnavailable(
                    f"tavily {what} refused", error=type(exc).__name__
                ) from None
            except (TimeoutError, errors.TimeoutError, OSError) as exc:
                last = type(exc).__name__
                log.warning("search.tavily_retry", what=what, attempt=attempt, error=last)
                await asyncio.sleep(attempt)
            except Exception as exc:  # network stack errors vary by version: bounded retry
                last = f"{type(exc).__name__}: {str(exc)[:120]}"
                log.warning("search.tavily_retry", what=what, attempt=attempt, error=last)
                await asyncio.sleep(attempt)
        raise SearchUnavailable(f"tavily {what} failed", error=last)

    async def search(self, query: SearchQuery) -> list[SearchHit]:
        resp = await self._call(
            "search",
            self.client.search,
            query.timeout_s,
            query=query.query,
            search_depth="basic",
            max_results=query.max_results,
            include_domains=list(query.include_domains),
            include_raw_content=True,
        )
        hits: list[SearchHit] = []
        short: list[str] = []
        for r in resp.get("results", [])[: query.max_results]:
            url = str(r.get("url", ""))[:2000]
            body = str(r.get("raw_content") or r.get("content") or "")
            if len(body) < FULL_FETCH_BELOW:
                short.append(url)
            hits.append(
                SearchHit(url=url, title=str(r.get("title", ""))[:500], content=body[:CONTENT_MAX])
            )
        if short:
            full = await self._call(
                "extract", self.client.extract, query.timeout_s, urls=short, format="text"
            )
            by_url = {
                str(x.get("url")): str(x.get("raw_content") or "") for x in full.get("results", [])
            }
            hits = [
                h.model_copy(update={"content": by_url[h.url][:CONTENT_MAX]})
                if by_url.get(h.url)
                else h
                for h in hits
            ]
        log.info("search.tavily", query=query.query, hits=[h.url for h in hits])
        if self.record:
            _record(
                self.fixtures,
                query_key(query),
                {"query": query.model_dump(), "hits": [h.model_dump() for h in hits]},
            )
        return hits

    async def fetch(self, urls: list[str]) -> list[SearchHit]:
        """Fetch known pages in full (markdown keeps links), via Tavily extract."""
        resp = await self._call(
            "extract", self.client.extract, 20.0, urls=list(urls), format="markdown"
        )
        hits = [
            SearchHit(
                url=str(r.get("url", ""))[:2000],
                title="",
                content=str(r.get("raw_content") or "")[:CONTENT_MAX],
            )
            for r in resp.get("results", [])
        ]
        log.info("search.tavily_fetch", urls=urls, got=[h.url for h in hits])
        if self.record:
            _record(
                self.fixtures,
                urls_key(urls),
                {"urls": urls, "hits": [h.model_dump() for h in hits]},
            )
        return hits
