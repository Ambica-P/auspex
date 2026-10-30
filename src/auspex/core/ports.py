"""Provider ports. Core logic depends on these Protocols only; adapters live elsewhere.

- LLMPort: Nemotron via Token Factory (`tokenfactory`), a local OpenAI-compatible server
  (`local`), or a deterministic fake (`fake`). Implemented in milestone 10.
- SearchPort: Tavily (`tavily`), recorded fixtures (`fixtures`), or the local mock vendor
  site (`local`). Implemented in milestone 9.

Everything a provider returns is untrusted until validated against a domain model.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import Field

from auspex.core.models import StrictModel


class ModelRole(StrEnum):
    TRIAGE = "triage"  # Nemotron Nano: interpretation, extraction, incident reports
    REASON = "reason"  # Nemotron Super: uncertainty, trade-offs, explanations


class ChatMessage(StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: Annotated[str, Field(max_length=200_000)]
    tool_call_id: str | None = None


class LLMRequest(StrictModel):
    role: ModelRole
    messages: list[ChatMessage] = Field(min_length=1)
    json_schema: dict[str, Any] | None = None  # structured output when supported
    max_tokens: int = Field(default=2048, gt=0, le=32_768)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    timeout_s: float = Field(default=30.0, gt=0.0, le=300.0)


class LLMResponse(StrictModel):
    model_id: str
    content: str  # raw text; the caller validates it against a domain model
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)


@runtime_checkable
class LLMPort(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse: ...


class SearchQuery(StrictModel):
    query: Annotated[str, Field(min_length=1, max_length=400)]
    include_domains: list[str] = Field(min_length=1, max_length=10)  # vendor advisory domains
    max_results: int = Field(default=3, ge=1, le=10)
    timeout_s: float = Field(default=10.0, gt=0.0, le=60.0)


class SearchHit(StrictModel):
    url: Annotated[str, Field(max_length=2000)]
    title: Annotated[str, Field(max_length=500)]
    content: Annotated[str, Field(max_length=20_000)]  # untrusted


@runtime_checkable
class SearchPort(Protocol):
    async def search(self, query: SearchQuery) -> list[SearchHit]: ...
