"""
Motet - Artifact Preparation Executor Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-07

Description:
    Unit tests for the ADR-0110 preparation executor, including fallback
    visibility when deterministic dispatch uses the text fallback path.

Dependencies:
    - pytest for assertions
    - artifact preparation models and executor

Usage:
    pytest tests/unit/core/artifacts/preparation/test_executor.py

Notes:
    - Fallback runs must be surfaced as prep_partial for indexing status.
"""

from __future__ import annotations

from types import SimpleNamespace

from motet.core.artifacts.preparation.executor import ArtifactPrepExecutor
from motet.core.artifacts.preparation.models import (
    ArtifactPayloadInfo,
    ArtifactPrepManifest,
    ArtifactPrepPlan,
    ArtifactPrepResult,
    PreparedArtifactChunk,
    TextCoord,
)
from motet.core.artifacts.preparation.strategy import ArtifactPrepContext


def _chunk(confidence: float = 0.95) -> PreparedArtifactChunk:
    return PreparedArtifactChunk(
        source_artifact_id="src",
        chunk_index=0,
        chunk_kind="text",
        content_text="hello",
        content_hash="hash",
        coordinates=TextCoord(byte_start=0, byte_end=5),
        tenant_id="tenant",
        principal_id="principal",
        motet_id="motet",
        conversation_id="conv",
        confidence=confidence,
        prep_strategy_id="text_default",
        prep_strategy_version="1.0.0",
        created_at=1.0,
    )


class _Strategy:
    manifest = ArtifactPrepManifest(strategy_id="text_default", strategy_version="1.0.0")

    def plan(self, context: ArtifactPrepContext) -> ArtifactPrepPlan:
        return ArtifactPrepPlan(strategy_id=self.manifest.strategy_id, strategy_version=self.manifest.strategy_version)

    def prepare(self, plan: ArtifactPrepPlan, context: ArtifactPrepContext) -> ArtifactPrepResult:
        return ArtifactPrepResult(plan=plan, prep_state="prep_complete", chunks=[_chunk()], diagnostics=[])


def test_executor_marks_fallback_results_partial() -> None:
    plan = ArtifactPrepPlan(
        strategy_id="text_default",
        strategy_version="1.0.0",
        diagnostics=["fallback_text_strategy"],
    )
    context = ArtifactPrepContext(
        artifact=SimpleNamespace(id="src", kind="source", metadata={}),
        payload=b"hello",
        payload_info=ArtifactPayloadInfo(content_type="text/plain", bytes=5),
        tenant_id="tenant",
        principal_id="principal",
        motet_id="motet",
    )

    result = ArtifactPrepExecutor().execute(strategy=_Strategy(), plan=plan, context=context)

    assert result.prep_state == "prep_partial"
    assert result.diagnostics == ["fallback_text_strategy"]
    assert result.chunks[0].prep_state == "prep_partial"
    assert result.chunks[0].confidence == 0.6


def test_executor_rejects_strategy_plan_mismatch() -> None:
    plan = ArtifactPrepPlan(strategy_id="json_pointer", strategy_version="1.0.0")
    context = ArtifactPrepContext(
        artifact=SimpleNamespace(id="src", kind="source", metadata={}),
        payload=b"{}",
        payload_info=ArtifactPayloadInfo(content_type="application/json", bytes=2),
        tenant_id="tenant",
        principal_id="principal",
        motet_id="motet",
    )

    try:
        ArtifactPrepExecutor().execute(strategy=_Strategy(), plan=plan, context=context)
    except ValueError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("Expected strategy/plan mismatch to raise")
