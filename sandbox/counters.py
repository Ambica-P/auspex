"""Usage counters for the sandbox: total model spend and model calls per UTC day.

Two stores behind one interface:
- MemoryCounters: one process, lost on restart (local runs).
- UpstashCounters: Upstash Redis over its REST API, so the counters survive restarts and
  sleeping hosts (Render's free plan restarts the container after ~15 idle minutes).

Spend is kept in integer micro-dollars. A call reserves its estimated cost before the model is
asked (refused if the reservation would pass the budget) and settles the difference afterwards,
so concurrent requests cannot overspend. If the store cannot be reached, callers fail closed.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from auspex.core.logging import get_logger

log = get_logger(__name__)

PREFIX = "auspex:sandbox"
SPENT_KEY = f"{PREFIX}:spent_micro_usd"
DAY_TTL_S = 2 * 24 * 3600
TIMEOUT_S = 3.0


class CountersUnavailable(RuntimeError):
    pass


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _calls_key(day: str) -> str:
    return f"{PREFIX}:calls:{day}"


class Counters(Protocol):
    name: str

    async def reserve(self, micro: int, budget_micro: int) -> bool: ...
    async def settle(self, delta_micro: int) -> None: ...
    async def take_call(self, cap: int) -> bool: ...
    async def snapshot(self) -> tuple[int, int]: ...  # (spent micro-USD, calls today)


class MemoryCounters:
    name = "memory"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spent = 0
        self._calls: dict[str, int] = {}

    async def reserve(self, micro: int, budget_micro: int) -> bool:
        with self._lock:
            if budget_micro > 0 and self._spent + micro > budget_micro:
                return False
            self._spent += micro
            return True

    async def settle(self, delta_micro: int) -> None:
        with self._lock:
            self._spent = max(0, self._spent + delta_micro)

    async def take_call(self, cap: int) -> bool:
        day = _today()
        with self._lock:
            self._calls = {day: self._calls.get(day, 0)}
            if self._calls[day] >= cap:
                return False
            self._calls[day] += 1
            return True

    async def snapshot(self) -> tuple[int, int]:
        with self._lock:
            return self._spent, self._calls.get(_today(), 0)


class UpstashCounters:
    """Upstash Redis REST: POST <url>/pipeline with [[cmd, args...], ...], bearer token;
    the reply is a list of {"result": ...} or {"error": ...}, one per command."""

    name = "upstash"

    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=TIMEOUT_S, headers={"Authorization": f"Bearer {token}"}
        )

    async def _pipeline(self, *commands: list[str]) -> list[Any]:
        try:
            res = await self._client.post(f"{self._url}/pipeline", json=list(commands))
            res.raise_for_status()
            body = res.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.error("sandbox.counters_unreachable", store=self.name, error=type(exc).__name__)
            raise CountersUnavailable(type(exc).__name__) from None
        if not isinstance(body, list) or len(body) != len(commands):
            raise CountersUnavailable("unexpected reply")
        out: list[Any] = []
        for item in body:
            if not isinstance(item, dict) or "error" in item:
                err = item.get("error") if isinstance(item, dict) else "bad item"
                log.error("sandbox.counters_error", store=self.name, error=str(err)[:120])
                raise CountersUnavailable(str(err)[:120])
            out.append(item.get("result"))
        return out

    async def reserve(self, micro: int, budget_micro: int) -> bool:
        (total,) = await self._pipeline(["INCRBY", SPENT_KEY, str(micro)])
        if budget_micro > 0 and int(total) > budget_micro:
            await self._pipeline(["DECRBY", SPENT_KEY, str(micro)])
            return False
        return True

    async def settle(self, delta_micro: int) -> None:
        if delta_micro:
            await self._pipeline(["INCRBY", SPENT_KEY, str(delta_micro)])

    async def take_call(self, cap: int) -> bool:
        key = _calls_key(_today())
        n, _ = await self._pipeline(["INCR", key], ["EXPIRE", key, str(DAY_TTL_S)])
        return int(n) <= cap

    async def snapshot(self) -> tuple[int, int]:
        spent, calls = await self._pipeline(["GET", SPENT_KEY], ["GET", _calls_key(_today())])
        return int(spent or 0), int(calls or 0)
