"""
Motet - Canonical Reasoning Effort Ladder Tests (ADR-0064)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-27

Description:
    Unit tests for the canonical reasoning-effort vocabulary shared by adapters and
    orchestration: low < medium < high < xhigh < max.

    Validates:
    - normalization of unusable values to a caller-supplied default
    - clamping to a provider's supported subset (highest rung at or below the request)
    - provider mappings verified live (OpenAI/xAI have no max, DeepSeek has two rungs)
    - orchestration coercion accepts xhigh instead of silently downgrading it

Dependencies:
    - pytest
    - motet.core.types: ReasoningEffort / REASONING_EFFORT_LADDER / normalize_reasoning_effort

Usage:
    pytest tests/unit/core/test_reasoning_effort_ladder.py -q
"""

from __future__ import annotations

import pytest

from motet.core.types import REASONING_EFFORT_LADDER, normalize_reasoning_effort


def test_ladder_is_ordered_cheapest_to_deepest() -> None:
    assert REASONING_EFFORT_LADDER == ("low", "medium", "high", "xhigh", "max")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("low", "low"),
        ("xhigh", "xhigh"),
        ("MAX", "max"),
        ("  high  ", "high"),
        ("banana", "medium"),
        (None, "medium"),
        (42, "medium"),
    ],
)
def test_normalize_without_provider_subset(value: object, expected: str) -> None:
    assert normalize_reasoning_effort(value) == expected


def test_normalize_honors_custom_default() -> None:
    assert normalize_reasoning_effort(None, default="high") == "high"
    assert normalize_reasoning_effort("nonsense", default="high") == "high"


@pytest.mark.parametrize(
    "requested,supported,expected",
    [
        # OpenAI / xAI: accept through xhigh, 400 on max.
        ("max", ("low", "medium", "high", "xhigh"), "xhigh"),
        ("xhigh", ("low", "medium", "high", "xhigh"), "xhigh"),
        # Anthropic Opus 5 with thinking disabled: capped at high.
        ("max", ("low", "medium", "high"), "high"),
        ("xhigh", ("low", "medium", "high"), "high"),
        ("low", ("low", "medium", "high"), "low"),
        # A provider whose floor is above the request degrades up to its cheapest rung.
        ("low", ("high", "max"), "high"),
    ],
)
def test_normalize_clamps_to_supported_subset(
    requested: str, supported: tuple[str, ...], expected: str
) -> None:
    assert normalize_reasoning_effort(requested, supported=supported) == expected


def test_normalize_ignores_empty_supported_set() -> None:
    """An empty allowlist should not silently erase the request."""
    assert normalize_reasoning_effort("xhigh", supported=()) == "xhigh"


def test_orchestration_coercion_accepts_full_ladder() -> None:
    from motet.core.orchestration.turn import _coerce_reasoning_effort

    for rung in REASONING_EFFORT_LADDER:
        assert _coerce_reasoning_effort(rung) == rung


def test_orchestration_coercion_falls_back_for_unusable_values() -> None:
    from motet.core.orchestration.turn import _coerce_reasoning_effort

    assert _coerce_reasoning_effort(None) == "medium"
    assert _coerce_reasoning_effort("banana") == "medium"
