"""
Motet - Unit tests for agent_turn / delegated metadata RAG keys

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-14

Description:
Verifies DELEGATED_CONTEXT_KEYS (SCHEDULE + ARTIFACT_RAG) are copied into
child distributed metadata so core.search_artifacts can honor
allow_broader_artifact_rag_scope when tool_execution inherits metadata
via motet.join (ADR-0122 Phase 7).

Usage:
    pytest tests/unit/core/orchestration/test_agent_turn_rag_metadata.py -q
"""

from __future__ import annotations

from motet.core.commands.command_data_classes import (
    ARTIFACT_RAG_CONTEXT_KEYS,
    DELEGATED_CONTEXT_KEYS,
    SCHEDULE_CONTEXT_KEYS,
)
from motet.core.orchestration.turn import _build_child_metadata


def test_delegated_keys_union_schedule_and_rag() -> None:
    assert "allow_broader_artifact_rag_scope" in ARTIFACT_RAG_CONTEXT_KEYS
    assert "artifact_rag_scope" in ARTIFACT_RAG_CONTEXT_KEYS
    assert "model_provider" in SCHEDULE_CONTEXT_KEYS
    assert set(SCHEDULE_CONTEXT_KEYS).issubset(DELEGATED_CONTEXT_KEYS)
    assert set(ARTIFACT_RAG_CONTEXT_KEYS).issubset(DELEGATED_CONTEXT_KEYS)


def test_build_child_metadata_copies_delegated_keys() -> None:
    meta = _build_child_metadata(
        {"routing": "keep"},
        {
            "model_provider": "xai",
            "allow_broader_artifact_rag_scope": True,
            "artifact_rag_scope": "motet",
            "artifact_tags": ["repo-docs"],
        },
        DELEGATED_CONTEXT_KEYS,
        qualified_id="app-builder.engineer",
        is_root_turn=True,
    )
    assert meta["routing"] == "keep"
    assert meta["model_provider"] == "xai"
    assert meta["allow_broader_artifact_rag_scope"] is True
    assert meta["artifact_rag_scope"] == "motet"
    assert meta["artifact_tags"] == ["repo-docs"]
    assert meta["agent_id"] == "app-builder.engineer"
    assert meta["conversation_primary_agent_id"] == "app-builder.engineer"


def test_build_child_metadata_schedule_only_omits_rag_auth() -> None:
    """tool.py / schedule_command keep SCHEDULE_CONTEXT_KEYS; RAG stays off stack."""
    meta = _build_child_metadata(
        {},
        {"allow_broader_artifact_rag_scope": True, "model_name": "grok-4.5"},
        SCHEDULE_CONTEXT_KEYS,
        qualified_id="core.default",
        is_root_turn=False,
    )
    assert "allow_broader_artifact_rag_scope" not in meta
    assert meta["model_name"] == "grok-4.5"


def test_delegated_forward_filter_keeps_rag_when_rebuilding_inherited_metadata() -> None:
    """Mirrors agentic_loop: rebuild inherited_metadata from DELEGATED_CONTEXT_KEYS."""
    parent = {
        "agent_id": "app-builder.engineer",
        "model_provider": "xai",
        "allow_broader_artifact_rag_scope": True,
        "artifact_rag_scope": "motet",
        "artifact_tags": ["repo-docs"],
        "routing": "internal-only",
    }
    inherited = {
        k: parent[k]
        for k in DELEGATED_CONTEXT_KEYS
        if parent.get(k) is not None and parent.get(k) != "" and parent.get(k) != []
    }
    assert inherited["allow_broader_artifact_rag_scope"] is True
    assert inherited["artifact_rag_scope"] == "motet"
    assert inherited["artifact_tags"] == ["repo-docs"]
    assert inherited["model_provider"] == "xai"
    assert "routing" not in inherited
