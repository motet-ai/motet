"""
Motet - Provider Adapter Base Types

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Provider-agnostic adapter interface for model inference.

    This is the boundary: adapters translate between canonical protocol types
    (LLMRequest/LLMResponse + LLMStreamEvent) and provider-specific APIs.

    Adapter responsibilities (translation-only):
    - Render canonical inputs → provider request schema
    - Parse provider responses/streams → canonical outputs/events
    - Attach provider metadata for debugging/accounting
    - Implement bounded retries for transient failures (RECOMMENDED)
    - Use circuit breaker for production resilience (RECOMMENDED)

    Adapter non-responsibilities (handled by orchestration):
    - Tool discovery and selection
    - Structured output validation and repair
    - Memory and conversation policy
    - Cost/budget enforcement

Dependencies:
    - abc: Abstract base class for formal interface
    - typing: Protocol and iterators for streaming
    - pydantic: Capability descriptor model
    - motet.core.types: Canonical request/response/stream types

Usage:
    from motet.core.models.adapters.base import LLMProviderAdapter, LLMAdapterBase, CapabilityDescriptor
    from motet.core.types import LLMRequest

    # Using the Protocol (duck typing)
    def run(adapter: LLMProviderAdapter, request: LLMRequest):
        caps = adapter.capabilities(model=request.model_settings.get("model_name", ""))
        return adapter.complete(request)

    # Using the ABC (explicit inheritance)
    class MyAdapter(LLMAdapterBase):
        def capabilities(self, *, model: str) -> CapabilityDescriptor: ...
        def complete(self, request: LLMRequest) -> LLMResponse: ...
        def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]: ...

Notes:
    - Adapters MUST be translation-only. Do not implement orchestration logic here.
    - CapabilityDescriptor is intentionally boolean-first; store provider specifics in metadata.
    - Error handling (R6): Adapters MAY implement bounded retries (3 attempts recommended).
      On terminal failure, adapters MUST emit ErrorEvent (streaming) or raise (non-streaming).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Iterator, Optional, Protocol

from pydantic import BaseModel, Field

from ...types import (
    ImageGenerationRequest,
    ImageGenerationResponse,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
)


class CapabilityDescriptor(BaseModel):
    """Provider/model capability descriptor."""

    provider: str = Field(..., description="Provider name (openai|anthropic|gemini|local|...).")
    model: str = Field(..., description="Model identifier within provider.")

    # Core capabilities
    supports_streaming: bool = Field(default=False, description="Supports streaming token/events.")
    supports_tools: bool = Field(default=False, description="Supports model-driven tool calling.")
    supports_parallel_tool_calls: bool = Field(default=False, description="Supports multiple tool calls in one turn.")
    supports_tool_call_id: bool = Field(default=False, description="Provider emits a stable tool call ID.")

    # Multimodal
    supports_vision: bool = Field(default=False, description="Supports image inputs (vision).")
    supports_audio: bool = Field(default=False, description="Supports audio inputs.")
    supports_video: bool = Field(default=False, description="Supports video inputs.")
    # ADR-0113: image *output* (model generates images), distinct from vision input
    supports_image_generation: bool = Field(
        default=False,
        description="Supports image generation/output (e.g. Gpt-image, grok-imagine, Imagen)..",
    )

    # Structured output
    supports_json_mode: bool = Field(default=False, description="Supports native JSON-only output mode.")
    supports_json_schema_strict: bool = Field(
        default=False,
        description="Supports strict JSON Schema enforcement (server-side or native).",
    )

    # Provider-side state and built-ins
    supports_stateful_sessions: bool = Field(
        default=False,
        description="Supports provider-side conversation state (e.g., OpenAI previous_response_id).",
    )
    supports_builtin_tools: bool = Field(
        default=False,
        description="Supports provider-native built-in tools (e.g., web search) that are not registry tools.",
    )

    # Misc / future
    supports_system_prompt: bool = Field(default=True, description="Supports a system/developer instruction channel.")
    supports_reasoning: bool = Field(default=False, description="Supports explicit reasoning traces/tokens.")

    # Provider-specific spillover
    provider_metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Provider-specific capability metadata (non-portable).",
    )


class LLMProviderAdapter(Protocol):
    """
    Protocol for provider adapters (translation-only boundary).

    This is a structural protocol (duck typing). Classes that implement
    the required methods are considered adapters without explicit inheritance.

    For explicit inheritance with abstract method enforcement, use LLMAdapterBase.

    Error Handling (ADR-0064 R6):
        - Adapters MAY implement bounded retries (3 attempts recommended)
        - On terminal failure:
          - complete(): MUST raise an exception
          - stream(): MUST emit ErrorEvent followed by StopEvent(reason=ERROR)
        - ErrorEvent.message SHOULD include provider error code when available
        - Circuit breakers are RECOMMENDED for production adapters
    """

    provider: str
    adapter_name: str

    def capabilities(self, *, model: str) -> CapabilityDescriptor:
        """Return capabilities for a given provider model."""

        ...

    def complete(self, request: LLMRequest) -> LLMResponse:
        """
        Perform a non-streaming completion.

        Args:
            request: Canonical LLM request with messages, tools, and settings.

        Returns:
            LLMResponse with output_items, stop_reason, and optional usage/metadata.

        Raises:
            Exception: On terminal failure after any retries.
        """

        ...

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        """
        Perform a streaming completion emitting canonical stream events.

        Args:
            request: Canonical LLM request with messages, tools, and settings.

        Yields:
            LLMStreamEvent instances in order:
            - TextDeltaEvent: Incremental text tokens
            - ToolCallDeltaEvent: Incremental tool call arguments (if supported)
            - ToolCallCompleteEvent: Complete tool call (required if tool calls occur)
            - ToolUseEvent: Tool invocation status (provider/motet/mcp/workflow)
            - ThinkingEvent: Model reasoning traces (if supported and enabled)
            - CitationsEvent: Citations/annotations (if supported)
            - UsageEvent: Token usage (best-effort, often at end)
            - ErrorEvent: On error (followed by StopEvent)
            - StopEvent: Exactly one, always last

        Contract:
            - Exactly one StopEvent MUST be emitted, always as the final event.
            - If tool calls occur, ToolCallCompleteEvent MUST be emitted before StopEvent.
            - ErrorEvent MUST be followed by StopEvent(reason=ERROR).
        """

        ...

    def generate_images(self, request: ImageGenerationRequest) -> ImageGenerationResponse:
        """
        Generate image(s) from a text(+image) prompt (ADR-0113).

        Optional: only adapters whose capabilities report supports_image_generation=True
        implement this. Others raise NotImplementedError and callers must capability-gate.

        Args:
            request: Canonical image-generation request.

        Returns:
            ImageGenerationResponse with generated images (base64 or URL).

        Raises:
            NotImplementedError: If the adapter/model does not support image generation.
            Exception: On terminal failure after any retries.
        """

        ...


class LLMAdapterBase(ABC):
    """
    Abstract base class for provider adapters (ADR-0064).

    Use this for explicit inheritance with abstract method enforcement.
    All concrete adapters MUST implement all three methods.

    For duck-typed adapters, use the LLMProviderAdapter Protocol instead.
    """

    provider: str
    adapter_name: str

    @abstractmethod
    def capabilities(self, *, model: str) -> CapabilityDescriptor:
        """Return capabilities for a given provider model."""
        ...

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:
        """Perform a non-streaming completion."""
        ...

    @abstractmethod
    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        """Perform a streaming completion emitting canonical stream events."""
        ...

    def generate_images(self, request: ImageGenerationRequest) -> ImageGenerationResponse:
        """
        Generate image(s) from a text(+image) prompt (ADR-0113).

        Default raises NotImplementedError so existing adapters remain valid; adapters that
        support image output override this and report supports_image_generation=True.
        """
        raise NotImplementedError(
            f"Adapter {getattr(self, 'adapter_name', type(self).__name__)} does not support image generation"
        )


__all__ = [
    "CapabilityDescriptor",
    "LLMProviderAdapter",
    "LLMAdapterBase",
]

