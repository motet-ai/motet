"""
Motet - Unit tests for optional workflow dependencies (continue_on_failure)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-21

Description:
Unit tests for WorkflowExecutor._should_skip_step: a dependency declared
continue_on_failure is optional — dependents run even when the dependency
failed or is missing from context (e.g. dispatch-level errors). Required
dependencies keep the existing skip behavior.

Usage:
    pytest tests/unit/orchestration/test_workflow_optional_dependencies.py -q
"""

from __future__ import annotations

from types import SimpleNamespace

from motet.core.workflow.executor import WorkflowExecutor


def _step(step_id: str, dependencies=None, continue_on_failure=False):
    return SimpleNamespace(
        step_id=step_id,
        dependencies=dependencies or [],
        continue_on_failure=continue_on_failure,
        skip_condition=None,
    )


def _workflow(steps, context):
    return SimpleNamespace(
        workflow_id="wf-test",
        steps={s.step_id: s for s in steps},
        context=context,
    )


def test_required_dependency_missing_skips():
    dep = _step("prepare")
    consumer = _step("plan", dependencies=["prepare"])
    workflow = _workflow([dep, consumer], context={})
    should_skip, reason = WorkflowExecutor()._should_skip_step(consumer, workflow)
    assert should_skip is True
    assert "not found in context" in reason


def test_required_dependency_failed_skips():
    dep = _step("prepare")
    consumer = _step("plan", dependencies=["prepare"])
    workflow = _workflow(
        [dep, consumer], context={"prepare": {"status": "failed", "error": "x"}}
    )
    should_skip, reason = WorkflowExecutor()._should_skip_step(consumer, workflow)
    assert should_skip is True
    assert "failed with status" in reason


def test_optional_dependency_missing_does_not_skip():
    dep = _step("ingest_refs", continue_on_failure=True)
    consumer = _step("plan", dependencies=["ingest_refs"])
    workflow = _workflow([dep, consumer], context={})
    should_skip, reason = WorkflowExecutor()._should_skip_step(consumer, workflow)
    assert should_skip is False
    assert reason is None


def test_optional_dependency_failed_does_not_skip():
    dep = _step("ingest_refs", continue_on_failure=True)
    consumer = _step("plan", dependencies=["ingest_refs"])
    workflow = _workflow(
        [dep, consumer],
        context={"ingest_refs": {"status": "failed", "error": "boom"}},
    )
    should_skip, reason = WorkflowExecutor()._should_skip_step(consumer, workflow)
    assert should_skip is False
    assert reason is None


def test_mixed_dependencies_required_failure_still_skips():
    optional = _step("ingest_refs", continue_on_failure=True)
    required = _step("classify")
    consumer = _step("plan", dependencies=["ingest_refs", "classify"])
    workflow = _workflow(
        [optional, required, consumer],
        context={"classify": {"status": "error", "error": "x"}},
    )
    should_skip, reason = WorkflowExecutor()._should_skip_step(consumer, workflow)
    assert should_skip is True
    assert "classify" in reason
