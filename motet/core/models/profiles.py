"""
Motet - Model Profiles

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Redis-backed model profiles for per-tenant and per-model routing/configuration overrides.

    ModelSpec (motet.core.models.specs) is the authoritative source for *capability*:
    - which adapters a model can support (supported_adapters)
    - which provider built-in tools a model can support (supported_builtin_tools)

    A ModelProfile is the authoritative source for *policy/config*:
    - which adapter to prefer for a given tenant/model (e.g., force chat_completions during rollout)
    - whether provider-native built-in tools are enabled and which are allowed/denied
    - per-model default model_settings (temperature, max_tokens, etc.), without relying on env vars

Dependencies:
    - pydantic: Structured profile models
    - motet.core.distributed.redis_manager: Centralized Redis storage helpers

Usage:
    from motet.core.models.profiles import (
        load_model_profile_sync, store_model_profile_sync, resolve_route_override
    )

    profile = load_model_profile_sync(tenant_id="t1", profile_name="default")
    override = resolve_route_override(profile, provider="openai", model_name="gpt-4o-mini")
    if override and override.adapter:
        print("Forced adapter:", override.adapter)

Notes:
    - Profiles are optional. If missing, callers should fall back to ModelSpec and env defaults.
    - Stored as ``{tenant}:model_profiles:{name}`` (issue #218). Leftover
      ``imf:model_profiles:{tenant}:{name}`` is not dual-read. JSON-string format for inspection.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ..distributed.redis_manager import (
    store_structured_data,
    store_structured_data_sync,
)
from ..distributed.tenant_keys import (
    retrieve_structured_data_tenant,
    retrieve_structured_data_tenant_sync,
    tenant_key,
)


def _logical_profile_key(*, profile_name: str) -> str:
    return f"model_profiles:{profile_name}"


def _profile_key(*, tenant_id: str, profile_name: str) -> str:
    return tenant_key(tenant_id, _logical_profile_key(profile_name=profile_name))


class ModelRouteOverride(BaseModel):
    """Per-provider or per-model overrides applied by a ModelProfile."""

    adapter: Optional[str] = Field(
        default=None,
        description="Adapter name override (e.g., 'responses', 'chat_completions', 'messages').",
        examples=["chat_completions"],
    )

    tools_enabled: Optional[bool] = Field(
        default=None,
        description="Enable provider-native built-in tools for this scope (policy).",
        examples=[True],
    )
    tool_allowlist: Optional[List[str]] = Field(
        default=None,
        description="Allowlisted built-in tool names (canonical namespaced names).",
        examples=[["openai.web_search"]],
    )
    tool_denylist: Optional[List[str]] = Field(
        default=None,
        description="Denied built-in tool names (canonical namespaced names).",
        examples=[[]],
    )

    model_settings: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Default model_settings applied for this scope (merged below request settings).",
        examples=[{"temperature": 0.2}],
    )


class ModelProfile(BaseModel):
    """Tenant-scoped model profile used for routing and policy overrides."""

    name: str = Field(..., description="Profile name", examples=["default"])
    tenant_id: str = Field(..., description="Tenant identifier", examples=["default"])

    # Provider-wide defaults (e.g., for all OpenAI models in a tenant)
    provider_defaults: Dict[str, ModelRouteOverride] = Field(
        default_factory=dict,
        description="Per-provider default overrides.",
    )

    # Per-model overrides: provider -> model_name -> override
    model_overrides: Dict[str, Dict[str, ModelRouteOverride]] = Field(
        default_factory=dict,
        description="Per-provider per-model overrides.",
    )

    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Last update timestamp (UTC ISO8601).",
    )


def resolve_route_override(
    profile: Optional[ModelProfile],
    *,
    provider: str,
    model_name: str,
) -> Optional[ModelRouteOverride]:
    """
    Resolve the effective override for provider+model_name.

    Precedence:
        provider_defaults[provider] -> model_overrides[provider][model_name]
        (later values override earlier if they are not None)
    """
    if profile is None:
        return None

    base = profile.provider_defaults.get(provider)
    model_map = profile.model_overrides.get(provider) or {}
    specific = model_map.get(model_name)

    if base is None and specific is None:
        return None
    if base is None:
        return specific
    if specific is None:
        return base

    # Merge by "specific overrides non-None fields from base"
    merged = base.model_copy(deep=True)
    specific_dict = specific.model_dump(exclude_unset=True)
    for k, v in specific_dict.items():
        if v is not None:
            setattr(merged, k, v)
    return merged


async def load_model_profile(*, tenant_id: str, profile_name: str) -> Optional[ModelProfile]:
    data = await retrieve_structured_data_tenant(
        "model_profiles",
        tenant_id,
        _logical_profile_key(profile_name=profile_name),
        format_type="json_string",
    )
    if not data:
        return None
    return ModelProfile.model_validate(data)


def load_model_profile_sync(*, tenant_id: str, profile_name: str) -> Optional[ModelProfile]:
    data = retrieve_structured_data_tenant_sync(
        "model_profiles",
        tenant_id,
        _logical_profile_key(profile_name=profile_name),
        format_type="json_string",
    )
    if not data:
        return None
    return ModelProfile.model_validate(data)


async def store_model_profile(*, profile: ModelProfile) -> None:
    await store_structured_data(
        "model_profiles",
        _profile_key(tenant_id=profile.tenant_id, profile_name=profile.name),
        profile.model_dump(),
        format_type="json_string",
    )


def store_model_profile_sync(*, profile: ModelProfile) -> None:
    store_structured_data_sync(
        "model_profiles",
        _profile_key(tenant_id=profile.tenant_id, profile_name=profile.name),
        profile.model_dump(),
        format_type="json_string",
    )


__all__ = [
    "ModelProfile",
    "ModelRouteOverride",
    "load_model_profile",
    "load_model_profile_sync",
    "resolve_route_override",
    "store_model_profile",
    "store_model_profile_sync",
]

