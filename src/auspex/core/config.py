"""Settings from the environment and `.env` (pydantic-settings). Secrets are SecretStr.

Run `python -m auspex.core.config` to check the configuration without printing secrets.
"""

from __future__ import annotations

import sys
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from auspex.core.errors import ConfigError

PositiveInt = Annotated[int, Field(gt=0)]


class LLMProvider(StrEnum):
    FAKE = "fake"
    LOCAL = "local"
    TOKENFACTORY = "tokenfactory"


class SearchProvider(StrEnum):
    LOCAL = "local"
    FIXTURES = "fixtures"
    TAVILY = "tavily"


def normalize_db_url(url: str) -> str:
    """Use the psycopg 3 driver whatever scheme the .env spells."""
    for prefix in ("postgresql+psycopg2://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    if url.startswith("postgresql+psycopg://"):
        return url
    raise ValueError("database URL must be a postgresql:// URL")


def db_url_parts(url: str) -> tuple[str, str]:
    """Return (role name, database name) from a database URL."""
    parts = urlsplit(url)
    role = parts.username or ""
    db = parts.path.lstrip("/")
    if not role or not db:
        raise ConfigError("database URL must name a user and a database")
    return role, db


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
        frozen=True,
    )

    # Always required
    database_url: SecretStr
    migration_database_url: SecretStr
    postgres_password: SecretStr
    auspex_app_db_password: SecretStr
    admin_token: SecretStr
    rmf_api_base: AnyHttpUrl

    # Providers
    llm_provider: LLMProvider = LLMProvider.FAKE
    local_llm_base_url: AnyHttpUrl | None = None
    local_llm_model: str | None = None
    nebius_api_base: AnyHttpUrl | None = None
    nebius_api_key: SecretStr | None = None
    model_triage: str | None = None
    model_reason: str | None = None
    llm_thinking: bool = False  # Nemotron reasoning mode; off: "/no_think" (faster, JSON only)
    llm_debug: bool = False  # log full prompts and answers (never on in a shared deployment)
    search_provider: SearchProvider = SearchProvider.LOCAL
    tavily_api_key: SecretStr | None = None
    # Published copy of sim/vendor_site/ (e.g. GitHub Pages); Tavily is restricted to its host.
    vendor_site_url: AnyHttpUrl | None = None
    tavily_record: bool = False  # write each live Tavily response to tests/fixtures/tavily/

    # Processes
    core_host: str = "127.0.0.1"
    core_port: PositiveInt = 8100  # 8000 is the RMF API server
    event_batch_size: PositiveInt = 200
    picture_tick_ms: PositiveInt = 2000
    ros_event_retention_s: PositiveInt = 3600

    # Optional tuning
    horizon_s: PositiveInt = 900
    tie_tolerance_s: PositiveInt = 30
    stall_threshold_s: PositiveInt = 45
    eta_slip_tolerance_s: PositiveInt = 60
    approval_timeout_s: PositiveInt = 60
    # A: observe only (safe default); B: deterministic decisions; C: B + Nemotron (M10).
    # Switchable at run time: PUT /api/v1/config (scripts/run-scenario sets it per run).
    auspex_config: Literal["A", "B", "C"] = "A"
    fast_lane_budget_ms: PositiveInt = 2000
    agent_budget_s: PositiveInt = 30
    tavily_cache_ttl_s: PositiveInt = 3600
    rmf_api_token: SecretStr | None = None
    log_level: str = "INFO"
    log_json: bool = False
    ros_setup: str | None = None
    ros_overlay_setup: str | None = None
    test_database_url: SecretStr | None = None
    test_migration_database_url: SecretStr | None = None

    @field_validator(
        "database_url",
        "migration_database_url",
        "test_database_url",
        "test_migration_database_url",
        mode="before",
    )
    @classmethod
    def _normalize_db(cls, v: object) -> object:
        if isinstance(v, str):
            return normalize_db_url(v.strip())
        return v

    @field_validator("admin_token")
    @classmethod
    def _token_length(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 24:
            raise ValueError("ADMIN_TOKEN must be at least 24 characters")
        return v

    @model_validator(mode="after")
    def _providers_complete(self) -> Self:
        missing: list[str] = []
        if self.llm_provider is LLMProvider.LOCAL:
            missing += [
                n
                for n, v in (
                    ("LOCAL_LLM_BASE_URL", self.local_llm_base_url),
                    ("LOCAL_LLM_MODEL", self.local_llm_model),
                )
                if v is None
            ]
        if self.llm_provider is LLMProvider.TOKENFACTORY:
            missing += [
                n
                for n, v in (
                    ("NEBIUS_API_BASE", self.nebius_api_base),
                    ("NEBIUS_API_KEY", self.nebius_api_key),
                    ("MODEL_TRIAGE", self.model_triage),
                    ("MODEL_REASON", self.model_reason),
                )
                if v is None
            ]
        if self.search_provider is SearchProvider.TAVILY and self.tavily_api_key is None:
            missing.append("TAVILY_API_KEY")
        if self.search_provider is SearchProvider.TAVILY and self.vendor_site_url is None:
            missing.append("VENDOR_SITE_URL")
        if missing:
            raise ValueError(
                f"providers llm={self.llm_provider.value} search={self.search_provider.value} "
                f"need: {', '.join(missing)}"
            )
        return self

    @property
    def core_url(self) -> str:
        return f"http://{self.core_host}:{self.core_port}"

    @property
    def app_role(self) -> str:
        return db_url_parts(self.database_url.get_secret_value())[0]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']).upper() or 'settings'}: {e['msg']}"
            for e in exc.errors()
        )
        raise ConfigError(f"invalid configuration: {problems}") from None


def main() -> int:
    """Print a secret-free summary of the configuration; exit 1 if it is invalid."""
    try:
        s = get_settings()
        app_role, app_db = db_url_parts(s.database_url.get_secret_value())
        mig_role, mig_db = db_url_parts(s.migration_database_url.get_secret_value())
    except ConfigError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    if app_db != mig_db:
        print(f"FAIL DATABASE_URL db={app_db} differs from MIGRATION_DATABASE_URL db={mig_db}")
        return 1
    if app_role == mig_role:
        print("FAIL DATABASE_URL must use a different role from MIGRATION_DATABASE_URL")
        return 1
    tests = "configured" if s.test_database_url and s.test_migration_database_url else "not set"
    print("OK configuration valid")
    print(f"   database={app_db} app_role={app_role} migration_role={mig_role}")
    print(f"   rmf_api_base={s.rmf_api_base}")
    print(f"   llm_provider={s.llm_provider.value} search_provider={s.search_provider.value}")
    print(f"   test database: {tests}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
