"""
Motet - Workflow Executor Resume Mixin

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-06

Description:
    Leave-pause path for WorkflowExecutor (issue #149 / #189): apply tagged
    resume payloads, rebuild Workflow instances from checkpoints, continue
    execution, and unwind parent frames when a nested child completes.

Dependencies:
    - motet.core.workflow.checkpoint: claim / load / suspend models
    - motet.core.checkpoints.redis_store: shared handback observation validation
    - copy / uuid: snapshot and interaction id generation

Usage:
    class WorkflowExecutor(WorkflowSuspendMixin, WorkflowResumeMixin, WorkflowStepsMixin):
        ...

Notes:
    - Resume may re-enter execute_workflow on the facade.
    - Re-suspend after a nested child pause uses WorkflowSuspendMixin._suspend_workflow.
    - Do not import reasoning from this module.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from uuid import uuid4

if TYPE_CHECKING:
    import structlog


class WorkflowResumeMixin:
    """Resume / parent-continue helpers for WorkflowExecutor (leave pause)."""

    # Provided by WorkflowExecutor.__init__ / sibling mixins (pyright mixin surface).
    logger: "structlog.stdlib.BoundLogger"

    if TYPE_CHECKING:
        def _suspend_workflow(
            self,
            workflow: Any,
            motet: Any,
            *,
            completed_step_ids: List[str],
            pending_step_ids: List[str],
            step_results: Dict[str, Any],
            suspend_reason: str,
            pending_interactions: List[Any],
            workflow_run_id: Optional[str] = None,
            resume_epoch: int = 0,
            run_created_at: Optional[float] = None,
            child_workflow_run_id: Optional[str] = None,
            blocked_step_id: Optional[str] = None,
            parent_workflow_run_id: Optional[str] = None,
        ) -> Dict[str, Any]: ...

        def execute_workflow(
            self,
            workflow: Any,
            motet: Any,
            *,
            completed_step_ids: Optional[List[str]] = None,
            step_results: Optional[Dict[str, Any]] = None,
            workflow_run_id: Optional[str] = None,
            skip_input_validation: bool = False,
            resume_epoch: int = 0,
            run_created_at: Optional[float] = None,
        ) -> Dict[str, Any]: ...

    def resume_from_checkpoint(
        self,
        checkpoint,
        motet,
        *,
        kind: str,
        observations: Optional[List[Dict[str, Any]]] = None,
        answers: Optional[Dict[str, Any]] = None,
        decision: Optional[str] = None,
        edited_parameters: Optional[Dict[str, Any]] = None,
        auth_status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Continue a paused workflow after a tagged resume payload (issue #149).

        When the checkpoint waits on a nested child (``child_workflow_run_id``),
        resume the leaf first if the payload targets it; otherwise require the
        child to already be terminal and continue the parent (#189).
        """
        from . import Workflow, WorkflowStatus
        from .checkpoint import (
            WorkflowRunStatus,
            WorkflowSuspendReason,
            load_workflow_checkpoint,
        )

        # Parent waiting on child: if payload is for the leaf, resume leaf then
        # auto-continue parent in the same command.
        child_run_id = str(getattr(checkpoint, "child_workflow_run_id", None) or "").strip()
        if child_run_id:
            child = load_workflow_checkpoint(
                tenant_id=checkpoint.tenant_id,
                motet_id=checkpoint.motet_id,
                workflow_run_id=child_run_id,
            )
            if child is not None and not child.is_terminal():
                # Resume targets the leaf that owns pending interactions.
                from motet.core.workflow.checkpoint import (
                    claim_workflow_run_for_resume,
                    release_workflow_run_to_paused,
                )

                claimed_child = claim_workflow_run_for_resume(
                    tenant_id=checkpoint.tenant_id,
                    motet_id=checkpoint.motet_id,
                    workflow_run_id=child_run_id,
                )
                if claimed_child is None:
                    claimed_child = child
                try:
                    child_result = self.resume_from_checkpoint(
                        claimed_child,
                        motet,
                        kind=kind,
                        observations=observations,
                        answers=answers,
                        decision=decision,
                        edited_parameters=edited_parameters,
                        auth_status=auth_status,
                    )
                except ValueError:
                    release_workflow_run_to_paused(claimed_child)
                    # Also release parent claim (caller claimed parent already).
                    raise
                if isinstance(child_result, dict) and (
                    child_result.get("suspended")
                    or child_result.get("status") == "suspended"
                ):
                    # Child re-suspended: keep parent paused on child; bubble leaf.
                    from .checkpoint import PendingInteraction, WorkflowSuspendReason

                    pending_raw = list(child_result.get("pending_interactions") or [])
                    interactions = []
                    for item in pending_raw:
                        if not isinstance(item, dict):
                            continue
                        kind_raw = item.get("kind") or child_result.get("suspend_reason")
                        try:
                            ikind = WorkflowSuspendReason(str(kind_raw))
                        except ValueError:
                            ikind = WorkflowSuspendReason.HANDBACK_TOOLS
                        interactions.append(
                            PendingInteraction(
                                interaction_id=str(
                                    item.get("interaction_id") or uuid4().hex
                                ),
                                kind=ikind,
                                step_id=str(checkpoint.blocked_step_id or ""),
                                tool_name=item.get("tool_name"),
                                parameters=dict(item.get("parameters") or {}),
                                interaction_schema=item.get("schema")
                                or item.get("interaction_schema"),
                                prompt=item.get("prompt"),
                                auth_challenge=item.get("auth_challenge"),
                            )
                        )
                    return self._suspend_workflow(
                        self._workflow_from_checkpoint(checkpoint),
                        motet,
                        completed_step_ids=list(checkpoint.completed_step_ids),
                        pending_step_ids=list(checkpoint.pending_step_ids),
                        step_results=copy.deepcopy(checkpoint.step_results),
                        suspend_reason=str(
                            child_result.get("suspend_reason")
                            or checkpoint.suspend_reason
                        ),
                        pending_interactions=interactions,
                        workflow_run_id=checkpoint.workflow_run_id,
                        resume_epoch=checkpoint.resume_epoch,
                        run_created_at=checkpoint.created_at,
                        child_workflow_run_id=child_result.get("leaf_workflow_run_id")
                        or child_result.get("workflow_run_id")
                        or child_run_id,
                        blocked_step_id=checkpoint.blocked_step_id,
                        parent_workflow_run_id=checkpoint.parent_workflow_run_id,
                    )
                # Child completed: merge into blocked step and continue parent.
                blocked = str(checkpoint.blocked_step_id or "").strip()
                if blocked:
                    checkpoint.context[blocked] = (
                        child_result.get("data")
                        if isinstance(child_result, dict) and "data" in child_result
                        else child_result
                    )
                    checkpoint.step_results[blocked] = {
                        "status": "completed",
                        "data": checkpoint.context[blocked],
                    }
                    if blocked not in checkpoint.completed_step_ids:
                        checkpoint.completed_step_ids.append(blocked)
                checkpoint.child_workflow_run_id = None
                checkpoint.blocked_step_id = None
                checkpoint.pending_interactions = []
                checkpoint.suspend_reason = WorkflowSuspendReason.NONE
                # Fall through to continue parent without re-applying leaf payload.
                workflow = self._workflow_from_checkpoint(checkpoint)
                return self.execute_workflow(
                    workflow,
                    motet,
                    completed_step_ids=list(checkpoint.completed_step_ids),
                    step_results=copy.deepcopy(checkpoint.step_results),
                    workflow_run_id=checkpoint.workflow_run_id,
                    skip_input_validation=True,
                    resume_epoch=int(checkpoint.resume_epoch or 0),
                    run_created_at=checkpoint.created_at,
                )

        recorded = (
            checkpoint.suspend_reason.value
            if isinstance(checkpoint.suspend_reason, WorkflowSuspendReason)
            else str(checkpoint.suspend_reason)
        )
        if kind != recorded and recorded != WorkflowSuspendReason.NONE.value:
            # Allow confirmation resume after a mixed level that primarily
            # surfaced as handback when confirmation was co-pending — still
            # require kind to match an interaction kind present on the checkpoint.
            interaction_kinds = {
                (
                    i.kind.value if isinstance(i.kind, WorkflowSuspendReason) else str(i.kind)
                )
                for i in checkpoint.pending_interactions
            }
            if kind not in interaction_kinds and kind != recorded:
                raise ValueError(
                    f"resume_workflow: kind '{kind}' does not match checkpoint "
                    f"suspend_reason '{recorded}'"
                )

        self._apply_resume_payload(
            checkpoint,
            kind=kind,
            observations=observations or [],
            answers=answers,
            decision=decision,
            edited_parameters=edited_parameters,
            auth_status=auth_status,
        )

        workflow = self._workflow_from_checkpoint(checkpoint)

        # Motet siblings in the suspended level still need to run; handback /
        # elicitation / confirmation steps were completed by _apply_resume_payload.
        result = self.execute_workflow(
            workflow,
            motet,
            completed_step_ids=list(checkpoint.completed_step_ids),
            step_results=copy.deepcopy(checkpoint.step_results),
            workflow_run_id=checkpoint.workflow_run_id,
            skip_input_validation=True,
            resume_epoch=int(checkpoint.resume_epoch or 0),
            run_created_at=checkpoint.created_at,
        )
        return self._maybe_continue_parent_after_child(checkpoint, motet, result)

    def _maybe_continue_parent_after_child(
        self,
        child_checkpoint,
        motet,
        child_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """When a nested child completes, claim and continue its paused parent (#189)."""
        if not isinstance(child_result, dict):
            return child_result
        if child_result.get("suspended") or child_result.get("status") == "suspended":
            return child_result
        if child_result.get("status") not in ("completed", "success"):
            return child_result
        parent_id = str(
            getattr(child_checkpoint, "parent_workflow_run_id", None) or ""
        ).strip()
        if not parent_id:
            return child_result

        from .checkpoint import (
            WorkflowSuspendReason,
            claim_workflow_run_for_resume,
            load_workflow_checkpoint,
            release_workflow_run_to_paused,
        )

        parent = load_workflow_checkpoint(
            tenant_id=child_checkpoint.tenant_id,
            motet_id=child_checkpoint.motet_id,
            workflow_run_id=parent_id,
        )
        if parent is None or not parent.child_workflow_run_id:
            return child_result
        if str(parent.child_workflow_run_id) != str(child_checkpoint.workflow_run_id):
            return child_result

        claimed = claim_workflow_run_for_resume(
            tenant_id=child_checkpoint.tenant_id,
            motet_id=child_checkpoint.motet_id,
            workflow_run_id=parent_id,
        )
        if claimed is None:
            return child_result
        blocked = str(claimed.blocked_step_id or "").strip()
        if blocked:
            claimed.context[blocked] = child_result
            claimed.step_results[blocked] = {
                "status": "completed",
                "data": child_result,
            }
            if blocked not in claimed.completed_step_ids:
                claimed.completed_step_ids.append(blocked)
        claimed.child_workflow_run_id = None
        claimed.blocked_step_id = None
        claimed.pending_interactions = []
        claimed.suspend_reason = WorkflowSuspendReason.NONE
        try:
            parent_result = self.execute_workflow(
                self._workflow_from_checkpoint(claimed),
                motet,
                completed_step_ids=list(claimed.completed_step_ids),
                step_results=copy.deepcopy(claimed.step_results),
                workflow_run_id=claimed.workflow_run_id,
                skip_input_validation=True,
                resume_epoch=int(claimed.resume_epoch or 0),
                run_created_at=claimed.created_at,
            )
        except Exception:
            release_workflow_run_to_paused(claimed)
            raise
        return self._maybe_continue_parent_after_child(claimed, motet, parent_result)

    def _workflow_from_checkpoint(self, checkpoint):
        from . import Workflow, WorkflowStatus

        return Workflow.from_dict(
            {
                "workflow_id": checkpoint.workflow_id,
                "name": checkpoint.workflow_name,
                "description": checkpoint.description,
                "required_inputs": checkpoint.required_inputs,
                "input_parameters": checkpoint.input_parameters,
                "output_field": checkpoint.output_field,
                "presentation": checkpoint.presentation,
                "use_for": checkpoint.use_for,
                "handback_tools": checkpoint.handback_tools,
                "steps": {
                    s.get("step_id"): s
                    for s in (checkpoint.workflow_steps or [])
                    if s.get("step_id")
                },
                "execution_order": checkpoint.execution_order,
                "context": copy.deepcopy(checkpoint.context),
                "status": WorkflowStatus.RUNNING.value,
                "durable": True,
            }
        )

    def _apply_resume_payload(
        self,
        checkpoint,
        *,
        kind: str,
        observations: List[Dict[str, Any]],
        answers: Optional[Dict[str, Any]],
        decision: Optional[str],
        edited_parameters: Optional[Dict[str, Any]],
        auth_status: Optional[str],
    ) -> None:
        from motet.core.checkpoints.redis_store import validate_handback_observations
        from .checkpoint import WorkflowSuspendReason

        recorded_ids = {
            str(i.interaction_id)
            for i in checkpoint.pending_interactions
            if i.interaction_id
        }

        if kind == WorkflowSuspendReason.HANDBACK_TOOLS.value:
            handback_ids = {
                str(i.interaction_id)
                for i in checkpoint.pending_interactions
                if (
                    i.kind == WorkflowSuspendReason.HANDBACK_TOOLS
                    or str(i.kind) == WorkflowSuspendReason.HANDBACK_TOOLS.value
                )
            }
            by_id = validate_handback_observations(
                handback_ids,
                observations,
                error_prefix="resume_workflow",
            )
            for interaction in checkpoint.pending_interactions:
                if str(interaction.interaction_id) not in by_id:
                    continue
                obs = by_id[str(interaction.interaction_id)]
                content = obs.get("content", obs.get("result", ""))
                checkpoint.context[interaction.step_id] = {
                    "status": "success",
                    "result": content,
                    "tool_call_id": interaction.interaction_id,
                    "tool_name": interaction.tool_name,
                }
                checkpoint.step_results[interaction.step_id] = {
                    "status": "success",
                    "data": checkpoint.context[interaction.step_id],
                }
                if interaction.step_id not in checkpoint.completed_step_ids:
                    checkpoint.completed_step_ids.append(interaction.step_id)

        elif kind == WorkflowSuspendReason.ELICITATION.value:
            if not answers or not isinstance(answers, dict):
                raise ValueError("resume_workflow: elicitation resume requires answers dict")
            for interaction in checkpoint.pending_interactions:
                if interaction.kind not in (
                    WorkflowSuspendReason.ELICITATION,
                    WorkflowSuspendReason.ELICITATION.value,
                ) and str(interaction.kind) != WorkflowSuspendReason.ELICITATION.value:
                    continue
                # When multiple elicitation interactions, answers may be keyed by step_id.
                step_answers = answers.get(interaction.step_id, answers)
                checkpoint.context[interaction.step_id] = {
                    "status": "success",
                    "answers": step_answers,
                }
                checkpoint.step_results[interaction.step_id] = {
                    "status": "success",
                    "data": checkpoint.context[interaction.step_id],
                }
                if interaction.step_id not in checkpoint.completed_step_ids:
                    checkpoint.completed_step_ids.append(interaction.step_id)

        elif kind == WorkflowSuspendReason.CONFIRMATION.value:
            dec = (decision or "").strip().lower()
            if dec not in ("approve", "reject"):
                raise ValueError(
                    "resume_workflow: confirmation resume requires decision=approve|reject"
                )
            for interaction in checkpoint.pending_interactions:
                if str(interaction.kind) not in (
                    WorkflowSuspendReason.CONFIRMATION.value,
                    WorkflowSuspendReason.CONFIRMATION,
                ) and interaction.kind != WorkflowSuspendReason.CONFIRMATION:
                    continue
                if dec == "reject":
                    checkpoint.context[interaction.step_id] = {
                        "status": "rejected",
                        "decision": "reject",
                    }
                    checkpoint.step_results[interaction.step_id] = {
                        "status": "failed",
                        "error": "confirmation rejected",
                        "data": checkpoint.context[interaction.step_id],
                    }
                    if interaction.step_id not in checkpoint.completed_step_ids:
                        checkpoint.completed_step_ids.append(interaction.step_id)
                else:
                    # Approved: leave step incomplete so Motet executes it on continue.
                    if edited_parameters:
                        # Stash edits for the upcoming Motet tool call.
                        for step_dict in checkpoint.workflow_steps:
                            if step_dict.get("step_id") == interaction.step_id:
                                cmd = dict(step_dict.get("command_data") or {})
                                params = dict(cmd.get("parameters") or {})
                                params.update(edited_parameters)
                                cmd["parameters"] = params
                                step_dict["command_data"] = cmd
                                # Clear confirmation so we don't re-suspend.
                                step_dict["requires_confirmation"] = False
                    else:
                        for step_dict in checkpoint.workflow_steps:
                            if step_dict.get("step_id") == interaction.step_id:
                                step_dict["requires_confirmation"] = False

        elif kind == WorkflowSuspendReason.OAUTH.value:
            status = (auth_status or "").strip().lower()
            if status not in ("completed", "failed"):
                raise ValueError(
                    "resume_workflow: oauth resume requires auth_status=completed|failed"
                )
            for interaction in checkpoint.pending_interactions:
                if str(interaction.kind) not in (
                    WorkflowSuspendReason.OAUTH.value,
                    WorkflowSuspendReason.OAUTH,
                ) and interaction.kind != WorkflowSuspendReason.OAUTH:
                    continue
                if status == "failed":
                    checkpoint.context[interaction.step_id] = {
                        "status": "failed",
                        "error": "oauth authorization failed",
                    }
                    checkpoint.step_results[interaction.step_id] = {
                        "status": "failed",
                        "error": "oauth authorization failed",
                    }
                    if interaction.step_id not in checkpoint.completed_step_ids:
                        checkpoint.completed_step_ids.append(interaction.step_id)
                # completed: leave step incomplete so Motet retries the tool.

        elif kind == WorkflowSuspendReason.OPERATOR.value:
            # Operator pause has no pending interactions — just continue the graph.
            pass

        else:
            raise ValueError(f"resume_workflow: unsupported kind '{kind}'")

        # Clear pending interactions after successful apply.
        checkpoint.pending_interactions = []
        checkpoint.suspend_reason = WorkflowSuspendReason.NONE
        checkpoint.pending_step_ids = [
            sid
            for sid in checkpoint.pending_step_ids
            if sid not in checkpoint.completed_step_ids
        ]
