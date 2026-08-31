"""
Motet - Workflow Executor Steps Mixin

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-29

Description:
    Step and level execution for WorkflowExecutor: parallel/sequential levels,
    single-step dispatch, sequential foreach / until loops, skip conditions,
    workflow_step events, and model-metadata stamping for nested tools.

Dependencies:
    - motet.core.workers.concurrency_primitives: WorkerExecutor, worker_sleep
    - motet.core.conversations.lineage: isolated conversation minting
    - motet.core.workflow.utils: substitute_parameters
    - structlog via self.logger on WorkflowExecutor

Usage:
    class WorkflowExecutor(WorkflowSuspendMixin, WorkflowResumeMixin, WorkflowStepsMixin):
        ...

Notes:
    - Nesting-depth violations re-raise from level helpers so the run fails
      loudly instead of marking a step failed and continuing (#189).
    - Foreach iterations are fail-fast; continue_on_failure applies to the
      whole step after retries.
"""

from __future__ import annotations

import copy
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, cast

from ..conversations.lineage import IsolatedConversation, mint_isolated_conversation
from ..workers.concurrency_primitives import worker_sleep
from .utils import substitute_parameters

if TYPE_CHECKING:
    import structlog


def _root_hint(motet: Any) -> Optional[str]:
    """Root conversation id from turn metadata, or None when unset."""
    meta = getattr(motet, "metadata", None)
    if not isinstance(meta, dict):
        return None
    root = str(meta.get("root_conversation_id") or "").strip()
    return root or None


