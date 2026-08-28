"""
Motet - Mock Adapter

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Mock adapter for testing that implements the adapter interface without provider objects.
    Provides deterministic echo-style responses and simple memory-aware behavior.
    Capability flags are derived from ModelSpec when present.

Dependencies:
    - re: Pattern matching for mock responses
    - motet.core.types: Canonical protocol models

Usage:
adapter = MockAdapter(provider="mock", adapter_name="mock")
resp = adapter.complete(LLMRequest(messages=[...]))

Notes:
    - This adapter is intended for tests and local development.
    - Streaming yields word-level TextDeltaEvent tokens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, cast

from ....types import (
    ErrorEvent,
    GeneratedImage,
    ImageGenerationRequest,
    ImageGenerationResponse,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMUsage,
    OutputItem,
    StopEvent,
    StopReason,
    TextDeltaEvent,
    TextPart,
    ThinkingEvent,
    UsageEvent,
)
from ..base import CapabilityDescriptor
from ...registry import get_model_spec
from ...specs import (
    CAP_IMAGE_GENERATION,
    CAP_JSON_MODE,
    CAP_REASONING,
    CAP_STREAM,
    CAP_SYSTEM_PROMPT,
    CAP_TOOL_USE,
    CAP_VISION,
)


# 1x1 transparent PNG used as a deterministic generated-image fixture for tests.
_MOCK_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


def _mock_response(messages: List[Any]) -> str:
    msgs = list(messages)
    last_user = next((m for m in reversed(msgs) if getattr(m, "role", None) == "user"), None)

    if last_user:
        name_found = None
        for m in msgs:
            text = getattr(m, "content", "") or ""
            m1 = re.search(r"\bmy name is\s+([A-Za-z][A-Za-z\-']{1,30})\b", text, re.I)
            if m1:
                name_found = m1.group(1)

        if re.search(r"what'?s my name|what is my name", (getattr(last_user, "content", "") or ""), re.I) and name_found:
            return f"Your name is {name_found}."

        memory_count = sum(
            1
            for m in msgs
            if getattr(m, "role", None) == "system"
            and str(getattr(m, "content", "") or "").startswith("[memory:")
        )
        suffix = f" (considering {memory_count} memories)" if memory_count else ""
        return f"You said: {getattr(last_user, 'content', '')}{suffix}"

    return "Hello, I am a mock assistant."


@dataclass
class MockAdapter:
    provider: str
    adapter_name: str
    credentials: Optional[Dict[str, Any]] = None

    def capabilities(self, *, model: str) -> CapabilityDescriptor:
        # Derive from ModelSpec so registry CAP_* ↔ CapabilityDescriptor stays consistent.
        # When no spec exists (ad-hoc test models), keep a permissive mock default.
        spec = get_model_spec(self.provider, model)
        if spec is None:
            return CapabilityDescriptor(
                provider=self.provider,
                model=model,
                supports_streaming=True,
                supports_tools=False,
                supports_parallel_tool_calls=False,
                supports_tool_call_id=False,
                supports_vision=False,
                supports_image_generation=True,
                supports_json_mode=False,
                supports_json_schema_strict=False,
                supports_stateful_sessions=False,
                supports_builtin_tools=False,
                supports_system_prompt=True,
                supports_reasoning=True,
                provider_metadata={"adapter": "mock"},
            )
        caps = set(spec.capabilities)
        return CapabilityDescriptor(
            provider=self.provider,
            model=model,
            supports_streaming=CAP_STREAM in caps,
            supports_tools=CAP_TOOL_USE in caps,
            supports_parallel_tool_calls=CAP_TOOL_USE in caps,
            supports_tool_call_id=False,
            supports_vision=CAP_VISION in caps,
            supports_image_generation=CAP_IMAGE_GENERATION in caps,
            supports_json_mode=CAP_JSON_MODE in caps,
            supports_json_schema_strict=False,
            supports_stateful_sessions=False,
            supports_builtin_tools=bool(spec.supported_builtin_tools),
            supports_system_prompt=CAP_SYSTEM_PROMPT in caps,
            supports_reasoning=CAP_REASONING in caps,
            provider_metadata={"adapter": "mock"},
        )

    def complete(self, request: LLMRequest) -> LLMResponse:
        settings = request.model_settings or {}
        enable_thinking = bool(settings.get("enable_thinking", False))
        output_text = _mock_response(request.messages)
        output_items = [TextPart(text=output_text)] if output_text else []
        reasoning_content = None
        if enable_thinking:
            reasoning_content = (
                "Let me think about this... I should consider the user's question carefully."
            )
        return LLMResponse(
            output_text=output_text or None,
            output_items=cast(List[OutputItem], output_items),
            stop_reason=StopReason.NATURAL_STOP,
            usage=None,
            reasoning_content=reasoning_content,
            raw_provider_metadata={"adapter": "mock"},
        )

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        settings = request.model_settings or {}
        enable_thinking = bool(settings.get("enable_thinking", False))

        # Emit mock thinking if enabled (for testing ThinkingEvent handling)
        if enable_thinking:
            thinking_text = "Let me think about this... I should consider the user's question carefully."
            for word in thinking_text.split():
                yield ThinkingEvent(text=word + " ", is_complete=False)
            yield ThinkingEvent(text="", is_complete=True)

        output_text = _mock_response(request.messages)
        for token in output_text.split():
            yield TextDeltaEvent(text=token + " ")
        
        # ADR-0064 R9: Emit mock usage for testing
        yield UsageEvent(usage=LLMUsage(
            prompt_tokens=100,
            output_tokens=len(output_text.split()),
            total_tokens=100 + len(output_text.split()),
        ))
        yield StopEvent(reason=StopReason.NATURAL_STOP)

    def generate_images(self, request: ImageGenerationRequest) -> ImageGenerationResponse:
        """ADR-0113: deterministic mock image generation (returns 1x1 PNGs)."""
        n = max(1, int(request.n or 1))
        images = [
            GeneratedImage(
                mime_type="image/png",
                base64_data=_MOCK_PNG_B64,
                revised_prompt=request.prompt,
            )
            for _ in range(n)
        ]
        return ImageGenerationResponse(
            images=images,
            model=(request.model_settings or {}).get("model_name") or "mock-image",
            raw_provider_metadata={"adapter": "mock"},
        )


__all__ = ["MockAdapter"]
