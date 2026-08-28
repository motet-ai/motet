"""
Motet - Meta (Muse Spark) Responses Adapter

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    OpenAI-compatible Responses API adapter for Meta Model API (Muse Spark).
    Subclasses ``OpenAIResponsesAdapter`` and applies Meta-specific policy:

    - Credentials: ``meta_api_key`` / ``MODEL_API_KEY`` → ``api_key``, default
      base URL ``https://api.meta.ai/v1``.
    - Always sends ``reasoning.effort`` (Muse Spark rejects ``none`` with HTTP
      400). Thinking-off requests use ``minimal``; thinking-on maps the
      canonical ladder onto ``low|medium|high|xhigh`` (``max`` → ``xhigh``).
    - Inherits parent ``store=false`` + ``include=["reasoning.encrypted_content"]``
      so tool-loop turns replay encrypted reasoning without server retention.
    - Inherits parent mapping of unified ``web_search`` / ``meta.web_search``
      to ``{"type": "web_search"}``. Function tools can share that request.

    Chat Completions is not registered: Meta redacts ``reasoning_content`` for
    external keys on that endpoint, so agent loops would lose CoT continuity.

Dependencies:
    - motet.core.models.adapters.providers.openai_responses: OpenAIResponsesAdapter
    - motet.core.types: LLMRequest, normalize_reasoning_effort

Usage:
    adapter = MetaResponsesAdapter(
        provider="meta",
        adapter_name="responses",
        credentials={"meta_api_key": "...", "base_url": "https://api.meta.ai/v1"},
    )
    resp = adapter.complete(LLMRequest(messages=[...], tools=[...]))

Notes:
    - Env override: META_API_BASE (wired in model.py); config key meta_api_key /
      MOTET_META_API_KEY / MODEL_API_KEY / META_API_KEY; vault key "meta".
    - Prefer ModelSpec.base_url when set via _normalize_adapter_credentials.
    - Native search and Motet function tools are sent on the same Responses
      ``tools`` list. Citations come from output annotations plus search items.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from ....types import LLMRequest, normalize_reasoning_effort
from .openai_responses import OpenAIResponsesAdapter

DEFAULT_META_BASE_URL = "https://api.meta.ai/v1"

# Muse Spark rejects `none` (400). `minimal` is Meta-only (not on Motet's ladder)
# and is used when enable_thinking is false. `max` is not accepted; clamp to xhigh.
_VALID_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})


def _normalize_meta_credentials(credentials: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Ensure the OpenAI client builder sees api_key + base_url for Meta Model API."""
    creds = dict(credentials or {})
    api_key = (
        creds.get("api_key")
        or creds.get("meta_api_key")
        or creds.get("model_api_key")
    )
    if api_key:
        creds["api_key"] = api_key
        creds.setdefault("meta_api_key", api_key)
    if not creds.get("base_url"):
        creds["base_url"] = DEFAULT_META_BASE_URL
    return creds


def _resolve_reasoning_effort(settings: Dict[str, Any]) -> str:
    """
    Muse Spark always reasons; ``none`` is a 400.

    When thinking is off, send Meta's shortest rung (``minimal``). When on,
    map the canonical ladder onto Meta's subset (``max`` → ``xhigh``).
    """
    if not settings.get("enable_thinking"):
        return "minimal"
    return normalize_reasoning_effort(
        settings.get("reasoning_effort"),
        default="medium",
        supported=_VALID_REASONING_EFFORTS,
    )


@dataclass
class MetaResponsesAdapter(OpenAIResponsesAdapter):
    """Responses API adapter for Meta Muse Spark (always-on reasoning + web_search)."""

    def __post_init__(self) -> None:
        self.credentials = _normalize_meta_credentials(self.credentials)

    def capabilities(self, *, model: str):
        return super().capabilities(model=model).model_copy(
            update={"provider_metadata": {"adapter": "meta_responses"}},
        )

    def _finalize_responses_params(
        self,
        params: Dict[str, Any],
        request: LLMRequest,
    ) -> Dict[str, Any]:
        settings = request.model_settings or {}
        params["reasoning"] = {
            "effort": _resolve_reasoning_effort(settings),
            "summary": "auto",
        }
        # Parent: store=false, include encrypted reasoning, prompt_cache_key.
        return super()._finalize_responses_params(params, request)


__all__ = [
    "MetaResponsesAdapter",
    "DEFAULT_META_BASE_URL",
]
