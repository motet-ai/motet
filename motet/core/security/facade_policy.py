"""
Motet - OpenAI Facade Policy

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Per-credential policy for the OpenAI-compatible API facade.

    Clients such as Cursor can only supply a base URL, an API key, and a model
    string, so facade authorization cannot rely on request headers. Policy is
    therefore bound to the service account token that authenticates the request:
    an execution mode (passthrough / hosted_tools / agent), a model allowlist,
    optional force_thinking, and optional agent_id for clients that cannot send
Motet extensions (e.g. Cursor BYOK).

    The bound mode acts as both the default and the ceiling. A request may select
    a weaker mode but never a stronger one. Model access is
    deny-by-default: an empty allowlist grants nothing.

Dependencies:
    - pydantic: policy model validation
    - motet.core.types.Principal: carries service account claims populated during auth

Usage:
    from motet.core.security.facade_policy import FacadeMode, resolve_facade_policy

    policy = resolve_facade_policy(principal, cfg)
    if not policy.allows_model("openai", "gpt-4o-mini"):
        raise PermissionError("model not allowlisted for this credential")

    effective = policy.resolve_mode(requested=FacadeMode.PASSTHROUGH)

Notes:
    - Allowlist entries are "provider/model" ids; "provider/*" and "*" are supported
    - Policy claims are attached to Principal by motet.core.security.auth on sa_* auth
    - Config supplies fallbacks when a token carries no explicit policy
    - force_thinking still requires CAP_REASONING on the resolved model
    - agent_id is a default for agent mode when the request omits motet_agent_id
"""

from __future__ import annotations

from enum import Enum
from typing import Any, List, Optional, Sequence

from pydantic import BaseModel, Field


class FacadeMode(str, Enum):
    """Execution depth for an OpenAI-compatible facade request."""
    PASSTHROUGH = "passthrough"
    HOSTED_TOOLS = "hosted_tools"
    AGENT = "agent"


FACADE_MODE_VALUES = frozenset(mode.value for mode in FacadeMode)

# Escalation order. A request may select a mode at or below the credential's
# bound mode, never above it.
_MODE_RANK = {
    FacadeMode.PASSTHROUGH: 0,
    FacadeMode.HOSTED_TOOLS: 1,
    FacadeMode.AGENT: 2,
}


def parse_facade_mode(value: Any) -> Optional[FacadeMode]:
    """Parse a mode string, returning None when absent or unrecognized."""
    if not value:
        return None
    try:
        return FacadeMode(str(value).strip().lower())
    except ValueError:
        return None


def _parse_optional_bool(value: Any) -> Optional[bool]:
    """Parse an optional bool claim; None means 'unset / use config default'."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("", "none", "null"):
        return None
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return None


class FacadePolicy(BaseModel):
    """Resolved facade policy for one authenticated credential."""

    mode: FacadeMode = Field(
        default=FacadeMode.PASSTHROUGH,
        description="Bound execution mode; also the ceiling for request-level selection",
    )
    allowed_models: List[str] = Field(
        default_factory=list,
        description="Allowlist entries as 'provider/model', 'provider/*', or '*'",
    )
    force_thinking: bool = Field(
        default=False,
        description=(
            "When true, enable Motet thinking for CAP_REASONING models even if the "
            "client omits reasoning opt-in (Cursor BYOK often sends none)"
        ),
    )
    force_thinking_effort: str = Field(
        default="medium",
        description="Default reasoning effort used when force_thinking applies without client effort",
    )
    agent_id: Optional[str] = Field(
        default=None,
        description=(
            "Default Motet agent id in agent mode when the request omits motet_agent_id "
            "(e.g. cursor.backend for Cursor BYOK)"
        ),
    )
    mode_source: str = Field(
        default="default",
        description="Where the bound mode came from: service_account or config_default",
    )
    allowlist_source: str = Field(
        default="config_default",
        description="Where the allowlist came from: service_account or config_default",
    )
    force_thinking_source: str = Field(
        default="config_default",
        description="Where force_thinking came from: service_account or config_default",
    )
    agent_id_source: str = Field(
        default="config_default",
        description="Where agent_id came from: service_account or config_default",
    )

    def allows_model(self, provider: str, registry_key: str) -> bool:
        """Whether this credential may use a registry model.

        Deny-by-default: an empty allowlist grants no models at all, so an
        operator who enables the facade without configuring policy cannot
        accidentally expose every vault-backed provider (ADR-0125 §11a).
        """
        if not self.allowed_models:
            return False
        candidate = f"{provider}/{registry_key}".lower()
        for entry in self.allowed_models:
            normalized = str(entry).strip().lower()
            if not normalized:
                continue
            if normalized in ("*", "*/*"):
                return True
            if normalized == candidate:
                return True
            if normalized.endswith("/*") and candidate.startswith(normalized[:-1]):
                return True
        return False

    def resolve_mode(self, requested: Optional[FacadeMode]) -> FacadeMode:
        """Clamp a requested mode to the credential's ceiling."""
        if requested is None:
            return self.mode
        if _MODE_RANK[requested] > _MODE_RANK[self.mode]:
            return self.mode
        return requested

    def permits_mode(self, requested: FacadeMode) -> bool:
        """Whether the credential may run at *requested* depth."""
        return _MODE_RANK[requested] <= _MODE_RANK[self.mode]


