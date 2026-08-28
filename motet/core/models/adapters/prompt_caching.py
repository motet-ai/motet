"""
Motet - Provider Prompt Caching Helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Shared helpers for Motet's provider prompt-caching policy.
    Adapters use these to:
    - Decide whether ``model_settings.enable_prompt_caching`` should take effect
    for a resolved ``ModelSpec`` (capability-gated; never fails the request).
    - Extract a stable ``prompt_cache_key`` from ``request_context.conversation_id``.
    - Inject that key into OpenAI-compatible request params when appropriate.

Dependencies:
    - motet.core.types.LLMRequest: request settings + request_context
    - motet.core.models.registry.get_model_spec: capability lookup
    - motet.core.models.specs.CAP_PROMPT_CACHING: capability constant

Usage:
    from motet.core.models.adapters.prompt_caching import (
        prompt_caching_enabled,
        conversation_prompt_cache_key,
        apply_prompt_cache_key,
    )

    if prompt_caching_enabled(request, provider="openai"):
        apply_prompt_cache_key(params, request)

Notes:
    - Absence of CAP_PROMPT_CACHING is a no-op (never raises).
    - ``enable_prompt_caching=False`` is not a universal "zero cache hits" switch
      on automatic providers; adapters only skip Motet's optimize-hits wiring.
    - xAI may still set ``prompt_cache_key`` independently (always-on affinity).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ...types import LLMRequest
from ..registry import get_model_spec
from ..specs import CAP_PROMPT_CACHING, ModelSpec


def resolve_model_name(request: LLMRequest) -> str:
    """Return the model name from ``model_settings`` (empty string if unset)."""
    settings = request.model_settings or {}
    name = settings.get("model_name") or settings.get("model") or ""
    return str(name).strip() if name is not None else ""


def prompt_caching_enabled(
    request: LLMRequest,
    *,
    provider: str,
    spec: Optional[ModelSpec] = None,
) -> bool:
    """
    True when the caller opted in and the model advertises CAP_PROMPT_CACHING.

    Missing specs / missing capability → False (policy no-op; never fail).
    """
    settings = request.model_settings or {}
    if not bool(settings.get("enable_prompt_caching", False)):
        return False
    resolved = spec if spec is not None else get_model_spec(provider, resolve_model_name(request))
    if resolved is None:
        return False
    return CAP_PROMPT_CACHING in (resolved.capabilities or set())


def conversation_prompt_cache_key(request: LLMRequest) -> Optional[str]:
    """Stable cache-routing key from ``request_context.conversation_id``, if present."""
    ctx = request.request_context
    if ctx is None:
        return None
    conversation_id = getattr(ctx, "conversation_id", None)
    if isinstance(conversation_id, str) and conversation_id.strip():
        return conversation_id.strip()
    return None


def apply_prompt_cache_key(
    params: Dict[str, Any],
    request: LLMRequest,
    *,
    provider: str,
    spec: Optional[ModelSpec] = None,
    require_enabled: bool = True,
) -> Dict[str, Any]:
    """
    Set ``prompt_cache_key`` on provider params when a conversation id is available.

    Args:
        params: Mutable provider request params.
        request: Canonical LLM request.
        provider: Provider id for capability lookup.
        spec: Optional pre-resolved ModelSpec.
        require_enabled: When True (default), require enable_prompt_caching + CAP.
            When False, set the key whenever conversation_id is present (xAI-style).
    """
    if require_enabled and not prompt_caching_enabled(request, provider=provider, spec=spec):
        return params
    cache_key = conversation_prompt_cache_key(request)
    if cache_key:
        params["prompt_cache_key"] = cache_key
    return params


__all__ = [
    "resolve_model_name",
    "prompt_caching_enabled",
    "conversation_prompt_cache_key",
    "apply_prompt_cache_key",
]
