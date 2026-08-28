"""
Motet - Canonical Adapter Contract Helpers (ADR-0064)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-23

Description:
    Shared assertions for ADR-0064 capability and canonical protocol contracts.
    Used by parametrized registry tests so every ModelSpec/adapter pair is checked
    the same way (CAP_* ↔ CapabilityDescriptor, stream event grammar, response shape).

Dependencies:
    - pytest: assertion helpers
    - motet.core.types: LLMResponse, stream events, ToolCallRequest
    - motet.core.models.specs: CAP_* constants
    - motet.core.models.adapters.base: CapabilityDescriptor

Usage:
    from tests.fixtures.canonical_adapter_contract import (
        assert_caps_match_spec,
        assert_canonical_llm_response,
        assert_canonical_stream_events,
    )
"""

from __future__ import annotations

from typing import Any, Iterable, List, Sequence, Set, Type

from motet.core.models.adapters.base import CapabilityDescriptor
from motet.core.models.specs import (
    CAP_IMAGE_GENERATION,
    CAP_JSON_MODE,
    CAP_REASONING,
    CAP_STREAM,
    CAP_SYSTEM_PROMPT,
    CAP_TOOL_USE,
    CAP_VISION,
    ModelSpec,
)
from motet.core.types import (
    CitationsEvent,
    ErrorEvent,
    LLMResponse,
    LLMStreamEvent,
    MediaPart,
    StopEvent,
    TextDeltaEvent,
    TextPart,
    ThinkingEvent,
    ToolCallCompleteEvent,
    ToolCallDeltaEvent,
    ToolCallRequest,
    ToolUseEvent,
    UsageEvent,
)

# ADR-0064 canonical stream event types (and their concrete classes).
CANONICAL_STREAM_EVENT_TYPES: Set[str] = {
    "text_delta",
    "tool_call_delta",
    "tool_call_complete",
    "tool_use",
    "citations",
    "thinking",
    "stop",
    "usage",
    "error",
}

CANONICAL_STREAM_EVENT_CLASSES: tuple[Type[Any], ...] = (
    TextDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallCompleteEvent,
    ToolUseEvent,
    CitationsEvent,
    ThinkingEvent,
    StopEvent,
    UsageEvent,
    ErrorEvent,
)

# Wire-format / provider keys that must not appear on orchestration-facing tool items.
_FORBIDDEN_TOOL_WIRE_KEYS = frozenset({"function", "type", "index"})


def _dummy_credentials(provider: str) -> dict[str, str]:
    """Credentials sufficient to construct adapters without network calls."""
    key = "test-key-not-for-live-calls"
    return {
        "api_key": key,
        f"{provider}_api_key": key,
        "openai_api_key": key,
        "anthropic_api_key": key,
        "gemini_api_key": key,
        "moonshot_api_key": key,
        "deepseek_api_key": key,
        "xai_api_key": key,
        "meta_api_key": key,
    }


def iter_registry_cases() -> List[tuple[str, str, ModelSpec]]:
    """Return (provider, model_name, spec) for every MODEL_REGISTRY entry."""
    from motet.core.models.specs import MODEL_REGISTRY

    cases: List[tuple[str, str, ModelSpec]] = []
    for provider, models in sorted(MODEL_REGISTRY.items()):
        for model_name, spec in sorted(models.items()):
            cases.append((provider, model_name, spec))
    return cases


