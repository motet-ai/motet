"""
Motet - Artifact Preparation Selector Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-07

Description:
    Unit tests for deterministic ADR-0110 strategy selection, including
    priorities, explicit overrides, disabled strategies, fallback diagnostics,
    and registry-backed preparation tools.

Dependencies:
    - pytest for assertions
    - artifact preparation selector and models
    - tool registry for bundle-style preparation tool registration

Usage:
    pytest tests/unit/core/artifacts/preparation/test_selector.py

Notes:
    - Registry tests use unique strategy IDs and unregister after execution.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from motet.core.artifacts.preparation.models import (
    ArtifactFeatureMatch,
    ArtifactPayloadInfo,
    ArtifactPrepHints,
    ArtifactPrepManifest,
    ArtifactPrepPlan,
    ArtifactPrepResult,
)
from motet.core.artifacts.preparation.selector import ArtifactPrepSelector
from motet.core.artifacts.preparation.strategy import ArtifactPrepContext


class _Strategy:
    def __init__(self, strategy_id: str, priority: int, content_type: str = "application/x-test") -> None:
        self.manifest = ArtifactPrepManifest(
            strategy_id=strategy_id,
            strategy_version="1.0.0",
            handles=[ArtifactFeatureMatch(content_types=[content_type])],
            priority=priority,
        )

    def plan(self, context: ArtifactPrepContext) -> ArtifactPrepPlan:
        return ArtifactPrepPlan(strategy_id=self.manifest.strategy_id, strategy_version=self.manifest.strategy_version)

    def prepare(self, plan: ArtifactPrepPlan, context: ArtifactPrepContext) -> ArtifactPrepResult:
        return ArtifactPrepResult(plan=plan, prep_state="prep_failed", diagnostics=["unit_test_strategy_not_executed"])


def _context(
    *,
    content_type: str = "application/x-test",
    hints: ArtifactPrepHints | None = None,
    metadata: dict[str, Any] | None = None,
) -> ArtifactPrepContext:
    return ArtifactPrepContext(
        artifact=SimpleNamespace(id="src", kind="source", metadata=metadata or {}, bytes=10),
        payload=b"payload",
        payload_info=ArtifactPayloadInfo(content_type=content_type, bytes=10),
        tenant_id="tenant",
        principal_id="principal",
        motet_id="motet",
        hints=hints or ArtifactPrepHints(),
    )


def test_selector_uses_priority_then_strategy_id_tiebreak() -> None:
    selector = ArtifactPrepSelector(
        strategies=[
            _Strategy("lower", priority=5),
            _Strategy("winner", priority=10),
            _Strategy("alpha", priority=10),
        ]
    )

    selection = selector.select(_context())

    assert selection.plan.strategy_id == "alpha"


def test_selector_honors_explicit_override_even_when_lower_priority() -> None:
    selector = ArtifactPrepSelector(strategies=[_Strategy("fast", priority=100), _Strategy("requested", priority=1)])

    selection = selector.select(_context(hints=ArtifactPrepHints(prep_strategy_id="requested")))

    assert selection.plan.strategy_id == "requested"


def test_selector_excludes_disabled_strategies() -> None:
    selector = ArtifactPrepSelector(strategies=[_Strategy("disabled", priority=100), _Strategy("enabled", priority=1)])

    selection = selector.select(_context(metadata={"disable_strategies": ["disabled"]}))

    assert selection.plan.strategy_id == "enabled"


def test_selector_adds_partial_text_fallback_for_text_payload() -> None:
    text_strategy = _Strategy("text_default", priority=1, content_type="text/plain")
    text_strategy.manifest.handles = []
    selector = ArtifactPrepSelector(strategies=[text_strategy])

    selection = selector.select(_context(content_type="text/plain"))

    assert selection.plan.strategy_id == "text_default"
    assert selection.plan.confidence == 0.6
    assert "fallback_text_strategy" in selection.plan.diagnostics


def test_selector_includes_registry_backed_prep_tools() -> None:
    from motet.core.tools.registry import registry as tool_registry

    strategy_id = "unit_registry_strategy"
    tool_name = "unit.registry_strategy"
    manifest = ArtifactPrepManifest(
        strategy_id=strategy_id,
        strategy_version="1.2.3",
        handles=[ArtifactFeatureMatch(content_types=["application/x-registry-prep"])],
        priority=99,
    )
    tool_registry.register(
        name=tool_name,
        description="Unit test registry-backed prep strategy",
        func=lambda _params: {},
        prep_manifest=manifest,
        expose_to_agents=False,
        category="artifact_preparation",
    )
    try:
        selection = ArtifactPrepSelector().select(_context(content_type="application/x-registry-prep"))
    finally:
        tool_registry.unregister(tool_name)

    assert selection.strategy.manifest.strategy_id == strategy_id
    assert selection.plan.strategy_id == strategy_id
    assert selection.plan.strategy_version == "1.2.3"


def test_selector_raises_for_disabled_explicit_strategy() -> None:
    selector = ArtifactPrepSelector(strategies=[_Strategy("requested", priority=1)])

    with pytest.raises(ValueError, match="not registered or enabled"):
        selector.select(
            _context(
                hints=ArtifactPrepHints(prep_strategy_id="requested", disable_strategies=["requested"]),
            )
        )
