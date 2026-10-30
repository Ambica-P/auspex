"""LLM clients behind LLMPort (spec 6.3, 13): Token Factory, a local server, or the fake.

- `tokenfactory`: Nebius Token Factory's OpenAI-compatible API through the `openai` SDK;
  NEBIUS_API_BASE, NEBIUS_API_KEY, MODEL_TRIAGE (Nemotron Nano), MODEL_REASON (Nemotron
  Super), all copied from the Nebius console (never hard-coded).
- `local`: any OpenAI-compatible server (LOCAL_LLM_BASE_URL, LOCAL_LLM_MODEL for both roles).
- `fake`: deterministic stand-in (agent/fake.py), for development and tests without paid calls.

Structured output: JSON schema (`response_format` json_schema) first; if the server rejects
that, JSON mode (json_object) from then on, logged once. Thinking is turned off by default
with Nemotron's `/no_think` system switch (LLM_THINKING=on to allow it); any <think> block a
model returns is dropped before parsing: only the JSON answer is used, never hidden reasoning.
Timeouts per call; bounded retries on connection errors and 5xx / rate limits.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import openai
from openai import AsyncOpenAI

from auspex.agent.fake import FakeLLM
from auspex.core.config import LLMProvider, Settings
from auspex.core.errors import AuspexError
from auspex.core.logging import get_logger
from auspex.core.ports import LLMPort, LLMRequest, LLMResponse, ModelRole

log = get_logger(__name__)

RETRIES = 2
_THINK = re.compile(r"(?is)<think>.*?</think>")


class LLMUnavailable(AuspexError):
    code = "llm_unavailable"


def strip_thinking(text: str) -> str:
    text = _THINK.sub("", text)
    if "</think>" in text:  # an unopened block: keep what follows it
        text = text.split("</think>", 1)[1]
    return text.strip()


class OpenAICompatLLM:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        models: dict[ModelRole, str],
        thinking: bool,
        provider: str,
    ) -> None:
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key, max_retries=0)
        self.models = models
        self.thinking = thinking
        self.provider = provider
        self.json_schema_ok = True
        # Nemotron 3 can reason before answering; with a token limit, a long reasoning pass
        # truncates or empties the JSON answer (live S1, 2026-10-29). Besides "/no_think", ask
        # the chat template to skip reasoning; dropped if the endpoint refuses the parameter.
        self.template_kwargs_ok = not thinking

    async def complete(self, request: LLMRequest) -> LLMResponse:
        model = self.models[request.role]
        messages: list[dict[str, Any]] = [
            {"role": m.role, "content": m.content} for m in request.messages
        ]
        if not self.thinking and messages and messages[0]["role"] == "system":
            messages[0] = {**messages[0], "content": "/no_think\n" + messages[0]["content"]}
        t0 = time.monotonic()
        last = ""
        for attempt in range(1, RETRIES + 2):
            fmt: dict[str, Any]
            if request.json_schema is not None and self.json_schema_ok:
                fmt = {
                    "type": "json_schema",
                    "json_schema": {"name": "answer", "schema": request.json_schema},
                }
            else:
                fmt = {"type": "json_object"}
            extra: dict[str, Any] = (
                {"chat_template_kwargs": {"enable_thinking": False}}
                if self.template_kwargs_ok
                else {}
            )
            try:
                completions: Any = self.client.chat.completions
                resp = await completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=request.max_tokens,
                    temperature=request.temperature,
                    response_format=fmt,
                    timeout=request.timeout_s,
                    extra_body=extra or None,
                )
            except openai.BadRequestError as exc:
                detail = (str(exc) + str(getattr(exc, "body", ""))).lower()
                if extra and ("chat_template_kwargs" in detail or "enable_thinking" in detail):
                    self.template_kwargs_ok = False
                    log.warning("llm.template_kwargs_unsupported", model=model)
                    continue
                if fmt["type"] == "json_schema" and "response_format" in detail:
                    self.json_schema_ok = False
                    log.warning("llm.json_schema_unsupported", model=model, using="json_object")
                    continue
                raise LLMUnavailable("request refused", model=model, error=str(exc)[:200]) from None
            except (
                openai.AuthenticationError,
                openai.PermissionDeniedError,
                openai.NotFoundError,
            ) as exc:
                raise LLMUnavailable(
                    "provider refused (key, permission or model id)",
                    model=model,
                    error=type(exc).__name__,
                ) from None
            except (
                openai.APITimeoutError,
                openai.APIConnectionError,
                openai.RateLimitError,
                openai.InternalServerError,
            ) as exc:
                last = type(exc).__name__
                log.warning("llm.retry", model=model, attempt=attempt, error=last)
                await asyncio.sleep(attempt)
                continue
            choice = resp.choices[0] if resp.choices else None
            content = strip_thinking(choice.message.content or "") if choice else ""
            if choice is not None and choice.finish_reason == "length":
                log.warning(
                    "llm.truncated",
                    model=model,
                    max_tokens=request.max_tokens,
                    completion_tokens=resp.usage.completion_tokens if resp.usage else None,
                )
            usage = resp.usage
            return LLMResponse(
                model_id=str(resp.model or model),
                content=content,
                prompt_tokens=int(usage.prompt_tokens) if usage else 0,
                completion_tokens=int(usage.completion_tokens) if usage else 0,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        raise LLMUnavailable("no answer after retries", model=model, error=last)


def make_llm(settings: Settings) -> LLMPort:
    if settings.llm_provider is LLMProvider.TOKENFACTORY:
        assert settings.nebius_api_base and settings.nebius_api_key  # enforced by Settings
        assert settings.model_triage and settings.model_reason
        return OpenAICompatLLM(
            base_url=str(settings.nebius_api_base),
            api_key=settings.nebius_api_key.get_secret_value(),
            models={
                ModelRole.TRIAGE: settings.model_triage,
                ModelRole.REASON: settings.model_reason,
            },
            thinking=settings.llm_thinking,
            provider="tokenfactory",
        )
    if settings.llm_provider is LLMProvider.LOCAL:
        assert settings.local_llm_base_url and settings.local_llm_model
        model = settings.local_llm_model
        return OpenAICompatLLM(
            base_url=str(settings.local_llm_base_url),
            api_key="local",
            models={ModelRole.TRIAGE: model, ModelRole.REASON: model},
            thinking=settings.llm_thinking,
            provider="local",
        )
    return FakeLLM()
