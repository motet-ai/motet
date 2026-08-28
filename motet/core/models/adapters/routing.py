"""
Motet - Adapter Routing Helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Centralized adapter selection logic that bridges the model registry (ModelSpec) and the
    adapter registry (translation implementations).

    This module implements 's routing rule:
        request override -> ModelSpec/profile -> environment default

    It is intentionally "pure" (no network calls) and safe to unit test.

Dependencies:
    - motet.core.models.registry: get_model_spec
    - motet.core.models.adapters.registry: adapter_registry
    - motet.core.config: Config (env-based defaults)

Usage:
    from motet.core.models.adapters.routing import select_adapter_name
    adapter_name, source = select_adapter_name(provider="openai", model_name="gpt-4o-mini", model_settings={...})

Notes:
    - This does not implement per-tenant/per-motet profiles yet; it is a stepping stone toward them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from ..registry import get_model_spec
from .registry import adapter_registry


@dataclass(frozen=True)
class AdapterSelection:
    provider: str
    model_name: str
    adapter_name: Optional[str]
    source: str  # request_override | model_spec | env_default | none


def _get_env_default_adapter(provider: str) -> Optional[str]:
    """
    Environment defaults (local/dev compatibility).

    These are only used if ModelSpec does not specify an adapter and the request does not override.
    """
    if provider == "openai":
        from ...config import Config

        cfg = Config()
        api_mode = getattr(cfg, "openai_api_mode", "chat_completions")
        return "responses" if api_mode == "responses" else "chat_completions"
    if provider == "anthropic":
        # Anthropic uses the Messages API; we only expose one adapter today.
        return "messages"
    if provider == "gemini":
        return "generate_content"
    if provider == "xai":
        return "responses"
    if provider == "meta":
        return "responses"
    if provider == "mock":
        # Test/dev provider: only one adapter. Needed when MOTET_MODEL_PROVIDER=mock
        # but model_name still defaults to an OpenAI id (no ModelSpec for that pair).
        return "mock"
    if provider == "local":
        return "local"
    return None


def select_adapter_name(
    *,
    provider: str,
    model_name: str,
    model_settings: Optional[Dict[str, Any]] = None,
    profile_override: Optional[Dict[str, Any]] = None,
) -> AdapterSelection:
    """
    Select an adapter name for a provider+model, preferring request overrides and ModelSpec.

    Returns:
        AdapterSelection with adapter_name possibly None if no adapter is selected/supported.
    """
    settings = model_settings or {}

    # 1) Request override (highest priority)
    override = settings.get("adapter") or settings.get("adapter_name")
    if isinstance(override, str) and override.strip():
        name = override.strip()
        if adapter_registry.supports(provider, name):
            return AdapterSelection(provider=provider, model_name=model_name, adapter_name=name, source="request_override")
        # If explicitly requested but unsupported, fail closed to None; caller can decide fallback behavior.
        return AdapterSelection(provider=provider, model_name=model_name, adapter_name=None, source="request_override")

    # 2) ModelProfile override (tenant/model policy) — only use when compatible with model spec
    prof_adapter = (profile_override or {}).get("adapter")
    if isinstance(prof_adapter, str) and prof_adapter.strip():
        name = prof_adapter.strip()
        spec = get_model_spec(provider, model_name)
        supported = getattr(spec, "supported_adapters", None) if spec is not None else None
        if (supported is None or name in supported) and adapter_registry.supports(provider, name):
            return AdapterSelection(provider=provider, model_name=model_name, adapter_name=name, source="model_profile")
        # Profile adapter not allowed by this model (e.g. alias only supports chat_completions);
        # fall through to ModelSpec so spec.default_adapter is used.

    # 3) ModelSpec
    spec = get_model_spec(provider, model_name)
    if spec is not None:
        supported = getattr(spec, "supported_adapters", None)
        preferred = getattr(spec, "default_adapter", None)
        if isinstance(preferred, str) and preferred:
            if (supported is None or preferred in supported) and adapter_registry.supports(provider, preferred):
                return AdapterSelection(provider=provider, model_name=model_name, adapter_name=preferred, source="model_spec")

        fallbacks = getattr(spec, "fallback_adapters", None) or []
        if isinstance(fallbacks, list):
            for name in fallbacks:
                if isinstance(name, str) and (supported is None or name in supported) and adapter_registry.supports(provider, name):
                    return AdapterSelection(provider=provider, model_name=model_name, adapter_name=name, source="model_spec")

    # 4) Env default
    env_default = _get_env_default_adapter(provider)
    if env_default and adapter_registry.supports(provider, env_default):
        # Env defaults must still respect per-model adapter allowlist when present.
        supported = None
        spec = get_model_spec(provider, model_name)
        if spec is not None:
            supported = getattr(spec, "supported_adapters", None)
        if supported is None or env_default in supported:
            return AdapterSelection(provider=provider, model_name=model_name, adapter_name=env_default, source="env_default")

    return AdapterSelection(provider=provider, model_name=model_name, adapter_name=None, source="none")


__all__ = ["AdapterSelection", "select_adapter_name"]

