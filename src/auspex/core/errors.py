"""Explicit domain errors. Every failure Auspex raises is one of these, with a stable code."""

from __future__ import annotations

from typing import Any


class AuspexError(Exception):
    """Base class. `code` is stable and safe to log; `context` must never hold secrets."""

    code = "auspex_error"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def __str__(self) -> str:
        if not self.context:
            return f"[{self.code}] {self.message}"
        ctx = ", ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"[{self.code}] {self.message} ({ctx})"


class ConfigError(AuspexError):
    code = "config_error"


class InvalidUrn(AuspexError):
    code = "invalid_urn"


class CanonicalJsonError(AuspexError):
    code = "canonical_json_error"


class PolicyError(AuspexError):
    """Policy files are invalid, or a value has no deterministic mapping."""

    code = "policy_error"


class UnmappedPriority(PolicyError):
    code = "unmapped_priority"


class SiteError(AuspexError):
    """The site model is invalid or does not match the live navigation graphs."""

    code = "site_error"


class DatabaseError(AuspexError):
    code = "database_error"


class LedgerError(AuspexError):
    code = "ledger_error"


class LedgerChainBroken(LedgerError):
    code = "ledger_chain_broken"


class InterfaceUnverified(AuspexError):
    """Raised when code would use an interface not yet CONFIRMED on the user's system."""

    code = "interface_unverified"
