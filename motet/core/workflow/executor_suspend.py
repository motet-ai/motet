"""
Motet - Workflow Executor Suspend Mixin

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-06

Description:
    Enter-pause path for WorkflowExecutor (issue #149 / #189): classify level
    suspends, build/persist WorkflowCheckpoint records, honor operator
    pause/cancel signals, and pause for nested-child or OAuth outcomes.

Dependencies:
    - motet.core.workflow.checkpoint: store / suspend models
    - copy / uuid: snapshot and interaction id generation

Usage:
    class WorkflowExecutor(WorkflowSuspendMixin, WorkflowResumeMixin, WorkflowStepsMixin):
        ...

Notes:
    - Pause writes are fail-loud; progress / terminal writes are best-effort.
    - Nested child suspend bubbles leaf pending interactions onto the parent.
    - Do not import reasoning from this module.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from uuid import uuid4

from .utils import substitute_parameters

if TYPE_CHECKING:
    import structlog


class WorkflowSuspendMixin:
    """Suspend / checkpoint helpers for WorkflowExecutor (enter pause)."""

    # Provided by WorkflowExecutor.__init__ / sibling mixins (pyright mixin surface).
    logger: "structlog.stdlib.BoundLogger"

    if TYPE_CHECKING:
        def _merge_step_result(self, workflow: Any, step_id: str, result: Any) -> None: ...

    @staticmethod
    def _step_tool_name(step) -> str:
        data = step.command_data or {}
        return str(
            data.get("tool_name")
            or (step.parameters or {}).get("tool_name")
            or step.name
            or step.step_id
        )

    @staticmethod
    def _step_tool_parameters(step, context: Dict[str, Any]) -> Dict[str, Any]:
        data = substitute_parameters(copy.deepcopy(step.command_data or {}), context)
        params = data.get("parameters")
        if isinstance(params, dict):
            return params
        # Legacy: command_data may itself be the parameters map.
        if "tool_name" in data:
            return {k: v for k, v in data.items() if k != "tool_name"}
        return dict(data) if isinstance(data, dict) else {}

    def _classify_level_suspend(
        self, workflow, level: List[str]
    ) -> Tuple[List[str], Optional[str]]:
        """
        Return (suspend_step_ids, suspend_reason) for a topological level.

        Handback / elicitation / confirmation steps cause the whole level to
        suspend before any Motet sibling runs (issue #149 Phase E join rule).
        """
        from .checkpoint import WorkflowSuspendReason

        handback: List[str] = []
        elicitation: List[str] = []
        confirmation: List[str] = []
        for sid in level:
            step = workflow.steps[sid]
            if getattr(step, "step_type", "command") == "elicitation":
                elicitation.append(sid)
            elif getattr(step, "ownership", "motet") == "handback":
                handback.append(sid)
            elif getattr(step, "requires_confirmation", False):
                confirmation.append(sid)
        if handback:
            return handback + elicitation + confirmation, WorkflowSuspendReason.HANDBACK_TOOLS.value
        if elicitation:
            return elicitation + confirmation, WorkflowSuspendReason.ELICITATION.value
        if confirmation:
            return confirmation, WorkflowSuspendReason.CONFIRMATION.value
        return [], None

    def _build_pending_interactions(
        self,
        workflow,
        suspend_step_ids: List[str],
        suspend_reason: str,
    ) -> List[Any]:
        from .checkpoint import PendingInteraction, WorkflowSuspendReason

        reason = WorkflowSuspendReason(suspend_reason)
        pending = []
        for sid in suspend_step_ids:
            step = workflow.steps[sid]
            step_type = getattr(step, "step_type", "command")
            if step_type == "elicitation":
                pending.append(
                    PendingInteraction(
                        interaction_id=f"elicit-{uuid4().hex}",
                        kind=WorkflowSuspendReason.ELICITATION,
                        step_id=sid,
                        interaction_schema=step.elicitation_schema
                        or (step.command_data or {}).get("schema"),
                        prompt=step.elicitation_prompt
                        or (step.command_data or {}).get("prompt")
                        or step.name,
                    )
                )
            elif getattr(step, "ownership", "motet") == "handback":
                pending.append(
                    PendingInteraction(
                        interaction_id=f"call_{uuid4().hex[:24]}",
                        kind=WorkflowSuspendReason.HANDBACK_TOOLS,
                        step_id=sid,
                        tool_name=self._step_tool_name(step),
                        parameters=self._step_tool_parameters(step, workflow.context),
                    )
                )
            elif getattr(step, "requires_confirmation", False):
                pending.append(
                    PendingInteraction(
                        interaction_id=f"confirm-{uuid4().hex}",
                        kind=WorkflowSuspendReason.CONFIRMATION,
                        step_id=sid,
                        tool_name=self._step_tool_name(step),
                        parameters=self._step_tool_parameters(step, workflow.context),
                        prompt=f"Confirm execution of {self._step_tool_name(step)}",
                    )
                )
            else:
                pending.append(
                    PendingInteraction(
                        interaction_id=f"pause-{uuid4().hex}",
                        kind=reason,
                        step_id=sid,
                    )
                )
        return pending

    def _build_checkpoint(
        self,
        workflow,
        motet,
        *,
        workflow_run_id: str,
        status: Any,
        completed_step_ids: List[str],
        pending_step_ids: List[str],
        step_results: Dict[str, Any],
        suspend_reason: Any = None,
        pending_interactions: Optional[List[Any]] = None,
        resume_epoch: int = 0,
        last_error: Optional[str] = None,
        run_created_at: Optional[float] = None,
        child_workflow_run_id: Optional[str] = None,
        blocked_step_id: Optional[str] = None,
        parent_workflow_run_id: Optional[str] = None,
    ):
        """Snapshot the run: identity, graph definition, cursor, and lifecycle.

        The graph definition travels with the checkpoint so a resume is unaffected
        by registry edits between pause and resume, and so confirmation edits
        (cleared ``requires_confirmation``, edited parameters) survive a restart.
        """
        from .checkpoint import WorkflowCheckpoint, WorkflowSuspendReason

        fields: Dict[str, Any] = dict(
            workflow_run_id=workflow_run_id,
            motet_id=getattr(motet, "motet_id", None) or "default",
            tenant_id=getattr(motet, "tenant_id", None),
            principal_id=getattr(motet, "principal_id", None),
            task_id=getattr(motet, "task_id", None),
            conversation_id=getattr(motet, "conversation_id", None),
            workflow_id=workflow.workflow_id,
            workflow_name=workflow.name,
            description=workflow.description or "",
            required_inputs=workflow.required_inputs,
            input_parameters=workflow.input_parameters,
            output_field=workflow.output_field,
            presentation=workflow.presentation,
            use_for=workflow.use_for,
            handback_tools=workflow.handback_tools,
            workflow_steps=[
                {**step.to_dict(), "step_id": sid}
                for sid, step in workflow.steps.items()
            ],
            execution_order=list(workflow.execution_order or []),
            completed_step_ids=list(completed_step_ids),
            pending_step_ids=list(pending_step_ids),
            context=copy.deepcopy(workflow.context),
            step_results=copy.deepcopy(step_results),
            suspend_reason=suspend_reason or WorkflowSuspendReason.NONE,
            pending_interactions=list(pending_interactions or []),
            status=status,
            resume_epoch=int(resume_epoch or 0),
            last_error=last_error,
            child_workflow_run_id=child_workflow_run_id,
            blocked_step_id=blocked_step_id,
            parent_workflow_run_id=parent_workflow_run_id,
        )
        if run_created_at is not None:
            fields["created_at"] = run_created_at
        return WorkflowCheckpoint(**fields)

    def _persist_run_state(
        self,
        workflow,
        motet,
        *,
        workflow_run_id: str,
        status: Any,
        completed_step_ids: List[str],
        pending_step_ids: List[str],
        step_results: Dict[str, Any],
        resume_epoch: int,
        last_error: Optional[str] = None,
        run_created_at: Optional[float] = None,
        child_workflow_run_id: Optional[str] = None,
        blocked_step_id: Optional[str] = None,
        parent_workflow_run_id: Optional[str] = None,
    ) -> None:
        """Write a non-pause run record (progress or terminal), best-effort."""
        from .checkpoint import store_workflow_progress

        checkpoint = self._build_checkpoint(
            workflow,
            motet,
            workflow_run_id=workflow_run_id,
            status=status,
            completed_step_ids=completed_step_ids,
            pending_step_ids=pending_step_ids,
            step_results=step_results,
            resume_epoch=resume_epoch,
            last_error=last_error,
            run_created_at=run_created_at,
            child_workflow_run_id=child_workflow_run_id,
            blocked_step_id=blocked_step_id,
            parent_workflow_run_id=parent_workflow_run_id,
        )
        store_workflow_progress(checkpoint)

    def _honor_operator_control(
        self,
        workflow,
        motet,
        *,
        workflow_run_id: str,
        completed_step_ids: List[str],
        pending_step_ids: List[str],
        step_results: Dict[str, Any],
        resume_epoch: int,
        run_created_at: Optional[float] = None,
        parent_workflow_run_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Honor a Redis pause/cancel signal at a level boundary (cooperative)."""
        from . import WorkflowStatus
        from .checkpoint import (
            WorkflowControlAction,
            WorkflowRunStatus,
            WorkflowSuspendReason,
            clear_workflow_run_control,
            peek_workflow_run_control,
        )

        tenant_id = getattr(motet, "tenant_id", None)
        motet_id = getattr(motet, "motet_id", None) or "default"
        signal = peek_workflow_run_control(
            tenant_id=tenant_id,
            motet_id=motet_id,
            workflow_run_id=workflow_run_id,
        )
        if not signal:
            return None

        action = str(signal.get("action") or "").strip().lower()
        clear_workflow_run_control(
            tenant_id=tenant_id,
            motet_id=motet_id,
            workflow_run_id=workflow_run_id,
        )

        if action == WorkflowControlAction.CANCEL.value:
            workflow.status = WorkflowStatus.CANCELLED
            self._persist_run_state(
                workflow,
                motet,
                workflow_run_id=workflow_run_id,
                status=WorkflowRunStatus.CANCELLED,
                completed_step_ids=completed_step_ids,
                pending_step_ids=pending_step_ids,
                step_results=step_results,
                resume_epoch=resume_epoch,
                run_created_at=run_created_at,
                parent_workflow_run_id=parent_workflow_run_id,
                last_error=str(signal.get("reason") or "cancelled by operator"),
            )
            self.logger.info(
                "workflow_run_cancelled",
                workflow_id=workflow.workflow_id,
                workflow_run_id=workflow_run_id,
                mode="cooperative",
            )
            return {
                "status": "cancelled",
                "workflow_id": workflow.workflow_id,
                "workflow_name": workflow.name,
                "workflow_run_id": workflow_run_id,
                "completed_step_ids": list(completed_step_ids),
                "pending_step_ids": list(pending_step_ids),
                "step_results": step_results,
                "resume_epoch": resume_epoch,
            }

        if action == WorkflowControlAction.PAUSE.value:
            return self._suspend_workflow(
                workflow,
                motet,
                completed_step_ids=completed_step_ids,
                pending_step_ids=pending_step_ids,
                step_results=step_results,
                suspend_reason=WorkflowSuspendReason.OPERATOR.value,
                pending_interactions=[],
                workflow_run_id=workflow_run_id,
                resume_epoch=resume_epoch,
                run_created_at=run_created_at,
                parent_workflow_run_id=parent_workflow_run_id,
            )

        self.logger.warning(
            "workflow_control_unknown_action",
            workflow_run_id=workflow_run_id,
            action=action,
        )
        return None

    def _suspend_workflow(
        self,
        workflow,
        motet,
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
    ) -> Dict[str, Any]:
        from . import WorkflowStatus
        from .checkpoint import (
            WorkflowRunStatus,
            WorkflowSuspendReason,
            store_workflow_checkpoint,
        )

        workflow.status = WorkflowStatus.PAUSED
        reason = WorkflowSuspendReason(suspend_reason)
        checkpoint = self._build_checkpoint(
            workflow,
            motet,
            workflow_run_id=workflow_run_id or f"wfrun-{uuid4().hex}",
            status=WorkflowRunStatus.PAUSED,
            completed_step_ids=completed_step_ids,
            pending_step_ids=pending_step_ids,
            step_results=step_results,
            suspend_reason=reason,
            pending_interactions=pending_interactions,
            resume_epoch=resume_epoch,
            run_created_at=run_created_at,
            child_workflow_run_id=child_workflow_run_id,
            blocked_step_id=blocked_step_id,
            parent_workflow_run_id=parent_workflow_run_id,
        )
        store_workflow_checkpoint(checkpoint)
        self.logger.info(
            "workflow_suspended",
            workflow_id=workflow.workflow_id,
            workflow_run_id=checkpoint.workflow_run_id,
            suspend_reason=reason.value,
            pending_count=len(pending_interactions),
            pending_step_ids=pending_step_ids,
            child_workflow_run_id=child_workflow_run_id,
            blocked_step_id=blocked_step_id,
        )
        envelope = {
            "status": "suspended",
            "suspended": True,
            "workflow_id": workflow.workflow_id,
            "workflow_name": workflow.name,
            "workflow_run_id": checkpoint.workflow_run_id,
            "suspend_reason": reason.value,
            "pending_interactions": [
                p.model_dump(mode="json") for p in pending_interactions
            ],
            "pending_tool_calls": checkpoint.pending_tool_calls(),
            "completed_step_ids": list(completed_step_ids),
            "pending_step_ids": list(pending_step_ids),
            "resume_epoch": checkpoint.resume_epoch,
        }
        if child_workflow_run_id:
            envelope["child_workflow_run_id"] = child_workflow_run_id
            envelope["blocked_step_id"] = blocked_step_id
            # Bubble leaf pending tools for agent/facade when parent waits on child.
            envelope["leaf_workflow_run_id"] = child_workflow_run_id
        return envelope

    def _unwrap_step_payload(self, result: Any) -> Dict[str, Any]:
        """Unwrap ADR-0029 envelopes to the inner dict payload when present."""
        if not isinstance(result, dict):
            return {}
        payload = result.get("data") if "data" in result else result
        return payload if isinstance(payload, dict) else {}

    def _maybe_suspend_for_nested_child(
        self,
        workflow,
        motet,
        remaining: List[str],
        level_results: Dict[str, Any],
        done: set,
        results: Dict[str, Any],
        workflow_run_id: Optional[str],
        *,
        resume_epoch: int = 0,
        run_created_at: Optional[float] = None,
        parent_workflow_run_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Pause parent when a nested workflow_execution step returns suspended (#189)."""
        for sid in remaining:
            result = level_results.get(sid)
            payload = self._unwrap_step_payload(result)
            if not payload and isinstance(result, dict):
                payload = result
            if not (
                isinstance(payload, dict)
                and (payload.get("suspended") or payload.get("status") == "suspended")
            ):
                continue
            child_run_id = str(
                payload.get("leaf_workflow_run_id")
                or payload.get("workflow_run_id")
                or ""
            ).strip()
            if not child_run_id:
                continue
            # Complete non-suspended siblings in this level before pausing.
            for other_sid, other_result in level_results.items():
                if other_sid == sid:
                    continue
                self._merge_step_result(workflow, other_sid, other_result)
                results[other_sid] = other_result
                done.add(other_sid)
            child_reason = str(payload.get("suspend_reason") or "handback_tools")
            pending = list(payload.get("pending_interactions") or [])
            # Reconstruct PendingInteraction models for parent index / bubble.
            from .checkpoint import PendingInteraction, WorkflowSuspendReason

            interactions = []
            for item in pending:
                if not isinstance(item, dict):
                    continue
                kind_raw = item.get("kind") or child_reason
                try:
                    kind = WorkflowSuspendReason(str(kind_raw))
                except ValueError:
                    kind = WorkflowSuspendReason.HANDBACK_TOOLS
                interactions.append(
                    PendingInteraction(
                        interaction_id=str(item.get("interaction_id") or uuid4().hex),
                        kind=kind,
                        step_id=sid,
                        tool_name=item.get("tool_name"),
                        parameters=dict(item.get("parameters") or {}),
                        interaction_schema=item.get("schema") or item.get("interaction_schema"),
                        prompt=item.get("prompt"),
                        auth_challenge=item.get("auth_challenge"),
                    )
                )
            return self._suspend_workflow(
                workflow,
                motet,
                completed_step_ids=sorted(done),
                pending_step_ids=[sid]
                + [x for x in remaining if x != sid and x not in done],
                step_results=results,
                suspend_reason=child_reason
                if child_reason
                in {r.value for r in WorkflowSuspendReason}
                else WorkflowSuspendReason.HANDBACK_TOOLS.value,
                pending_interactions=interactions,
                workflow_run_id=workflow_run_id,
                resume_epoch=resume_epoch,
                run_created_at=run_created_at,
                child_workflow_run_id=child_run_id,
                blocked_step_id=sid,
                parent_workflow_run_id=parent_workflow_run_id,
            )
        return None

    def _maybe_suspend_for_oauth(
        self,
        workflow,
        motet,
        remaining: List[str],
        level_results: Dict[str, Any],
        done: set,
        results: Dict[str, Any],
        workflow_run_id: Optional[str],
        *,
        resume_epoch: int = 0,
        run_created_at: Optional[float] = None,
        parent_workflow_run_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        from .checkpoint import PendingInteraction, WorkflowSuspendReason

        for sid in remaining:
            result = level_results.get(sid)
            if not isinstance(result, dict):
                continue
            payload = self._unwrap_step_payload(result)
            if not payload:
                continue
            status = str(payload.get("status") or result.get("status") or "")
            if status != "auth_required" and not payload.get("auth_required"):
                continue
            # Mark non-oauth siblings completed before pause.
            for other_sid, other_result in level_results.items():
                if other_sid == sid:
                    continue
                self._merge_step_result(workflow, other_sid, other_result)
                results[other_sid] = other_result
                done.add(other_sid)
            interaction = PendingInteraction(
                interaction_id=f"oauth-{uuid4().hex}",
                kind=WorkflowSuspendReason.OAUTH,
                step_id=sid,
                tool_name=self._step_tool_name(workflow.steps[sid]),
                auth_challenge=payload if isinstance(payload, dict) else {"raw": payload},
            )
            return self._suspend_workflow(
                workflow,
                motet,
                completed_step_ids=sorted(done),
                pending_step_ids=[sid] + [x for x in remaining if x != sid and x not in done],
                step_results=results,
                suspend_reason=WorkflowSuspendReason.OAUTH.value,
                pending_interactions=[interaction],
                workflow_run_id=workflow_run_id,
                resume_epoch=resume_epoch,
                run_created_at=run_created_at,
                parent_workflow_run_id=parent_workflow_run_id,
            )
        return None

