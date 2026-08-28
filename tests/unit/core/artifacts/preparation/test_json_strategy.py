"""
Motet - JSON Preparation Strategy Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-07

Description:
    Unit tests for ADR-0110 JSON pointer preparation, including RFC 6901
    escaping, large-array window diagnostics, max-depth behavior, and parse
    failures.

Dependencies:
    - pytest for assertions
    - JSON preparation strategy models

Usage:
    pytest tests/unit/core/artifacts/preparation/test_json_strategy.py

Notes:
    - These tests cover structured chunk provenance used by artifact RAG.
"""

from __future__ import annotations

from types import SimpleNamespace

from motet.core.artifacts.preparation.models import ArtifactPayloadInfo, JsonCoord
from motet.core.artifacts.preparation.strategies.json import JsonPreparationStrategy
from motet.core.artifacts.preparation.strategy import ArtifactPrepContext


def _context(payload: object, *, max_depth: int = 3) -> ArtifactPrepContext:
    return ArtifactPrepContext(
        artifact=SimpleNamespace(id="artifact-1", kind="tool_artifact", metadata={"tags": ["json"]}, created_at=1.0),
        payload=payload,
        payload_info=ArtifactPayloadInfo(
            content_type="application/json",
            extension=".json",
            bytes=len(str(payload)),
            filename="data.json",
            content_hash="declared-source-hash",
        ),
        source_artifact_id="source-1",
        artifact_tags=["source-tag"],
        tenant_id="tenant",
        principal_id="principal",
        motet_id="motet",
        conversation_id="conv",
        config={"json_max_depth": max_depth},
    )


def test_json_strategy_escapes_pointers_and_windows_large_arrays() -> None:
    payload = {"a/b": {"~key": 1}, "items": list(range(105))}
    strategy = JsonPreparationStrategy()
    context = _context(payload)
    plan = strategy.plan(context)

    result = strategy.prepare(plan, context)

    assert all(isinstance(chunk.coordinates, JsonCoord) for chunk in result.chunks)
    pointers = {chunk.coordinates.pointer for chunk in result.chunks if isinstance(chunk.coordinates, JsonCoord)}
    assert result.prep_state == "prep_complete"
    assert "/a~1b/~0key" in pointers
    assert "/items/100-" in pointers
    assert all(chunk.source_artifact_id == "source-1" for chunk in result.chunks)
    assert all(chunk.artifact_tags == ["source-tag"] for chunk in result.chunks)
    assert result.chunk_cache_key


def test_json_strategy_respects_max_depth() -> None:
    payload = {"level1": {"level2": {"level3": {"value": 1}}}}
    strategy = JsonPreparationStrategy()
    context = _context(payload, max_depth=1)
    plan = strategy.plan(context)

    result = strategy.prepare(plan, context)

    assert len(result.chunks) == 1
    assert isinstance(result.chunks[0].coordinates, JsonCoord)
    assert result.chunks[0].coordinates.pointer == "/level1"
    assert result.chunks[0].structured_payload == {"level2": {"level3": {"value": 1}}}


def test_json_strategy_reports_parse_failure() -> None:
    strategy = JsonPreparationStrategy()
    context = _context("{not json")
    plan = strategy.plan(context)

    result = strategy.prepare(plan, context)

    assert result.prep_state == "prep_failed"
    assert result.chunks == []
    assert result.diagnostics
    assert result.diagnostics[0].startswith("json_parse_failed:")
