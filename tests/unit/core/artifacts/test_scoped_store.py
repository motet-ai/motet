"""
Motet - Scoped Artifact Store Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Unit tests for ScopedArtifactStore, the wrapper that pre-binds isolation
    context (tenant_id, principal_id, motet_id) onto an underlying artifact
    store. These tests verify that delegating methods forward the pre-bound
    context to the underlying store so callers never pass isolation params.

    Regression coverage: update_metadata was previously missing from the
    wrapper, so callers (e.g. artifact RAG prep state persistence in
    rag.py) raised AttributeError and silently dropped durable prep state.

Dependencies:
    - pytest: test runner
    - unittest.mock: MagicMock for the underlying store
    - motet.core.artifacts.scoped_store: system under test

Usage:
    pytest tests/unit/core/artifacts/test_scoped_store.py
"""

from __future__ import annotations

from unittest.mock import MagicMock

from motet.core.artifacts.scoped_store import ScopedArtifactStore


def _make_scoped() -> tuple[ScopedArtifactStore, MagicMock]:
    underlying = MagicMock()
    scoped = ScopedArtifactStore(
        store=underlying,
        tenant_id="tenant-123",
        principal_id="user-456",
        motet_id="default",
    )
    return scoped, underlying


def test_update_metadata_delegates_with_bound_context() -> None:
    scoped, underlying = _make_scoped()
    sentinel = object()
    underlying.update_metadata.return_value = sentinel

    result = scoped.update_metadata("art-1", {"prep_state_by_strategy": {"video_default": "prep_complete"}})

    assert result is sentinel
    underlying.update_metadata.assert_called_once_with(
        artifact_id="art-1",
        metadata_patch={"prep_state_by_strategy": {"video_default": "prep_complete"}},
        tenant_id="tenant-123",
        principal_id="user-456",
        motet_id="default",
    )


def test_update_metadata_does_not_accept_caller_context_overrides() -> None:
    """The wrapper pre-binds context; callers must not pass isolation params."""
    scoped, _ = _make_scoped()

    try:
        scoped.update_metadata(  # type: ignore[call-arg]
            "art-1",
            {"k": "v"},
            tenant_id="other-tenant",
        )
    except TypeError:
        return
    raise AssertionError("update_metadata should reject caller-supplied context kwargs")
