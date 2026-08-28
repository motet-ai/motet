"""
Motet - Provider Adapter Interfaces

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Defines the provider-agnostic adapter surface for model inference as specified in.
    Adapters translate between the canonical LLM protocol (LLMRequest/LLMResponse and streaming events)
    and provider-specific wire formats (OpenAI Responses, Anthropic Messages, Gemini, Moonshot,
    DeepSeek, xAI, Meta Muse Spark, local runtimes).

Dependencies:
    - motet.core.types: Canonical protocol models (LLMRequest, LLMResponse, LLMStreamEvent)
    - typing: Protocols and type hints
    - pydantic: Capability descriptor validation

Usage:
    from motet.core.models.adapters import (
        LLMProviderAdapter, CapabilityDescriptor,
        adapter_registry
    )

    adapter = adapter_registry.build("openai", "responses", credentials={...})
    resp = adapter.complete(LLMRequest(messages=[...]))

Notes:
    - Adapters MUST be translation-only. Orchestration policy (tool discovery, retries, repair)
      belongs outside the adapter boundary.
"""

from .base import CapabilityDescriptor, LLMProviderAdapter
from .registry import AdapterRegistry, adapter_registry

# Provider adapter registrations (translation-only)
# NOTE: importing provider adapters here should not trigger network calls or heavy initialization.
from .providers.openai_chat_completions import OpenAIChatCompletionsAdapter
from .providers.openai_responses import OpenAIResponsesAdapter
from .providers.anthropic_messages import AnthropicMessagesAdapter
from .providers.moonshot_chat_completions import MoonshotChatCompletionsAdapter
from .providers.deepseek_chat_completions import DeepSeekChatCompletionsAdapter
from .providers.deepseek_responses import DeepSeekResponsesAdapter
from .providers.xai_responses import XAIResponsesAdapter
from .providers.meta_responses import MetaResponsesAdapter
from .providers.gemini_generate_content import GeminiGenerateContentAdapter
from .providers.mock import MockAdapter
from .providers.local import LocalAdapter


def _register_default_adapters() -> None:
    """Register built-in adapters at import time."""

    adapter_registry.register(
        "openai",
        "chat_completions",
        factory=lambda *, provider, adapter_name, credentials=None, **kw: OpenAIChatCompletionsAdapter(
            provider=provider,
            adapter_name=adapter_name,
            credentials=credentials,
        ),
    )
    # Moonshot (Kimi) - Uses dedicated adapter for Moonshot-specific wire format
    adapter_registry.register(
        "moonshot",
        "chat_completions",
        factory=lambda *, provider, adapter_name, credentials=None, **kw: MoonshotChatCompletionsAdapter(
            provider=provider,
            adapter_name=adapter_name,
            credentials=credentials,
        ),
    )
    # DeepSeek V4 - Chat Completions fallback (reasoning_content replay + thinking params)
    adapter_registry.register(
        "deepseek",
        "chat_completions",
        factory=lambda *, provider, adapter_name, credentials=None, **kw: DeepSeekChatCompletionsAdapter(
            provider=provider,
            adapter_name=adapter_name,
            credentials=credentials,
        ),
    )
    # DeepSeek V4 - Responses (default; builtin web_search)
    adapter_registry.register(
        "deepseek",
        "responses",
        factory=lambda *, provider, adapter_name, credentials=None, **kw: DeepSeekResponsesAdapter(
            provider=provider,
            adapter_name=adapter_name,
            credentials=credentials,
        ),
    )
    # xAI (Grok) - Responses API with Grok reasoning/cache policy
    adapter_registry.register(
        "xai",
        "responses",
        factory=lambda *, provider, adapter_name, credentials=None, **kw: XAIResponsesAdapter(
            provider=provider,
            adapter_name=adapter_name,
            credentials=credentials,
        ),
    )
    # Meta (Muse Spark) - Responses API with always-on reasoning + web_search
    adapter_registry.register(
        "meta",
        "responses",
        factory=lambda *, provider, adapter_name, credentials=None, **kw: MetaResponsesAdapter(
            provider=provider,
            adapter_name=adapter_name,
            credentials=credentials,
        ),
    )
    adapter_registry.register(
        "openai",
        "responses",
        factory=lambda *, provider, adapter_name, credentials=None, **kw: OpenAIResponsesAdapter(
            provider=provider,
            adapter_name=adapter_name,
            credentials=credentials,
        ),
    )

    adapter_registry.register(
        "anthropic",
        "messages",
        factory=lambda *, provider, adapter_name, credentials=None, **kw: AnthropicMessagesAdapter(
            provider=provider,
            adapter_name=adapter_name,
            credentials=credentials,
        ),
    )

    adapter_registry.register(
        "gemini",
        "generate_content",
        factory=lambda *, provider, adapter_name, credentials=None, **kw: GeminiGenerateContentAdapter(
            provider=provider,
            adapter_name=adapter_name,
            credentials=credentials,
        ),
    )

    adapter_registry.register(
        "mock",
        "mock",
        factory=lambda *, provider, adapter_name, credentials=None, **kw: MockAdapter(
            provider=provider,
            adapter_name=adapter_name,
            credentials=credentials,
        ),
    )

    adapter_registry.register(
        "local",
        "local",
        factory=lambda *, provider, adapter_name, credentials=None, **kw: LocalAdapter(
            provider=provider,
            adapter_name=adapter_name,
            credentials=credentials,
        ),
    )


_register_default_adapters()

__all__ = [
    "CapabilityDescriptor",
    "LLMProviderAdapter",
    "AdapterRegistry",
    "adapter_registry",
    "OpenAIChatCompletionsAdapter",
    "OpenAIResponsesAdapter",
    "AnthropicMessagesAdapter",
    "MoonshotChatCompletionsAdapter",
    "DeepSeekChatCompletionsAdapter",
    "DeepSeekResponsesAdapter",
    "XAIResponsesAdapter",
    "MetaResponsesAdapter",
    "GeminiGenerateContentAdapter",
    "MockAdapter",
    "LocalAdapter",
]