class WorkflowStepsMixin:
    """Level / step / loop execution helpers for WorkflowExecutor."""

    # Provided by WorkflowExecutor.__init__ (pyright mixin surface).
    logger: "structlog.stdlib.BoundLogger"

    def _emit_workflow_step_event(
        self,
        motet,
        workflow_id: str,
        workflow_name: str,
        step_id: str,
        step_name: str,
        command_type: str,
        status: str = "started",
        stream_key: Optional[str] = None,
        duration_ms: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        import json

        try:
            event_data: Dict[str, Any] = {
                "workflow_id": workflow_id,
                "workflow_name": workflow_name,
                "step_id": step_id,
                "step_name": step_name,
                "command_type": command_type,
                "status": status,
            }
            if duration_ms is not None:
                event_data["duration_ms"] = duration_ms
            if error:
                event_data["error"] = error[:500]

            if motet and hasattr(motet, "stream_event"):
                try:
                    motet.stream_event("workflow_step", stream_key=stream_key, data=json.dumps(event_data))
                except Exception as stream_err:
                    self.logger.debug("workflow_step_stream_failed", error=str(stream_err))

            if motet and hasattr(motet, "event_bus") and motet.event_bus:
                bus_event_data = {
                    "kind": "workflow_step",
                    "task_id": motet.task_id,
                    "trace_id": motet.task_id,
                    "source": "workflow",
                    **event_data,
                    "timestamp": datetime.utcnow().isoformat(),
                }
                motet.publish_event(bus_event_data)
        except Exception as e:
            self.logger.warning(
                "workflow_step_event_emission_failed",
                workflow_id=workflow_id,
                step_id=step_id,
                error=str(e),
            )

    def _stamp_model_metadata_from_context(self, workflow, motet) -> None:
        """
        Copy workflow context model identity into MotetContext metadata.

        Workflow YAML often exposes ``provider`` / ``model_name`` as inputs
        (e.g. deep-research). Those land in ``workflow.context`` but not in
        ``motet.metadata``, so nested ``core.web_search`` could not use the
        LLM-native path. Stamp missing keys only — never overwrite agent/chat
        metadata already present on the parent command.
        """
        if motet is None:
            return
        ctx = getattr(workflow, "context", None) or {}
        if not isinstance(ctx, dict):
            return
        provider = str(ctx.get("model_provider") or ctx.get("provider") or "").strip()
        model_name = str(ctx.get("model_name") or "").strip()
        if not provider and not model_name:
            return

        # Prefer the live command-context dict. motet.metadata uses ``or {}``,
        # which returns a *new* empty dict when stored metadata is falsy ({}).
        meta: Optional[Dict[str, Any]] = None
        cmd = getattr(motet, "_command", None)
        dctx = getattr(cmd, "distributed_context", None) if cmd is not None else None
        if dctx is not None:
            existing = getattr(dctx, "metadata", None)
            if not isinstance(existing, dict):
                dctx.metadata = {}
                existing = dctx.metadata
            meta = existing
        else:
            fb = getattr(motet, "_metadata_fallback", None)
            if isinstance(fb, dict):
                meta = fb
            else:
                try:
                    motet._metadata_fallback = {}
                    meta = motet._metadata_fallback
                except Exception:
                    raw = getattr(motet, "metadata", None)
                    meta = raw if isinstance(raw, dict) else None

        if not isinstance(meta, dict):
            return

        stamped: Dict[str, str] = {}
        if provider and not str(meta.get("model_provider") or "").strip():
            meta["model_provider"] = provider
            stamped["model_provider"] = provider
        if model_name and not str(meta.get("model_name") or "").strip():
            meta["model_name"] = model_name
            stamped["model_name"] = model_name
        if stamped:
            self.logger.info(
                "workflow_model_metadata_stamped",
                workflow_id=getattr(workflow, "workflow_id", None),
                **stamped,
            )

    def _merge_step_result(self, workflow, step_id: str, result: Any) -> None:
        workflow.context[step_id] = result

    def _execute_level_parallel(self, workflow, level, motet):
        from ..workers.concurrency_primitives import WorkerExecutor

        results = {}
        with WorkerExecutor(max_workers=len(level)) as executor:
            futures = {executor.submit(self._execute_step, workflow.steps[sid], workflow, motet): sid for sid in level}
            for future, sid in futures.items():
                try:
                    results[sid] = future.result()
                except Exception as e:
                    if "nesting depth" in str(e).lower():
                        raise
                    results[sid] = {"status": "failed", "error": str(e)}
        return results

    def _execute_level_sequential(self, workflow, level, motet):
        results = {}
        for sid in level:
            try:
                results[sid] = self._execute_step(workflow.steps[sid], workflow, motet)
            except Exception as e:
                # Nesting-budget violations are configuration / safety rails —
                # fail the workflow instead of marking the step failed and
                # continuing dependents (issue #189).
                if "nesting depth" in str(e).lower():
                    raise
                results[sid] = {"status": "failed", "error": str(e)}
        return results

    def _execute_step(self, step, workflow, motet) -> Dict[str, Any]:
        should_skip, skip_reason = self._should_skip_step(step, workflow)
        if should_skip:
            self.logger.info("Skipping step", step_id=step.step_id, reason=skip_reason, workflow_id=workflow.workflow_id)
            return {"status": "skipped", "reason": skip_reason}

        if step.foreach or step.until:
            return self._execute_loop_step(step, workflow, motet)

        return self._execute_step_once(step, workflow, motet, context=workflow.context)

    def _execute_loop_step(self, step, workflow, motet) -> Dict[str, Any]:
        """Run step.command per foreach item, or until step.until holds (sequential)."""
        step_start_time = time.time()
        stream_key = getattr(motet, "stream_key", None)

        items = self._resolve_loop_items(step, workflow)
        if len(items) > step.max_loop_iterations:
            raise ValueError(
                f"Foreach step '{step.step_id}' has {len(items)} items but "
                f"max_loop_iterations={step.max_loop_iterations}"
            )

        self._emit_workflow_step_event(
            motet=motet,
            workflow_id=workflow.workflow_id,
            workflow_name=workflow.name,
            step_id=step.step_id,
            step_name=step.name,
            command_type=step.command_type,
            status="started",
            stream_key=stream_key,
        )

        results: List[Any] = []
        # Overwritten to "until_met" if the break condition fires; otherwise the
        # loop ran out of road, which downstream steps gate on.
        stopped_reason = "items_exhausted" if step.foreach else "max_iterations"
        loop_var = step.loop_var or "item"
        parent_cid = getattr(motet, "conversation_id", None)
        try:
            for i, item in enumerate(items):
                previous = results[-1] if results else {"final_response": ""}
                overlay = {
                    **workflow.context,
                    loop_var: item,
                    "loop": {
                        "index": i,
                        "previous": previous,
                        # Accumulated carry: iteration N sees ALL prior
                        # iteration summaries, not just N-1 (chunked agent
                        # turns need earlier decisions to stay consistent).
                        "previous_summaries": self._format_previous_summaries(results),
                        "all_previous": list(results),
                    },
                }
                iter_step_id = f"{step.step_id}[{i}]"
                iter_conversation_id: Optional[str] = None
                if getattr(step, "isolate_conversation", False):
                    iso = mint_isolated_conversation(
                        parent_cid,
                        tenant_id=getattr(motet, "tenant_id", None),
                        kind="workflow_isolate",
                        root_conversation_id=_root_hint(motet),
                    )
                    iter_conversation_id = iso.conversation_id
                    overlay["isolated_conversation_id"] = iso.conversation_id
                    overlay["parent_conversation_id"] = iso.parent_conversation_id
                    overlay["root_conversation_id"] = iso.root_conversation_id
                self.logger.info(
                    "foreach_iteration_start",
                    step_id=step.step_id,
                    iteration=i,
                    total=len(items),
                    workflow_id=workflow.workflow_id,
                    isolate_conversation=bool(getattr(step, "isolate_conversation", False)),
                    conversation_id=iter_conversation_id or parent_cid,
                )
                iter_result = self._execute_step_once(
                    step,
                    workflow,
                    motet,
                    context=overlay,
                    event_step_id=iter_step_id,
                    event_step_name=f"{step.name}[{i}]",
                    conversation_id=iter_conversation_id,
                )
                unwrapped = self._unwrap_step_result(iter_result)
                results.append(unwrapped)

                # Repeat-until, not while: the body always runs at least once.
                if step.until and self._evaluate_condition(
                    step.until, {**overlay, "result": unwrapped}
                ):
                    stopped_reason = "until_met"
                    self.logger.info(
                        "loop_until_met",
                        step_id=step.step_id,
                        iteration=i,
                        iterations_run=len(results),
                        condition=step.until,
                        workflow_id=workflow.workflow_id,
                    )
                    break

            if step.until and stopped_reason != "until_met":
                self.logger.warning(
                    "loop_until_not_met",
                    step_id=step.step_id,
                    iterations_run=len(results),
                    stopped_reason=stopped_reason,
                    condition=step.until,
                    workflow_id=workflow.workflow_id,
                )

            duration_ms = int((time.time() - step_start_time) * 1000)
            self._emit_workflow_step_event(
                motet=motet,
                workflow_id=workflow.workflow_id,
                workflow_name=workflow.name,
                step_id=step.step_id,
                step_name=step.name,
                command_type=step.command_type,
                status="completed",
                stream_key=stream_key,
                duration_ms=duration_ms,
            )
            # Domain payload (not an ADR-0029 envelope): templates use
            # {{step_id.count}} / {{step_id.results}}.
            return {
                "results": results,
                "count": len(results),
                "stopped_reason": stopped_reason,
            }
        except Exception as e:
            duration_ms = int((time.time() - step_start_time) * 1000)
            self._emit_workflow_step_event(
                motet=motet,
                workflow_id=workflow.workflow_id,
                workflow_name=workflow.name,
                step_id=step.step_id,
                step_name=step.name,
                command_type=step.command_type,
                status="failed",
                stream_key=stream_key,
                duration_ms=duration_ms,
                error=str(e),
            )
            if step.fallback_step_id:
                return self._execute_step(workflow.steps[step.fallback_step_id], workflow, motet)
            if step.continue_on_failure:
                return {
                    "status": "failed",
                    "error": str(e),
                    "results": results,
                    "count": len(results),
                    "stopped_reason": "failed",
                }
            raise

    def _resolve_loop_items(self, step, workflow) -> List[Any]:
        # `until` without `foreach` is a counted repeat: the budget is the list,
        # and loop_var binds the 0-based attempt number.
        if not step.foreach:
            return list(range(step.max_loop_iterations)) if step.until else []

        raw = self._resolve_path(step.foreach, workflow.context)
        if raw is None:
            return []
        if isinstance(raw, str):
            # Templating / JSON round-trip may leave a JSON array string.
            import json

            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                self.logger.warning(
                    "foreach_path_not_list",
                    step_id=step.step_id,
                    path=step.foreach,
                    value_type=type(raw).__name__,
                )
                return []
            if not isinstance(parsed, list):
                raise ValueError(
                    f"Foreach path '{step.foreach}' for step '{step.step_id}' "
                    f"resolved to JSON {type(parsed).__name__}, expected list"
                )
            return parsed
        if not isinstance(raw, list):
            raise ValueError(
                f"Foreach path '{step.foreach}' for step '{step.step_id}' "
                f"resolved to {type(raw).__name__}, expected list"
            )
        return raw

    @staticmethod
    def _format_previous_summaries(results: List[Any]) -> str:
        """Join all prior foreach iteration summaries into prompt-ready text.

        Prefers ``final_response`` (agent turns), falling back to ``message``
        or ``str(result)``. Empty when no prior iteration produced text.
        """
        parts: List[str] = []
        for idx, result in enumerate(results):
            if isinstance(result, dict):
                text = str(
                    result.get("final_response") or result.get("message") or ""
                ).strip()
            else:
                text = str(result or "").strip()
            if text:
                parts.append(f"[iteration {idx + 1}]\n{text}")
        return "\n\n".join(parts)

    @staticmethod
    def _unwrap_step_result(result: Any) -> Any:
        """Store unwrapped command data in foreach results for {{loop.previous.*}}."""
        if isinstance(result, dict):
            if "data" in result:
                return result["data"]
            if result.get("status") in ("completed", "success") and "result" in result:
                return result["result"]
        return result

    def _execute_step_once(
        self,
        step,
        workflow,
        motet,
        *,
        context: Dict[str, Any],
        event_step_id: Optional[str] = None,
        event_step_name: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        step_start_time = time.time()
        stream_key = getattr(motet, "stream_key", None)
        emit_id = event_step_id or step.step_id
        emit_name = event_step_name or step.name

        # Non-foreach steps may also request isolation (e.g. soft reviewer).
        effective_conversation_id = conversation_id
        isolated: Optional[IsolatedConversation] = None
        if (
            effective_conversation_id is None
            and getattr(step, "isolate_conversation", False)
            and not getattr(step, "foreach", None)
        ):
            isolated = mint_isolated_conversation(
                getattr(motet, "conversation_id", None),
                tenant_id=getattr(motet, "tenant_id", None),
                kind="workflow_isolate",
                root_conversation_id=_root_hint(motet),
            )
            effective_conversation_id = isolated.conversation_id

        self._emit_workflow_step_event(
            motet=motet,
            workflow_id=workflow.workflow_id,
            workflow_name=workflow.name,
            step_id=emit_id,
            step_name=emit_name,
            command_type=step.command_type,
            status="started",
            stream_key=stream_key,
        )

        from motet.core.commands.command_type_registry import command_type_registry

        registration = command_type_registry.get(step.command_type)
        if not registration:
            available = ", ".join(sorted(command_type_registry.get_command_types()))
            raise ValueError(f"Unknown command '{step.command_type}'. Available: {available}")

        data_class = registration.data_class
        if not data_class:
            impl = registration.implementation
            command_class = getattr(impl, "__command_class__", None)
            if command_class is not None:
                get_data_class = getattr(command_class, "_get_data_class", None)
                data_class = get_data_class() if get_data_class else None
            else:
                get_data_class = getattr(impl, "_get_data_class", None)
                data_class = get_data_class() if get_data_class else None
            if not data_class:
                raise ValueError(f"Command {step.command_type} has no data class")

        resolved_data = substitute_parameters(copy.deepcopy(step.command_data), context)
        data = data_class(**resolved_data)
        execution_kwargs = step.execution_context or {}
        # Preserve parent metadata but allow step-specific execution context metadata.
        # Critical for stream attribution: nested core.agent_turn steps must carry their
        # own agent identity so task-stream frames include the correct agent_id.
        # Only treat real dict metadata as metadata; unittest.Mock exposes arbitrary attrs as
        # MagicMock, which breaks dict() / .keys() merge and caused workflow tests to fail
        # before motet.do ran.
        parent_meta: Dict[str, Any] = {}
        if motet is not None:
            raw_meta = getattr(motet, "metadata", None)
            if isinstance(raw_meta, dict):
                parent_meta = dict(raw_meta)
        merged_meta: Dict[str, Any] = dict(parent_meta)
        # Preserve chat primary agent for nested agent_turn transcript ordering (turn.agent_turn).
        _primary = merged_meta.get("conversation_primary_agent_id")
        step_meta = execution_kwargs.get("metadata") if isinstance(execution_kwargs, dict) else None
        if isinstance(step_meta, dict):
            merged_meta.update(step_meta)
        if isinstance(resolved_data, dict):
            step_agent_id = resolved_data.get("agent_id")
            if isinstance(step_agent_id, str) and step_agent_id.strip():
                aid = step_agent_id.strip()
                merged_meta["agent_id"] = aid
                merged_meta["configured_agent_id"] = aid
                merged_meta["configured_agent_qualified_id"] = aid
        if _primary and not merged_meta.get("conversation_primary_agent_id"):
            merged_meta["conversation_primary_agent_id"] = _primary
        if isolated is not None:
            merged_meta["parent_conversation_id"] = isolated.parent_conversation_id
            merged_meta["root_conversation_id"] = isolated.root_conversation_id
            merged_meta["isolated_conversation"] = True
        elif effective_conversation_id:
            if context.get("root_conversation_id"):
                merged_meta["parent_conversation_id"] = context.get("parent_conversation_id")
                merged_meta["root_conversation_id"] = context.get("root_conversation_id")
            else:
                merged_meta.setdefault(
                    "parent_conversation_id", getattr(motet, "conversation_id", None)
                )
            merged_meta["isolated_conversation"] = True
        execution_kwargs = {**execution_kwargs, "metadata": merged_meta}
        if effective_conversation_id:
            # motet.do merges kwargs after parent conversation_id → override wins.
            execution_kwargs["conversation_id"] = effective_conversation_id

        cmd = (step.command_type or "").strip()
        if cmd in (
            "workflow_execution",
            "core.workflow_execution",
            "full_workflow_execution",
            "core.full_workflow_execution",
        ):
            from .checkpoint import WORKFLOW_MAX_NESTING_DEPTH

            current_depth = int(merged_meta.get("workflow_nesting_depth") or 0)
            # Prefer child workflow override when present on command data.
            child_cap = getattr(data, "max_nesting_depth", None)
            parent_cap = getattr(workflow, "max_nesting_depth", None)
            max_depth = (
                int(child_cap)
                if child_cap is not None
                else int(parent_cap)
                if parent_cap is not None
                else WORKFLOW_MAX_NESTING_DEPTH
            )
            if current_depth >= max_depth:
                raise ValueError(
                    f"Workflow nesting depth {current_depth + 1} exceeds max "
                    f"{max_depth} (step '{step.step_id}'); refuse nested "
                    f"{cmd} to protect worker slots (issue #189)"
                )
            # Child frame identity: depth + parent run pointer for stack unwind.
            parent_run = merged_meta.get("workflow_run_id") or merged_meta.get(
                "current_workflow_run_id"
            )
            merged_meta["workflow_nesting_depth"] = current_depth + 1
            if parent_run:
                merged_meta["parent_workflow_run_id"] = parent_run
            execution_kwargs = {**execution_kwargs, "metadata": merged_meta}

        for attempt in range(step.step_retry_attempts + 1):
            try:
                result = motet.do(registration.implementation, data=data, **execution_kwargs)
                duration_ms = int((time.time() - step_start_time) * 1000)
                self._emit_workflow_step_event(
                    motet=motet,
                    workflow_id=workflow.workflow_id,
                    workflow_name=workflow.name,
                    step_id=emit_id,
                    step_name=emit_name,
                    command_type=step.command_type,
                    status="completed",
                    stream_key=stream_key,
                    duration_ms=duration_ms,
                )
                return cast(Dict[str, Any], result)
            except Exception as e:
                if attempt < step.step_retry_attempts:
                    worker_sleep(step.step_retry_delay_seconds * (2**attempt))
                    continue

                duration_ms = int((time.time() - step_start_time) * 1000)
                self._emit_workflow_step_event(
                    motet=motet,
                    workflow_id=workflow.workflow_id,
                    workflow_name=workflow.name,
                    step_id=emit_id,
                    step_name=emit_name,
                    command_type=step.command_type,
                    status="failed",
                    stream_key=stream_key,
                    duration_ms=duration_ms,
                    error=str(e),
                )

                # Fallback / continue_on_failure only for non-foreach single steps.
                # Foreach owns those decisions at the aggregate step level.
                if event_step_id is None:
                    if step.fallback_step_id:
                        return self._execute_step(workflow.steps[step.fallback_step_id], workflow, motet)
                    if step.continue_on_failure:
                        return {"status": "failed", "error": str(e)}
                raise

        raise RuntimeError(
            f"Step '{step.step_id}' exhausted retries without returning a result"
        )

    def _should_skip_step(self, step, workflow) -> tuple[bool, Optional[str]]:
        for dep_id in step.dependencies:
            # A dependency declared continue_on_failure is optional: its
            # failure (or absence, e.g. dispatch-level errors) must not
            # cascade into skipping dependents — that would silently disable
            # the core of a workflow because an enrichment step hiccuped.
            dep_step = workflow.steps.get(dep_id)
            dep_optional = bool(getattr(dep_step, "continue_on_failure", False))
            dep_result = workflow.context.get(dep_id)
            if not dep_result:
                if dep_optional:
                    self.logger.info(
                        "optional_dependency_missing_continuing",
                        step_id=step.step_id,
                        dependency=dep_id,
                        workflow_id=workflow.workflow_id,
                    )
                    continue
                return (True, f"Dependency {dep_id} not found in context")
            if isinstance(dep_result, dict):
                status = dep_result.get("status")
                if status in ["failed", "error"]:
                    if dep_optional:
                        self.logger.info(
                            "optional_dependency_failed_continuing",
                            step_id=step.step_id,
                            dependency=dep_id,
                            dependency_status=status,
                            workflow_id=workflow.workflow_id,
                        )
                        continue
                    return (True, f"Dependency {dep_id} failed with status: {status}")

        if step.skip_condition:
            condition_met = self._evaluate_condition(step.skip_condition, workflow.context)
            if condition_met:
                return (True, f"Condition met: {step.skip_condition}")

        return (False, None)

    def _evaluate_condition(self, condition: str, context: Dict[str, Any]) -> bool:
        parts = condition.split(":", 2)
        if len(parts) < 2:
            self.logger.warning(f"Invalid condition format: {condition}")
            return False

        condition_type = parts[0]
        if condition_type == "if_empty":
            value = self._resolve_path(parts[1], context)
            if value is None:
                return True
            if isinstance(value, (list, dict, str)) and len(value) == 0:
                return True
            return False
        if condition_type == "if_not_empty":
            value = self._resolve_path(parts[1], context)
            if value is None:
                return False
            if isinstance(value, (list, dict, str)):
                return len(value) > 0
            return True
        if condition_type == "if_equals":
            if len(parts) < 3:
                return False
            value = self._resolve_path(parts[1], context)
            return self._values_equal(value, parts[2])
        if condition_type == "if_contains":
            if len(parts) < 3:
                return False
            value = self._resolve_path(parts[1], context)
            if isinstance(value, str):
                return parts[2] in value
            if isinstance(value, (list, dict)):
                return parts[2] in value
            return False
        if condition_type == "if_failed":
            result = context.get(parts[1], {})
            if isinstance(result, dict):
                return result.get("status") in ["failed", "error"]
            return False

        self.logger.warning(f"Unknown condition type: {condition_type}")
        return False

    @staticmethod
    def _values_equal(value: Any, literal: str) -> bool:
        """Typed comparison for ``if_equals`` literals.

        Booleans match case-insensitive true/false tokens (``True``/``true``/
        ``1`` and ``False``/``false``/``0``), None matches ``None``/``null``/
        ``~``, and numbers compare numerically, so workflow YAML does not need
        to know Python's ``str()`` casing. Everything else falls back to the
        historical ``str(value) == literal`` comparison.
        """
        text = literal.strip()
        lowered = text.lower()
        if isinstance(value, bool):
            if lowered in ("true", "1", "yes"):
                return value is True
            if lowered in ("false", "0", "no"):
                return value is False
            return False
        if value is None:
            return lowered in ("none", "null", "~", "")
        if isinstance(value, (int, float)):
            try:
                return float(value) == float(text)
            except ValueError:
                return False
        return str(value) == literal

    def _resolve_path(self, path: str, context: Dict[str, Any]) -> Any:
        parts = path.split(".")
        value: Any = context
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
                if value is None:
                    return None
            else:
                return None
        return value