def assert_caps_match_spec(
    spec: ModelSpec,
    caps: CapabilityDescriptor,
    *,
    model_id: str | None = None,
) -> None:
    """Assert ModelSpec CAP_* flags match CapabilityDescriptor booleans.

    ``model_id`` is the registry key / request model name passed to
    ``capabilities(model=...)``. Registry aliases may differ from ``spec.name``
    (e.g. ``claude-opus-4.7`` → ``claude-opus-4-7``); the descriptor echoes the
    requested id.
    """
    assert caps.provider == spec.provider
    if model_id is not None:
        assert caps.model == model_id

    expected = set(spec.capabilities)
    assert caps.supports_streaming is (CAP_STREAM in expected)
    assert caps.supports_tools is (CAP_TOOL_USE in expected)
    assert caps.supports_vision is (CAP_VISION in expected)
    assert caps.supports_reasoning is (CAP_REASONING in expected)
    assert caps.supports_json_mode is (CAP_JSON_MODE in expected)
    assert caps.supports_system_prompt is (CAP_SYSTEM_PROMPT in expected)
    assert caps.supports_image_generation is (CAP_IMAGE_GENERATION in expected)

    # One-way: if the ModelSpec lists provider builtins, the descriptor must advertise them.
    if spec.supported_builtin_tools:
        assert caps.supports_builtin_tools is True


def assert_canonical_output_item(item: Any) -> None:
    """Assert a single LLMResponse.output_items entry is a canonical type."""
    assert isinstance(item, (TextPart, MediaPart, ToolCallRequest)), (
        f"Non-canonical output item type: {type(item).__name__}"
    )
    if isinstance(item, ToolCallRequest):
        assert item.call_id
        assert item.tool_name
        assert isinstance(item.arguments_json, str)
        # Reject OpenAI Chat Completions wire shape leaking into output_items.
        if hasattr(item, "model_dump"):
            dumped = item.model_dump()
            assert not (_FORBIDDEN_TOOL_WIRE_KEYS & set(dumped.keys()) - {"type"}), (
                f"ToolCallRequest looks like provider wire format: {sorted(dumped.keys())}"
            )


def assert_canonical_llm_response(resp: LLMResponse) -> None:
    """Assert LLMResponse uses only canonical fields/types."""
    assert isinstance(resp, LLMResponse)
    assert resp.stop_reason is not None
    for item in resp.output_items or []:
        assert_canonical_output_item(item)
    if resp.reasoning_content is not None:
        assert isinstance(resp.reasoning_content, str)


def assert_canonical_stream_events(
    events: Sequence[LLMStreamEvent],
    *,
    require_terminal_stop: bool = True,
    allow_thinking: bool = True,
) -> None:
    """
    Assert a stream is composed only of ADR-0064 canonical events.

    Invariants:
    - Every event is a known canonical class with a known ``type`` literal
    - At most one terminal StopEvent (last non-error path)
    - ThinkingEvent only when allow_thinking is True
    - ToolCallCompleteEvent has call_id + tool_name + arguments_json
    """
    assert events, "stream produced no events"
    types_seen: List[str] = []
    for ev in events:
        assert isinstance(ev, CANONICAL_STREAM_EVENT_CLASSES), (
            f"Non-canonical stream event: {type(ev).__name__}"
        )
        ev_type = getattr(ev, "type", None)
        assert ev_type in CANONICAL_STREAM_EVENT_TYPES, f"Unknown event type: {ev_type!r}"
        types_seen.append(str(ev_type))

        if isinstance(ev, ThinkingEvent) and not allow_thinking:
            raise AssertionError("ThinkingEvent emitted but thinking was not enabled")

        if isinstance(ev, ToolCallCompleteEvent):
            assert ev.call_id
            assert ev.tool_name
            assert isinstance(ev.arguments_json, str)

        if isinstance(ev, ToolCallDeltaEvent):
            assert ev.call_id

    if require_terminal_stop:
        assert any(isinstance(ev, StopEvent) for ev in events), "stream missing StopEvent"
        # Last event should be StopEvent (optionally after ErrorEvent+Stop on failure paths)
        assert isinstance(events[-1], StopEvent), (
            f"stream must end with StopEvent, got {type(events[-1]).__name__}"
        )


def collect_stream(events: Iterable[LLMStreamEvent]) -> List[LLMStreamEvent]:
    """Materialize a stream iterator for assertions."""
    return list(events)
