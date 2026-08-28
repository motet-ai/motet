"""
Motet - Context Provider Pipeline Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-30

Description:
    Unit tests for the ADR-0109 context preparation provider pipeline. These
    tests validate provider ordering, memory item serialization behavior,
    and the no-double-fetch invariant (#132) outside the distributed
    prepare_context command wrapper.

Dependencies:
    - dataclasses for lightweight test doubles
    - motet.core.orchestration.context provider modules
    - motet.core.types.Message for canonical user messages

Usage:
    pytest tests/unit/core/orchestration/test_context_provider_pipeline.py

Notes:
    - Tests avoid worker infrastructure and exercise providers directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from motet.core.orchestration.context.memory_context import MemoryRecallProvider
from motet.core.orchestration.context.pipeline import DEFAULT_CONTEXT_PROVIDERS
from motet.core.orchestration.context.token_budget import TokenBudgetProvider
from motet.core.orchestration.context.types import ContextPipelineState
from motet.core.types import MediaPart, Message, TextPart


@dataclass
class _PrepareData:
    include_memory_recall: bool = True


class _MemoryItemWithDump:
    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return {"content": "from model dump", "mode": mode}


class _MemoryStub:
    def recall(self, **kwargs: Any) -> list[Any]:
        return [_MemoryItemWithDump()]


@dataclass
class _MotetStub:
    memory: Any = _MemoryStub()
    conversation_id: str = "conv-1"


class _LoggerStub:
    def info(self, *args: Any, **kwargs: Any) -> None:
        return None

    def warning(self, *args: Any, **kwargs: Any) -> None:
        return None


def test_default_context_provider_order_exposes_rag_hook() -> None:
    provider_names = [provider.name for provider in DEFAULT_CONTEXT_PROVIDERS]

    assert provider_names == [
        "conversation_history",
        "memory_recall",
        "artifact_context",
        "rag_context",
        "token_budget",
    ]


def test_memory_provider_prefers_model_dump_for_context_info() -> None:
    state = ContextPipelineState(messages=[Message(role="user", content="remember this")])

    out = MemoryRecallProvider().apply(
        state,
        data=_PrepareData(),
        motet=_MotetStub(),
        logger=_LoggerStub(),
    )

    assert out.context_info["memory_items"] == [{"content": "from model dump", "mode": "json"}]


class _CountingMemoryStub:
    """Memory stub that counts calls to hybrid_retrieve and apply_vector_recall."""

    def __init__(self) -> None:
        self.hybrid_retrieve_call_count = 0
        self.apply_vector_recall_call_count = 0
        self.apply_vector_recall_got_memory_items = False

    def hybrid_retrieve(self, **kwargs: Any) -> list[Any]:
        self.hybrid_retrieve_call_count += 1
        return [_MemoryItemWithDump()]

    def apply_vector_recall(self, **kwargs: Any) -> list[Any]:
        self.apply_vector_recall_call_count += 1
        self.apply_vector_recall_got_memory_items = bool(kwargs.get("memory_items"))
        return kwargs.get("messages", [])

    def recall(self, **kwargs: Any) -> list[Any]:
        return []


def test_memory_provider_does_not_double_fetch() -> None:
    """A single prepare_context memory stage performs at most one hybrid_retrieve (#132).

    The provider should call hybrid_retrieve once and pass the results to
    apply_vector_recall via the memory_items= kwarg instead of having
    apply_vector_recall call hybrid_retrieve again internally.
    """
    state = ContextPipelineState(messages=[Message(role="user", content="query")])
    counting_memory = _CountingMemoryStub()

    @dataclass
    class _MotetWithCountingMemory:
        memory: Any = counting_memory
        conversation_id: str = "conv-1"

    MemoryRecallProvider().apply(
        state,
        data=_PrepareData(),
        motet=_MotetWithCountingMemory(),
        logger=_LoggerStub(),
    )

    assert counting_memory.hybrid_retrieve_call_count == 1, (
        f"Expected exactly 1 hybrid_retrieve call, got {counting_memory.hybrid_retrieve_call_count}. "
        "The provider and apply_vector_recall should not both call hybrid_retrieve."
    )
    assert counting_memory.apply_vector_recall_call_count == 1, (
        f"Expected exactly 1 apply_vector_recall call, got {counting_memory.apply_vector_recall_call_count}"
    )
    assert counting_memory.apply_vector_recall_got_memory_items, (
        "apply_vector_recall should receive pre-retrieved memory_items= kwarg "
        "so it skips its internal hybrid_retrieve call (#132)."
    )


def test_token_budget_provider_prunes_oldest_images_to_renderer_limit() -> None:
    messages = [
        Message(
            role="user",
            content=f"image turn {idx}",
            content_parts=[
                TextPart(text=f"image turn {idx}"),
                MediaPart(
                    media_type="image",
                    mime_type="image/jpeg",
                    artifact_id=f"image-{idx}",
                ),
            ],
        )
        for idx in range(5)
    ]
    state = ContextPipelineState(messages=messages)
    motet = SimpleNamespace(
        conversation_id="conv-1",
        stack=SimpleNamespace(config=SimpleNamespace(max_images=4)),
    )

    out = TokenBudgetProvider().apply(
        state,
        data=SimpleNamespace(max_context_tokens=None),
        motet=motet,
        logger=_LoggerStub(),
    )

    retained_image_ids = [
        part.artifact_id
        for msg in out.messages
        for part in (getattr(msg, "content_parts", None) or [])
        if getattr(part, "type", None) == "media"
    ]
    assert retained_image_ids == ["image-1", "image-2", "image-3", "image-4"]
    assert out.context_info["image_budget_applied"] is True
    assert out.context_info["image_parts_pruned"] == 1
    assert out.context_info["image_count"] == 4
