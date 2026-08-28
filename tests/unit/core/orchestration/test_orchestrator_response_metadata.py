"""
Motet - Orchestrator Response Metadata Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-25

Description:
    Focused tests for metadata preservation in the distributed orchestrator
    response aggregation path. These tests ensure terminal stream metadata,
    including artifact RAG citations, survives non-streaming chat execution.

Dependencies:
    - pytest for async test execution
    - motet.core.orchestration.orchestrator for response aggregation
    - motet.core.types for canonical message models

Usage:
    pytest tests/unit/core/orchestration/test_orchestrator_response_metadata.py

Notes:
    - The stream source is stubbed to keep the test isolated from Redis and
      worker infrastructure.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

import pytest

from motet.core.orchestration.orchestrator import DistributedOrchestrator
from motet.core.types import Message


@pytest.mark.asyncio
async def test_run_preserves_terminal_artifact_rag_citations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-streaming run should return terminal citation metadata in Response.raw."""

    orchestrator = DistributedOrchestrator()
    citations = [
        {
            "citation_id": "A1",
            "source_label": "sample.pdf",
            "artifact_id": "source-1",
        }
    ]

    async def _stream_events(*args: Any, **kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
        _ = args, kwargs
        yield {"event": "token", "data": "answer"}
        yield {"event": "end", "content": "answer", "artifact_rag_citations": citations}

    monkeypatch.setattr(orchestrator, "stream_events", _stream_events)

    response = await orchestrator.run(object(), [Message(role="user", content="question")])

    assert response.content == "answer"
    assert response.raw is not None
    assert response.raw["artifact_rag_citations"] == citations


@pytest.mark.asyncio
async def test_run_maps_terminal_usage_onto_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-streaming run should populate Response usage fields from end.usage."""

    orchestrator = DistributedOrchestrator()
    usage = {
        "prompt_tokens": 12,
        "completion_tokens": 34,
        "total_tokens": 46,
        "reasoning_tokens": 5,
    }

    async def _stream_events(*args: Any, **kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
        _ = args, kwargs
        yield {"event": "token", "data": "answer"}
        yield {"event": "end", "content": "answer", "usage": usage}

    monkeypatch.setattr(orchestrator, "stream_events", _stream_events)

    response = await orchestrator.run(object(), [Message(role="user", content="question")])

    assert response.content == "answer"
    assert response.usage_tokens_input == 12
    assert response.usage_tokens_output == 34
    assert response.raw is not None
    assert response.raw["usage"] == usage
