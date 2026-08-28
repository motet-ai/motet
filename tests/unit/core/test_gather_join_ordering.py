"""
Motet - Gather/Join Submission-Order Regression Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-19

Description:
    Ensures GatherCommand._aggregate_all_results emits results in submission
    order so motet.join() positional unpacking is safe. Sets do not preserve
    insertion order; this was observed swapping tone/accuracy in the
    content-review.coordinate_reviews example bundle.

Dependencies:
    - pytest
    - unittest.mock
    - motet.core.commands.concurrency

Usage:
    pytest -q tests/unit/core/test_gather_join_ordering.py

Notes:
    - Pure unit tests; no Redis/Celery required.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from motet.core.commands.concurrency import GatherCommand


def test_aggregate_all_results_reorders_scrambled_completion_events() -> None:
    """Aggregation must key by command_id and emit child_commands order."""
    children = [
        SimpleNamespace(command_id="id-grammar", get_command_type=lambda: "content-review.review_grammar"),
        SimpleNamespace(command_id="id-tone", get_command_type=lambda: "content-review.review_tone"),
        SimpleNamespace(command_id="id-accuracy", get_command_type=lambda: "content-review.review_accuracy"),
    ]

    # Scrambled completion order (matches the content-review join bug)
    scrambled: List[Dict[str, Any]] = [
        {
            "command_id": "id-grammar",
            "command_type": "content-review.review_grammar",
            "status": "success",
            "result": {
                "status": "success",
                "data": {"perspective": "grammar"},
                "metadata": {},
            },
        },
        {
            "command_id": "id-accuracy",
            "command_type": "content-review.review_accuracy",
            "status": "success",
            "result": {
                "status": "success",
                "data": {"perspective": "accuracy"},
                "metadata": {},
            },
        },
        {
            "command_id": "id-tone",
            "command_type": "content-review.review_tone",
            "status": "success",
            "result": {
                "status": "success",
                "data": {"perspective": "tone"},
                "metadata": {},
            },
        },
    ]

    gather = GatherCommand.__new__(GatherCommand)
    aggregated = GatherCommand._aggregate_all_results(gather, scrambled, children)  # type: ignore[arg-type]

    assert aggregated["successful"] == 3
    assert aggregated["failed"] == 0
    assert [r["metadata"]["command_id"] for r in aggregated["results"]] == [
        "id-grammar",
        "id-tone",
        "id-accuracy",
    ]
    assert [r["data"]["perspective"] for r in aggregated["results"]] == [
        "grammar",
        "tone",
        "accuracy",
    ]


def test_aggregate_all_results_keeps_failures_in_submission_slots() -> None:
    """Failures stay in-place so result[i] still matches child_commands[i]."""
    children = [
        SimpleNamespace(command_id="id-a", get_command_type=lambda: "cmd.a"),
        SimpleNamespace(command_id="id-b", get_command_type=lambda: "cmd.b"),
        SimpleNamespace(command_id="id-c", get_command_type=lambda: "cmd.c"),
    ]
    results = [
        {
            "command_id": "id-a",
            "command_type": "cmd.a",
            "status": "success",
            "result": {"status": "success", "data": {"v": "a"}, "metadata": {}},
        },
        {
            "command_id": "id-c",
            "command_type": "cmd.c",
            "status": "success",
            "result": {"status": "success", "data": {"v": "c"}, "metadata": {}},
        },
        # id-b missing → timeout stub slot
    ]

    gather = GatherCommand.__new__(GatherCommand)
    aggregated = GatherCommand._aggregate_all_results(gather, results, children)  # type: ignore[arg-type]

    assert aggregated["total_commands"] == 3
    assert aggregated["successful"] == 2
    assert aggregated["failed"] == 1
    assert [r["status"] for r in aggregated["results"]] == ["success", "error", "success"]
    assert aggregated["results"][0]["data"]["v"] == "a"
    assert aggregated["results"][2]["data"]["v"] == "c"
    assert aggregated["results"][1]["error"]["type"] == "MissingResult"
    assert len(aggregated["failed_commands"]) == 1
    assert aggregated["failed_commands"][0]["metadata"]["command_id"] == "id-b"
