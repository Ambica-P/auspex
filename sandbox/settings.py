"""Sandbox settings (spec 13, "Sandbox only"). Environment variables only; the Token Factory
key is a dedicated one for the sandbox, never sent to the browser."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SandboxSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True)

    sandbox_enabled: bool = False  # kill switch
    sandbox_daily_cap: int = Field(default=300, ge=0)  # model calls per UTC day
    sandbox_rate_per_ip_per_min: int = Field(default=6, ge=1)
    sandbox_max_input_chars: int = Field(default=200, ge=20, le=500)
    sandbox_allowed_origin: str | None = None  # the replay site's origin (CORS)
    sandbox_llm: Literal["tokenfactory", "fake"] = "tokenfactory"
    sandbox_cache_size: int = Field(default=512, ge=0)
    # Spending cap for the sandbox's model calls, in US dollars: lifetime total in Upstash when
    # configured, else for the life of the container (0 disables the cap). Prices per million
    # tokens come from the Nebius console.
    sandbox_budget_usd: float = Field(default=0.02, ge=0)
    sandbox_price_in_per_mtok: float | None = Field(default=None, gt=0)
    sandbox_price_out_per_mtok: float | None = Field(default=None, gt=0)
    # Persistent usage counters (Upstash Redis REST). Unset: counters live in memory.
    upstash_redis_rest_url: AnyHttpUrl | None = None
    upstash_redis_rest_token: SecretStr | None = None
    nebius_api_base: AnyHttpUrl | None = None
    nebius_api_key: SecretStr | None = None
    model_triage: str | None = None
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _tokenfactory_complete(self) -> Self:
        if bool(self.upstash_redis_rest_url) != bool(self.upstash_redis_rest_token):
            raise ValueError(
                "set both UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN, or neither"
            )
        if self.sandbox_llm == "tokenfactory" and not (
            self.nebius_api_base and self.nebius_api_key and self.model_triage
        ):
            raise ValueError(
                "SANDBOX_LLM=tokenfactory needs NEBIUS_API_BASE, NEBIUS_API_KEY, MODEL_TRIAGE"
            )
        if (
            self.sandbox_llm == "tokenfactory"
            and self.sandbox_budget_usd > 0
            and not (self.sandbox_price_in_per_mtok and self.sandbox_price_out_per_mtok)
        ):
            raise ValueError(
                "SANDBOX_BUDGET_USD needs SANDBOX_PRICE_IN_PER_MTOK and SANDBOX_PRICE_OUT_PER_MTOK"
                " (the model's prices per million tokens, from the Nebius console)"
            )
        return self
