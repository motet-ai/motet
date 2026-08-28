"""
Motet - xAI (Grok) Responses Adapter

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    OpenAI-compatible Responses API adapter for SpaceXAI / xAI Grok models.
    Subclasses ``OpenAIResponsesAdapter`` and applies Grok-specific policy:

    - Credentials: ``xai_api_key`` → ``api_key``, default base URL
    ``https://api.x.ai/v1`` (and ``base_url`` on the OpenAI client).
    - Always sends ``reasoning.effort`` (Grok 4.x cannot disable reasoning;
    default ``medium`` unless ``model_settings.reasoning_effort`` is set).
    xAI supports low|medium|high|xhigh but rejects ``max``, so canonical ``max``
    degrades to ``xhigh`` rather than falling back to the default.
    - Sets ``prompt_cache_key`` from ``request_context.conversation_id`` when
    present so agent-loop iterations hit the same cache shard.
    - Drops Chat Completions–era fields that xAI rejects on reasoning models
    (``presence_penalty``, ``frequency_penalty``, ``stop``).
    - Inherits parent mapping of unified ``web_search`` / ``xai.web_search``
    to ``{"type": "web_search"}``. Function tools can share that request.

    Chat Completions is intentionally not used as the primary path: xAI's
    Responses API returns reasoning summaries and is the forward-compatible
    surface for agentic tool loops.

Dependencies:
    - motet.core.models.adapters.providers.openai_responses: OpenAIResponsesAdapter

Usage:
    adapter = XAIResponsesAdapter(
        provider="xai",
        adapter_name="responses",
        credentials={"xai_api_key": "...", "base_url": "https://api.x.ai/v1"},
    )
    resp = adapter.complete(LLMRequest(messages=[...], tools=[...]))

Notes:
    - Env override: XAI_API_BASE (wired in model.py); config key xai_api_key /
      MOTET_XAI_API_KEY / XAI_API_KEY; vault key "xai".
    - Prefer ModelSpec.base_url when set via _normalize_adapter_credentials.
    - Native search and Motet function tools (``core.spawn_agents``, etc.) are
      sent on the same Responses ``tools`` list. Citations come from output
      annotations plus the top-level URL list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from ....types import LLMRequest, normalize_reasoning_effort
from ..base import CapabilityDescriptor
from ...registry import get_model_spec
from ...specs import (
    CAP_IMAGE_GENERATION,
    CAP_JSON_MODE,
    CAP_STREAM,
    CAP_SYSTEM_PROMPT,
    CAP_TOOL_USE,
    CAP_VISION,
)
from ..prompt_caching import apply_prompt_cache_key
from .openai_responses import OpenAIResponsesAdapter

DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"

# xAI rejects these on reasoning models (Chat Completions–era params).
_XAI_FORBIDDEN_PARAMS = frozenset({"presence_penalty", "frequency_penalty", "stop"})

# Verified live 2026-07-25: xAI accepts low|medium|high|xhigh and 400s on `max`
# ("Invalid reasoning effort."), so canonical `max` must degrade to `xhigh`.
_VALID_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})


def _normalize_xai_credentials(credentials: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Ensure the OpenAI client builder sees api_key + base_url for xAI."""
    creds = dict(credentials or {})
    api_key = creds.get("api_key") or creds.get("xai_api_key")
    if api_key:
        creds["api_key"] = api_key
        creds.setdefault("xai_api_key", api_key)
    if not creds.get("base_url"):
        creds["base_url"] = DEFAULT_XAI_BASE_URL
    return creds


def _resolve_reasoning_effort(settings: Dict[str, Any]) -> str:
    """
    Grok reasoning cannot be disabled; default medium for cost/latency balance.

    Canonical ``max`` is clamped down to ``xhigh`` (xAI's top rung) instead of being
    treated as unrecognized, which would have dropped it all the way to the default.
    """
    # enable_thinking True without an explicit effort still uses medium (not xAI's high default)
    return normalize_reasoning_effort(
        settings.get("reasoning_effort"),
        default="medium",
        supported=_VALID_REASONING_EFFORTS,
    )


@dataclass
class XAIResponsesAdapter(OpenAIResponsesAdapter):
    """Responses API adapter for xAI Grok with always-on reasoning policy."""

    def __post_init__(self) -> None:
        self.credentials = _normalize_xai_credentials(self.credentials)

    def capabilities(self, *, model: str) -> CapabilityDescriptor:
        spec = get_model_spec(self.provider, model)
        caps = set(spec.capabilities) if spec else set()
        return CapabilityDescriptor(
            provider=self.provider,
            model=model,
            supports_streaming=CAP_STREAM in caps,
            supports_tools=CAP_TOOL_USE in caps,
            supports_parallel_tool_calls=CAP_TOOL_USE in caps,
            supports_tool_call_id=True,
            supports_vision=CAP_VISION in caps,
            supports_audio=False,
            supports_video=False,
            supports_image_generation=CAP_IMAGE_GENERATION in caps,
            supports_json_mode=CAP_JSON_MODE in caps,
            supports_json_schema_strict=CAP_JSON_MODE in caps,
            supports_stateful_sessions=True,
            supports_builtin_tools=bool(getattr(spec, "supported_builtin_tools", None)),
            supports_system_prompt=CAP_SYSTEM_PROMPT in caps if spec else True,
            supports_reasoning=("reasoning" in caps),
            provider_metadata={"adapter": "xai_responses"},
        )

    def _finalize_responses_params(
        self,
        params: Dict[str, Any],
        request: LLMRequest,
    ) -> Dict[str, Any]:
        settings = request.model_settings or {}
        effort = _resolve_reasoning_effort(settings)
        # Always set reasoning — Grok 4.x cannot disable it; without this the
        # API defaults to "high", which is too expensive for agent implement loops.
        params["reasoning"] = {"effort": effort, "summary": "auto"}

        # ADR-0124 / ADR-0122: always set cache key affinity when conversation_id
        # is present (independent of enable_prompt_caching; automatic provider).
        apply_prompt_cache_key(
            params,
            request,
            provider=self.provider,
            require_enabled=False,
        )

        for key in _XAI_FORBIDDEN_PARAMS:
            params.pop(key, None)

        return params


__all__ = [
    "XAIResponsesAdapter",
    "DEFAULT_XAI_BASE_URL",
    "_normalize_xai_credentials",
    "_resolve_reasoning_effort",
]
