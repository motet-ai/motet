"""
Motet - Workflow Executor

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Workflow execution facade. The executor instance holds no
    per-run fields — cursor and durability live on the ``Workflow`` object and
    Redis ``WorkflowCheckpoint`` records — but runs themselves may pause,
    resume, and persist progress across workers (issue #149 / #189).
    Durable runs push ``workflow_run_id`` onto ``cancel_scopes``
    so nested commands honor workflow cancel via the shared control key.

    Orchestrates topological levels via ``execute_workflow``; step dispatch
    lives in ``executor_steps.WorkflowStepsMixin``, enter-pause in
    ``executor_suspend.WorkflowSuspendMixin``, and leave-pause in
    ``executor_resume.WorkflowResumeMixin``.

Dependencies:
    - executor_suspend / executor_resume / executor_steps: mixin modules
    - structlog: Structured logging

Usage:
    from motet.core.workflow import WorkflowExecutor

    executor = WorkflowExecutor()
    result = executor.execute_workflow(workflow, motet)

Notes:
    - Mixins keep a single ``self`` surface; import WorkflowExecutor only.
    - See package README Module Layout for the split rationale.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from ..workers.concurrency_primitives import worker_sleep  # noqa: F401 — test patch target
from .executor_resume import WorkflowResumeMixin
from .executor_steps import WorkflowStepsMixin
from .executor_suspend import WorkflowSuspendMixin
from .utils import substitute_parameters

# Re-export for callers / tests that patch ``executor.worker_sleep``.
__all__ = ["WorkflowExecutor", "worker_sleep"]


class WorkflowExecutor(WorkflowSuspendMixin, WorkflowResumeMixin, WorkflowStepsMixin):
    """Workflow execution service (instance-local; run state is external)."""

    def __init__(self):
        import structlog

        self.logger = structlog.get_logger(__name__)

    def execute_workflow(
        self,
        workflow,
        motet,
        *,
        completed_step_ids: Optional[List[str]] = None,
        step_results: Optional[Dict[str, Any]] = None,
        workflow_run_id: Optional[str] = None,
        skip_input_validation: bool = False,
        resume_epoch: int = 0,
        run_created_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        from . import WorkflowStatus
        from .checkpoint import WorkflowRunStatus

        workflow.status = WorkflowStatus.RUNNING
        if workflow.started_at is None:
            workflow.started_at = datetime.utcnow()

        if not skip_input_validation:
            if workflow.input_parameters:
                for param_name, param_schema in workflow.input_parameters.items():
                    if param_name not in workflow.context:
                        if "default" in param_schema:
                            workflow.context[param_name] = param_schema["default"]
                        elif "default_expression" in param_schema:
                            template_dict = {"value": param_schema["default_expression"]}
                            substituted = substitute_parameters(template_dict, workflow.context)
                            workflow.context[param_name] = substituted["value"]

            if workflow.required_inputs:
                missing_inputs = [
                    req for req in workflow.required_inputs if req not in workflow.context
                ]
                if missing_inputs:
                    workflow.status = WorkflowStatus.FAILED
                    raise ValueError(
                        f"Workflow '{workflow.workflow_id}' requires the following inputs that were not provided: "
                        f"{', '.join(missing_inputs)}. Available context keys: {list(workflow.context.keys())}"
                    )

        # Propagate workflow model identity into MotetContext metadata so nested
        # tool_execution / core.web_search can use the LLM-native search path.
        self._stamp_model_metadata_from_context(workflow, motet)

        done = set(completed_step_ids or [])
        results: Dict[str, Any] = dict(step_results or {})

        # A run is tracked once it has an id: either it already suspended (and a
        # checkpoint exists) or the workflow opted into durability. Untracked runs
        # write nothing, because a progress record carries the whole context and
        # step_results — paying that on every short workflow is not worth it.
        run_id = workflow_run_id
        if run_id is None and bool(getattr(workflow, "durable", False)):
            run_id = f"wfrun-{uuid4().hex}"
        # Nesting (#189): ensure a run id exists before any nested workflow_execution
        # so a child suspend can pause the parent with a durable pointer.
        if run_id is None and any(
            (getattr(s, "command_type", "") or "").strip()
            in (
                "workflow_execution",
                "core.workflow_execution",
                "full_workflow_execution",
                "core.full_workflow_execution",
            )
            for s in workflow.steps.values()
        ):
            run_id = f"wfrun-{uuid4().hex}"
        parent_run_id = None
        if isinstance(getattr(motet, "metadata", None), dict):
            parent_run_id = motet.metadata.get("parent_workflow_run_id")
            if run_id:
                motet.metadata["workflow_run_id"] = run_id
                motet.metadata["current_workflow_run_id"] = run_id
                if hasattr(motet, "push_cancel_scope"):
                    motet.push_cancel_scope(run_id)

        # Publish a running record as soon as we have a run id so operators can
        # pause/cancel before the first level completes.
        if run_id and not (completed_step_ids or step_results):
            self._persist_run_state(
                workflow,
                motet,
                workflow_run_id=run_id,
                status=WorkflowRunStatus.RUNNING,
                completed_step_ids=[],
                pending_step_ids=list(workflow.steps.keys()),
                step_results={},
                resume_epoch=resume_epoch,
                run_created_at=run_created_at,
                parent_workflow_run_id=parent_run_id,
            )

        try:
            for level in workflow.execution_order:
                remaining = [sid for sid in level if sid not in done]
                if not remaining:
                    continue

                if run_id:
                    control_result = self._honor_operator_control(
                        workflow,
                        motet,
                        workflow_run_id=run_id,
                        completed_step_ids=sorted(done),
                        pending_step_ids=[
                            sid for sid in workflow.steps if sid not in done
                        ],
                        step_results=results,
                        resume_epoch=resume_epoch,
                        run_created_at=run_created_at,
                        parent_workflow_run_id=parent_run_id,
                    )
                    if control_result is not None:
                        return control_result

                suspend_ids, suspend_reason = self._classify_level_suspend(workflow, remaining)
                if suspend_ids and suspend_reason:
                    # Whole-level suspend: Motet siblings wait until resume (Phase E).
                    interactions = self._build_pending_interactions(
                        workflow, suspend_ids, suspend_reason
                    )
                    # pending_step_ids = all remaining in level (handback + Motet siblings)
                    return self._suspend_workflow(
                        workflow,
                        motet,
                        completed_step_ids=sorted(done),
                        pending_step_ids=remaining,
                        step_results=results,
                        suspend_reason=suspend_reason,
                        pending_interactions=interactions,
                        workflow_run_id=run_id,
                        resume_epoch=resume_epoch,
                        run_created_at=run_created_at,
                        parent_workflow_run_id=parent_run_id,
                    )

                if len(remaining) > 1:
                    level_results = self._execute_level_parallel(workflow, remaining, motet)
                else:
                    level_results = self._execute_level_sequential(workflow, remaining, motet)

                # Nested child workflow suspended (issue #189): parent pauses on child.
                child_suspend = self._maybe_suspend_for_nested_child(
                    workflow,
                    motet,
                    remaining,
                    level_results,
                    done,
                    results,
                    run_id,
                    resume_epoch=resume_epoch,
                    run_created_at=run_created_at,
                    parent_workflow_run_id=parent_run_id,
                )
                if child_suspend is not None:
                    return child_suspend

                # OAuth: Motet-owned MCP step returned auth_required → pause.
                oauth_suspend = self._maybe_suspend_for_oauth(
                    workflow,
                    motet,
                    remaining,
                    level_results,
                    done,
                    results,
                    run_id,
                    resume_epoch=resume_epoch,
                    run_created_at=run_created_at,
                    parent_workflow_run_id=parent_run_id,
                )
                if oauth_suspend is not None:
                    return oauth_suspend

                for step_id, result in level_results.items():
                    self._merge_step_result(workflow, step_id, result)
                    results[step_id] = result
                    done.add(step_id)

                # Progress checkpoint: a crash or retry after this point resumes
                # from here instead of re-running steps that already landed.
                if run_id:
                    self._persist_run_state(
                        workflow,
                        motet,
                        workflow_run_id=run_id,
                        status=WorkflowRunStatus.RUNNING,
                        completed_step_ids=sorted(done),
                        pending_step_ids=[
                            sid for sid in workflow.steps if sid not in done
                        ],
                        step_results=results,
                        resume_epoch=resume_epoch,
                        run_created_at=run_created_at,
                        parent_workflow_run_id=parent_run_id,
                    )

            workflow.status = WorkflowStatus.COMPLETED
            workflow.completed_at = datetime.utcnow()
            result: Dict[str, Any] = {
                "status": "completed",
                "step_results": results,
                "workflow_id": workflow.workflow_id,
                "workflow_name": workflow.name,
                "execution_time_ms": (
                    (workflow.completed_at - workflow.started_at).total_seconds() * 1000
                ),
            }
            if run_id:
                result["workflow_run_id"] = run_id
                self._persist_run_state(
                    workflow,
                    motet,
                    workflow_run_id=run_id,
                    status=WorkflowRunStatus.COMPLETED,
                    completed_step_ids=sorted(done),
                    pending_step_ids=[],
                    step_results=results,
                    resume_epoch=resume_epoch,
                    run_created_at=run_created_at,
                    parent_workflow_run_id=parent_run_id,
                )
            if workflow.output_field:
                result["output_field"] = workflow.output_field
            if workflow.presentation:
                result["presentation"] = workflow.presentation
            return result
        except Exception as e:
            workflow.status = WorkflowStatus.FAILED
            if run_id:
                self._persist_run_state(
                    workflow,
                    motet,
                    workflow_run_id=run_id,
                    status=WorkflowRunStatus.FAILED,
                    completed_step_ids=sorted(done),
                    pending_step_ids=[
                        sid for sid in workflow.steps if sid not in done
                    ],
                    step_results=results,
                    resume_epoch=resume_epoch,
                    run_created_at=run_created_at,
                    last_error=str(e),
                    parent_workflow_run_id=parent_run_id,
                )
            raise
