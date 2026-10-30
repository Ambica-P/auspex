"""Decision sandbox API (spec 14.6). Run: uvicorn sandbox.app:app (scripts/run-sandbox), or the
container from sandbox/Dockerfile on a Nebius Serverless Endpoint.

Controls, all enforced before any model call:
- SANDBOX_ENABLED kill switch (off: every request gets the "paused" message);
- a global daily cap on model calls (UTC day), and a per-IP rate limit per minute;
- a strict request model (unknown fields rejected) with a maximum length per field;
- a cache keyed by the hash of the normalised input (repeats cost nothing);
- CORS limited to the replay site's origin; the Token Factory key never leaves the server;
- one structlog line per request: request id, input hash, model, latency, tokens, verdict;
  never the input text, never a secret.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from auspex.agent.agent import Agent
from auspex.agent.fake import FakeLLM
from auspex.agent.llm import LLMUnavailable, OpenAICompatLLM
from auspex.core.logging import configure_logging, get_logger
from auspex.core.ports import LLMPort, ModelRole
from sandbox.decide import World, decide, load_world, validate_input
from sandbox.settings import SandboxSettings

log = get_logger(__name__)

PAUSED = "Sandbox paused; see the replay and the video."
CAPPED = "Demo limit reached for today; see the replay and the video."
SPENT = "The demo's model budget is used up; see the replay and the video."
EST_PROMPT, EST_COMPLETION = 1200, 400  # one interpretation, to check the budget before a call
LIMITED = "Too many requests from your address; try again in a minute."


class Limits:
    """In-memory counters (one container): spending budget, daily model-call cap, per-IP rate."""

    def __init__(
        self,
        daily_cap: int,
        per_ip_per_min: int,
        budget_usd: float = 0.0,
        price_in: float | None = None,
        price_out: float | None = None,
    ) -> None:
        self.daily_cap = daily_cap
        self.per_ip = per_ip_per_min
        self.budget_usd = budget_usd
        self.price_in = price_in or 0.0
        self.price_out = price_out or 0.0
        self.spent_usd = 0.0
        self._lock = threading.Lock()
        self._day = ""
        self._used = 0
        self._hits: dict[str, deque[float]] = {}

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (prompt_tokens * self.price_in + completion_tokens * self.price_out) / 1_000_000

    def can_spend(self) -> bool:
        if self.budget_usd <= 0:
            return True
        with self._lock:
            return self.spent_usd + self.cost(EST_PROMPT, EST_COMPLETION) <= self.budget_usd

    def spend(self, prompt_tokens: int, completion_tokens: int) -> None:
        with self._lock:
            self.spent_usd += self.cost(prompt_tokens, completion_tokens)

    def allow_ip(self, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(ip, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_ip:
                return False
            q.append(now)
            if len(self._hits) > 10_000:  # bound memory: drop idle addresses
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return True

    def take_call(self) -> bool:
        today = datetime.now(UTC).date().isoformat()
        with self._lock:
            if today != self._day:
                self._day, self._used = today, 0
            if self._used >= self.daily_cap:
                return False
            self._used += 1
            return True

    def used(self) -> int:
        return self._used


class Cache:
    def __init__(self, size: int) -> None:
        self.size = size
        self._data: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    def put(self, key: str, value: dict[str, Any]) -> None:
        if self.size == 0:
            return
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.size:
                self._data.popitem(last=False)


def make_llm(settings: SandboxSettings) -> LLMPort:
    if settings.sandbox_llm == "fake":
        return FakeLLM()
    assert settings.nebius_api_base and settings.nebius_api_key and settings.model_triage
    return OpenAICompatLLM(
        base_url=str(settings.nebius_api_base),
        api_key=settings.nebius_api_key.get_secret_value(),
        models={ModelRole.TRIAGE: settings.model_triage, ModelRole.REASON: settings.model_triage},
        thinking=False,
        provider="tokenfactory",
    )


class State:
    settings: SandboxSettings
    world: World
    agent: Agent
    limits: Limits
    cache: Cache


state = State()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = SandboxSettings()
    configure_logging(settings.log_level, json=True)
    state.settings = settings
    state.world = load_world()
    state.agent = Agent(make_llm(settings), debug=False)
    state.limits = Limits(
        settings.sandbox_daily_cap,
        settings.sandbox_rate_per_ip_per_min,
        settings.sandbox_budget_usd if settings.sandbox_llm == "tokenfactory" else 0.0,
        settings.sandbox_price_in_per_mtok,
        settings.sandbox_price_out_per_mtok,
    )
    state.cache = Cache(settings.sandbox_cache_size)
    log.info(
        "sandbox.started",
        enabled=settings.sandbox_enabled,
        llm=settings.sandbox_llm,
        model=settings.model_triage,
        daily_cap=settings.sandbox_daily_cap,
        origin=settings.sandbox_allowed_origin,
    )
    yield


app = FastAPI(title="Auspex decision sandbox", lifespan=lifespan, docs_url=None, redoc_url=None)
_origin = SandboxSettings().sandbox_allowed_origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_origin] if _origin else [],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
    max_age=3600,
)


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def _refuse(status: int, message: str, rid: str, **fields: Any) -> JSONResponse:
    log.info("sandbox.refused", request_id=rid, status=status, **fields)
    return JSONResponse({"ok": False, "message": message, "request_id": rid}, status_code=status)


@app.get("/health")
def health() -> dict[str, Any]:
    s = state.settings
    return {
        "status": "ok",
        "enabled": s.sandbox_enabled,
        "llm": s.sandbox_llm,
        "model": s.model_triage if s.sandbox_llm == "tokenfactory" else "fake-deterministic",
        "calls_today": state.limits.used(),
        "daily_cap": s.sandbox_daily_cap,
        "budget_usd": state.limits.budget_usd,
        "spent_usd": round(state.limits.spent_usd, 6),
        "snapshot": state.world.snapshot.get("source"),
    }


@app.post("/api/s1/decide")
async def s1_decide(request: Request) -> JSONResponse:
    rid = uuid.uuid4().hex[:12]
    s = state.settings
    if not s.sandbox_enabled:
        return _refuse(503, PAUSED, rid, reason="disabled")
    if not state.limits.allow_ip(_ip(request)):
        return _refuse(429, LIMITED, rid, reason="rate_limited")
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        inp = validate_input(body, s.sandbox_max_input_chars)
    except (ValueError, ValidationError) as exc:
        detail = (
            [{"field": ".".join(map(str, e["loc"])), "problem": e["msg"]} for e in exc.errors()]
            if isinstance(exc, ValidationError)
            else [{"field": "body", "problem": str(exc)[:200]}]
        )
        log.info("sandbox.invalid_input", request_id=rid, problems=len(detail))
        return JSONResponse(
            {"ok": False, "message": "invalid input", "problems": detail, "request_id": rid},
            status_code=422,
        )
    key = hashlib.sha256(
        json.dumps(inp.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    cached = state.cache.get(key)
    if cached is not None:
        log.info(
            "sandbox.cache_hit", request_id=rid, input_hash=key[:16], verdict=cached["verdict"]
        )
        return JSONResponse({**cached, "ok": True, "cached": True, "request_id": rid})
    if not state.limits.can_spend():
        return _refuse(429, SPENT, rid, reason="budget", spent_usd=state.limits.spent_usd)
    if not state.limits.take_call():
        return _refuse(429, CAPPED, rid, reason="daily_cap", input_hash=key[:16])
    t0 = time.monotonic()
    try:
        result = await decide(state.world, state.agent, inp)
    except LLMUnavailable as exc:
        log.error("sandbox.llm_unavailable", request_id=rid, input_hash=key[:16], error=exc.message)
        return _refuse(502, "Nemotron did not answer; try again shortly.", rid)
    models = result["models"]
    state.limits.spend(
        sum(m["prompt_tokens"] for m in models), sum(m["completion_tokens"] for m in models)
    )
    log.info(
        "sandbox.request",
        request_id=rid,
        input_hash=key[:16],
        model=[m["model_id"] for m in models],
        tokens=sum(m["tokens"] for m in models),
        llm_ms=sum(m["latency_ms"] for m in models),
        total_ms=round((time.monotonic() - t0) * 1000),
        verdict=result["verdict"],
        rules=result["rule_ids"],
        spent_usd=round(state.limits.spent_usd, 6),
    )
    state.cache.put(key, result)
    return JSONResponse({**result, "ok": True, "cached": False, "request_id": rid})