def _split_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


def resolve_facade_policy(principal: Any, cfg: Any) -> FacadePolicy:
    """Build the effective policy for *principal*.

    Service account claims win over configuration defaults. Non-service-account
    credentials (JWT users) fall back entirely to configuration, which keeps the
    facade usable for interactive testing without minting a token while still
    honoring deny-by-default model access.
    """
    claims = getattr(principal, "claims", None) or {}

    bound_mode = parse_facade_mode(claims.get("facade_mode"))
    mode_source = "service_account"
    if bound_mode is None:
        bound_mode = parse_facade_mode(getattr(cfg, "openai_compat_default_mode", None))
        mode_source = "config_default"
    if bound_mode is None:
        bound_mode = FacadeMode.PASSTHROUGH
        mode_source = "fallback"

    claim_models: Sequence[Any] = claims.get("allowed_models") or []
    allowed_models = [str(item) for item in claim_models if str(item).strip()]
    allowlist_source = "service_account"
    if not allowed_models:
        allowed_models = _split_csv(getattr(cfg, "openai_compat_default_allowed_models", None))
        allowlist_source = "config_default"

    claim_force = _parse_optional_bool(claims.get("force_thinking"))
    force_thinking_source = "service_account"
    if claim_force is None:
        force_thinking = bool(getattr(cfg, "openai_compat_force_thinking", False))
        force_thinking_source = "config_default"
    else:
        force_thinking = claim_force

    claim_effort = claims.get("force_thinking_effort")
    if isinstance(claim_effort, str) and claim_effort.strip():
        force_thinking_effort = claim_effort.strip()
    else:
        cfg_effort = getattr(cfg, "openai_compat_force_thinking_effort", None)
        force_thinking_effort = (
            str(cfg_effort).strip() if cfg_effort is not None and str(cfg_effort).strip() else "medium"
        )

    claim_agent = claims.get("agent_id")
    agent_id_source = "service_account"
    if isinstance(claim_agent, str) and claim_agent.strip():
        agent_id: Optional[str] = claim_agent.strip()
    else:
        cfg_agent = getattr(cfg, "openai_compat_default_agent_id", None)
        agent_id = str(cfg_agent).strip() if cfg_agent is not None and str(cfg_agent).strip() else None
        agent_id_source = "config_default"

    return FacadePolicy(
        mode=bound_mode,
        allowed_models=allowed_models,
        force_thinking=force_thinking,
        force_thinking_effort=force_thinking_effort,
        agent_id=agent_id,
        mode_source=mode_source,
        allowlist_source=allowlist_source,
        force_thinking_source=force_thinking_source,
        agent_id_source=agent_id_source,
    )


__all__ = [
    "FACADE_MODE_VALUES",
    "FacadeMode",
    "FacadePolicy",
    "parse_facade_mode",
    "resolve_facade_policy",
]
