"""
Motet - Turn Ownership Classifier Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Unit tests for the shared turn ownership classifier (issue #157 / ADR-0125
    deviation 5). Pure policy table tests — no Redis or loop execution.

Usage:
    pytest tests/unit/core/test_turn_ownership.py
"""

from __future__ import annotations

import pytest

from motet.core.checkpoints import (
    TurnOwnership,
    call_tool_names,
    classify_turn_ownership,
    split_calls_by_ownership,
)


@pytest.mark.parametrize(
    "calls,external,expected",
    [
        ([], {"client_edit"}, TurnOwnership.EXECUTE),
        (["read_file"], set(), TurnOwnership.EXECUTE),
        (["read_file"], {"client_edit"}, TurnOwnership.EXECUTE),
        (["client_edit"], {"client_edit"}, TurnOwnership.HANDBACK_ALL),
        (
            ["read_file", "client_edit"],
            {"client_edit"},
            TurnOwnership.HANDBACK_ALL,
        ),
        (
            ["client_a", "client_b"],
            {"client_a", "client_b"},
            TurnOwnership.HANDBACK_ALL,
        ),
        ([""], {"client_edit"}, TurnOwnership.EXECUTE),
    ],
)
def test_classify_turn_ownership(calls, external, expected):
    assert classify_turn_ownership(calls, external_names=external) is expected


@pytest.mark.parametrize(
    "calls,external,expected",
    [
        ([], {"client_edit"}, TurnOwnership.EXECUTE),
        (["read_file"], set(), TurnOwnership.EXECUTE),
        (["read_file"], {"client_edit"}, TurnOwnership.EXECUTE),
        (["client_edit"], {"client_edit"}, TurnOwnership.HANDBACK_ALL),
        (
            ["read_file", "client_edit"],
            {"client_edit"},
            TurnOwnership.HANDBACK_ALL,
        ),
        (
            ["client_a", "client_b"],
            {"client_a", "client_b"},
            TurnOwnership.HANDBACK_ALL,
        ),
        ([""], {"client_edit"}, TurnOwnership.EXECUTE),
    ],
)
def test_calls_require_handback_matches_classifier(calls, external, expected):
    from motet.core.reasoning.react.loop_intents import calls_require_handback

    dicts = [{"tool_name": name} for name in calls]
    require = calls_require_handback(dicts, external_names=external)
    assert require is (expected is TurnOwnership.HANDBACK_ALL)


def test_call_tool_names_extracts_strip():
    assert call_tool_names(
        [{"tool_name": " a "}, {"tool_name": None}, {}]
    ) == ["a", "", ""]


def test_split_calls_by_ownership():
    motet_owned, external = split_calls_by_ownership(
        [
            {"tool_name": "read_file", "tool_call_id": "1"},
            {"tool_name": "client_edit", "tool_call_id": "2"},
            {"tool_name": "read_file", "tool_call_id": "3"},
        ],
        external_names={"client_edit"},
    )
    assert [c["tool_call_id"] for c in motet_owned] == ["1", "3"]
    assert [c["tool_call_id"] for c in external] == ["2"]
