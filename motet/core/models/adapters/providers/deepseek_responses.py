"""
Motet - DeepSeek Responses Adapter

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    DeepSeek V4 adapter for ``POST /responses`` at ``https://api.deepseek.com``.
    Subclasses ``OpenAIResponsesAdapter`` and applies DeepSeek's Responses subset:

    - Credentials: ``deepseek_api_key`` → ``api_key``, default base URL
      ``https://api.deepseek.com``.
    - Stateless only: drop ``store``, ``include``, ``previous_response_id``,
      ``prompt_cache_key``. Context cache is automatic on the host.
    - ``reasoning.effort`` only (``high`` | ``max``). No summary and no
      encrypted reasoning replay.
    - Inherits parent mapping of unified ``web_search`` /
      ``deepseek.web_search`` to ``{"type": "web_search"}``. Function tools
      can share that request.
    - Other OpenAI builtins (file_search, code_interpreter, …) are ignored.

Dependencies:
    - motet.core.models.adapters.providers.openai_responses: OpenAIResponsesAdapter
    - motet.core.models.adapters.providers.deepseek_chat_completions: credential
      and reasoning-effort helpers

Usage:
    adapter = DeepSeekResponsesAdapter(
        provider="deepseek",
        adapter_name="responses",
        credentials={"deepseek_api_key": "...", "base_url": "https://api.deepseek.com"},
    )
    resp = adapter.complete(LLMRequest(messages=[...], tools=[...]))

Notes:
    - Env override: DEEPSEEK_API_BASE (wired in model.py); config key
      deepseek_api_key / MOTET_DEEPSEEK_API_KEY / DEEPSEEK_API_KEY; vault key
      "deepseek".
    - Prefer ModelSpec.base_url when set via _normalize_adapter_credentials.
    - Native search and Motet function tools are sent on the same Responses
      ``tools`` list. ``developer`` input roles are folded into ``instructions``
      (system) by the parent formatter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from ....types import LLMRequest
from ..base import CapabilityDescriptor
from .deepseek_chat_completions import (
    DEFAULT_DEEPSEEK_BASE_URL,
    _normalize_deepseek_credentials,
    _resolve_reasoning_effort,
)
from .openai_responses import OpenAIResponsesAdapter

_DEEPSEEK_UNSUPPORTED_PARAMS = frozenset(
    {
        "store",
        "include",
        "prompt_cache_key",
        "previous_response_id",
        "conversation",
    }
)


@dataclass
class DeepSeekResponsesAdapter(OpenAIResponsesAdapter):
    """Responses API adapter for DeepSeek V4 (stateless + builtin web_search)."""

    def __post_init__(self) -> None:
        self.credentials = _normalize_deepseek_credentials(self.credentials)

    def capabilities(self, *, model: str) -> CapabilityDescriptor:
        return super().capabilities(model=model).model_copy(
            update={
                "supports_stateful_sessions": False,
                "supports_image_generation": False,
                "provider_metadata": {"adapter": "deepseek_responses"},
            }
        )

    def _finalize_responses_params(
        self,
        params: Dict[str, Any],
        request: LLMRequest,
    ) -> Dict[str, Any]:
        settings = request.model_settings or {}
        if settings.get("enable_thinking") or params.get("reasoning"):
            params["reasoning"] = {"effort": _resolve_reasoning_effort(settings)}
        else:
            params.pop("reasoning", None)

        for key in _DEEPSEEK_UNSUPPORTED_PARAMS:
            params.pop(key, None)
        return params


__all__ = [
    "DeepSeekResponsesAdapter",
    "DEFAULT_DEEPSEEK_BASE_URL",
]
