"""
Motet - Artifact Preparation Routing Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-07

Description:
    Unit tests for ADR-0110 source-vs-derived routing. The routing helper uses
    the selector against both payloads so non-text source strategies can replace
    derived plain-text indexing.

Dependencies:
    - pytest for fixtures and assertions
    - artifact preparation routing and selector models

Usage:
    pytest tests/unit/core/artifacts/preparation/test_routing.py

Notes:
    - These tests protect generic routing behavior beyond DOCX-specific cases.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from motet.core.artifacts.preparation.models import ArtifactPayloadInfo, ArtifactPrepManifest, ArtifactPrepPlan
from motet.core.artifacts.preparation.routing import should_prepare_source_instead_of_derived
from motet.core.artifacts.preparation.selector import ArtifactPrepSelection
from motet.core.artifacts.preparation.strategy import ArtifactPrepContext, ArtifactPrepStrategy


@pytest.fixture()
def text_manifest() -> ArtifactPrepManifest:
    return ArtifactPrepManifest(strategy_id="text_default", strategy_version="1.0.0")


@pytest.fixture()
def docx_manifest() -> ArtifactPrepManifest:
    return ArtifactPrepManifest(strategy_id="docx_structured", strategy_version="1.0.0")


@pytest.fixture()
def source_ctx() -> ArtifactPrepContext:
    return ArtifactPrepContext(
        artifact=SimpleNamespace(id="src", kind="source", metadata={}),
        payload=b"x",
        payload_info=ArtifactPayloadInfo(content_type="application/octet-stream", bytes=1),
        tenant_id="t1",
        principal_id="p1",
        motet_id="m1",
    )


@pytest.fixture()
def derived_ctx() -> ArtifactPrepContext:
    return ArtifactPrepContext(
        artifact=SimpleNamespace(id="der", kind="derived", metadata={}),
        payload=b"y",
        payload_info=ArtifactPayloadInfo(content_type="text/plain", bytes=1),
        tenant_id="t1",
        principal_id="p1",
        motet_id="m1",
    )


def test_should_prepare_source_when_derived_plan_is_text_default(
    text_manifest: ArtifactPrepManifest,
    docx_manifest: ArtifactPrepManifest,
    source_ctx: ArtifactPrepContext,
    derived_ctx: ArtifactPrepContext,
) -> None:
    text_strategy = Mock(spec=ArtifactPrepStrategy)
    text_strategy.manifest = text_manifest
    docx_strategy = Mock(spec=ArtifactPrepStrategy)
    docx_strategy.manifest = docx_manifest

    mock_selector = Mock()
    mock_selector.select = Mock(
        side_effect=[
            ArtifactPrepSelection(
                strategy=docx_strategy,
                plan=ArtifactPrepPlan(strategy_id="docx_structured", strategy_version="1.0.0"),
            ),
            ArtifactPrepSelection(
                strategy=text_strategy,
                plan=ArtifactPrepPlan(strategy_id="text_default", strategy_version="1.0.0"),
            ),
        ]
    )

    assert (
        should_prepare_source_instead_of_derived(
            mock_selector,
            source_context=source_ctx,
            derived_context=derived_ctx,
        )
        is True
    )
    assert mock_selector.select.call_count == 2


def test_should_not_prepare_source_when_both_select_text_default(
    text_manifest: ArtifactPrepManifest,
    source_ctx: ArtifactPrepContext,
    derived_ctx: ArtifactPrepContext,
) -> None:
    text_strategy = Mock(spec=ArtifactPrepStrategy)
    text_strategy.manifest = text_manifest
    same_plan = ArtifactPrepPlan(strategy_id="text_default", strategy_version="1.0.0")
    mock_selector = Mock()
    mock_selector.select = Mock(
        return_value=ArtifactPrepSelection(strategy=text_strategy, plan=same_plan),
    )

    assert (
        should_prepare_source_instead_of_derived(
            mock_selector,
            source_context=source_ctx,
            derived_context=derived_ctx,
        )
        is False
    )
    assert mock_selector.select.call_count == 2


def test_prepare_source_when_derived_selection_raises_and_source_non_default(
    docx_manifest: ArtifactPrepManifest,
    source_ctx: ArtifactPrepContext,
    derived_ctx: ArtifactPrepContext,
) -> None:
    docx_strategy = Mock(spec=ArtifactPrepStrategy)
    docx_strategy.manifest = docx_manifest
    mock_selector = Mock()
    mock_selector.select = Mock(
        side_effect=[
            ArtifactPrepSelection(
                strategy=docx_strategy,
                plan=ArtifactPrepPlan(strategy_id="docx_structured", strategy_version="1.0.0"),
            ),
            ValueError("no strategy"),
        ]
    )

    assert (
        should_prepare_source_instead_of_derived(
            mock_selector,
            source_context=source_ctx,
            derived_context=derived_ctx,
        )
        is True
    )


def test_no_source_prep_when_source_selection_raises(
    source_ctx: ArtifactPrepContext,
    derived_ctx: ArtifactPrepContext,
) -> None:
    mock_selector = Mock()
    mock_selector.select = Mock(side_effect=ValueError("no strategy"))

    assert (
        should_prepare_source_instead_of_derived(
            mock_selector,
            source_context=source_ctx,
            derived_context=derived_ctx,
        )
        is False
    )
    mock_selector.select.assert_called_once_with(source_ctx)
