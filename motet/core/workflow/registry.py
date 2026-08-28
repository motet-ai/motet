"""
Motet - Workflow Registry

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Workflow registry and schema export service extracted from workflow package
    initialization module.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ..registry import RegistryScope, ScopedRegistry, ScopeFilter, RegistryEntry, scope_from_qualified_name

if TYPE_CHECKING:
    from ..types import CanonicalToolSchema
    from . import Workflow


class WorkflowRegistry:
    _registry: ScopedRegistry["Workflow"] = ScopedRegistry(registry_name="workflow_registry")
    _on_workflow_registered_callback: Optional[Any] = None
    _on_workflow_unregistered_callback: Optional[Any] = None

    @classmethod
    def set_on_registered_callback(cls, callback: Any) -> None:
        cls._on_workflow_registered_callback = callback

    @classmethod
    def set_on_unregistered_callback(cls, callback: Any) -> None:
        cls._on_workflow_unregistered_callback = callback

    @classmethod
    def register(cls, workflow: "Workflow", *, scope: Optional[RegistryScope] = None) -> None:
        is_new = cls._registry.get(workflow.workflow_id) is None
        resolved_scope = scope
        if resolved_scope is None:
            from motet.core.workflow.user_catalog import (
                is_user_workflow_id,
                scope_for_user_workflow,
            )

            if is_user_workflow_id(str(workflow.workflow_id)):
                resolved_scope = scope_for_user_workflow(workflow)
        resolved_scope = resolved_scope or scope_from_qualified_name(workflow.workflow_id)
        cls._registry.register(workflow.workflow_id, workflow, scope=resolved_scope)
        if is_new and cls._on_workflow_registered_callback is not None:
            try:
                cls._on_workflow_registered_callback(workflow.workflow_id)
            except Exception:
                pass  # callback must not crash registration

    @classmethod
    def get(cls, workflow_id: str) -> Optional["Workflow"]:
        return cls._registry.get(workflow_id)

    @classmethod
    def get_scope(cls, workflow_id: str) -> Optional[RegistryScope]:
        entry = cls._registry.get_entry(workflow_id)
        return entry.scope if entry else None

    @classmethod
    def unregister(cls, workflow_id: str) -> bool:
        removed = cls._registry.unregister(workflow_id)
        if removed and cls._on_workflow_unregistered_callback is not None:
            try:
                cls._on_workflow_unregistered_callback(workflow_id)
            except Exception:
                pass  # callback must not crash unregister
        return removed

    @classmethod
    def list_all(cls) -> List["Workflow"]:
        return list(cls._registry.list_items().values())

    @classmethod
    def list_workflow_ids_used_for_tool(cls) -> List[str]:
        """Workflow IDs that are used for tool discovery (indexed and exported as tool schemas)."""
        return [w.workflow_id for w in cls.list_all() if w.is_used_for_tool()]

    @classmethod
    def list_items(cls) -> Dict[str, "Workflow"]:
        return cls._registry.list_items()

    @classmethod
    def list_entries(cls) -> List[RegistryEntry["Workflow"]]:
        return cls._registry.list_entries()

    @classmethod
    def list_visible(cls, scope_filter: ScopeFilter) -> Dict[str, "Workflow"]:
        return cls._registry.list_visible(scope_filter)

    @classmethod
    def list_visible_entries(cls, scope_filter: ScopeFilter) -> List[RegistryEntry["Workflow"]]:
        return cls._registry.list_visible_entries(scope_filter)

    @classmethod
    def unregister_namespace(cls, namespace: str) -> List[str]:
        return cls._registry.unregister_namespace(namespace)

    @classmethod
    def prepare_workflow_for_execution(
        cls,
        workflow_id: str,
        llm_parameters: Dict[str, Any],
        motet: Optional[Any] = None,
        conversation_history: Optional[List] = None,
        reasoning_task: Optional[Any] = None,
        tenant_id: Optional[str] = None,
    ) -> Any:
        from motet.core.commands.command_data_classes import WorkflowExecutionData

        workflow = cls.get(workflow_id)
        from motet.core.workflow.user_catalog import (
            assert_user_workflow_invokable,
            is_user_workflow_id,
        )

        if is_user_workflow_id(str(workflow_id or "")):
            caller = tenant_id
            if not caller and motet is not None:
                caller = getattr(motet, "tenant_id", None)
            workflow = assert_user_workflow_invokable(workflow_id, caller)
        if not workflow:
            raise ValueError(f"Workflow '{workflow_id}' not found in registry")

        workflow_data = workflow.to_execution_data(context_overrides=llm_parameters)
        if conversation_history is not None:
            workflow_data.conversation_history = conversation_history
        if reasoning_task is not None:
            workflow_data.reasoning_task = reasoning_task
        return workflow_data

    @classmethod
    def export_canonical_schemas(
        cls, *, tenant_id: Optional[str] = None
    ) -> List["CanonicalToolSchema"]:
        import structlog
        from ..types import CanonicalToolSchema
        from motet.core.workflow.user_catalog import (
            caller_tenant_id,
            list_visible_workflows,
        )

        logger = structlog.get_logger(__name__)
        workflow_schemas: List[CanonicalToolSchema] = []
        tid = (tenant_id or caller_tenant_id() or "").strip()
        emitted: set[str] = set()

        for workflow in list_visible_workflows(tid):
            if not workflow.is_used_for_tool():
                continue
            if workflow.workflow_id in emitted:
                continue
            emitted.add(workflow.workflow_id)
            workflow_inputs = workflow.get_workflow_inputs()
            properties: Dict[str, Any] = {}
            required_params: List[str] = []

            # Params listed in required_inputs are required by definition —
            # the field means "parameters the LLM must provide".
            if workflow.required_inputs:
                for rp in workflow.required_inputs:
                    if rp not in required_params:
                        required_params.append(rp)

            for param_name in workflow_inputs:
                if workflow.input_parameters and param_name in workflow.input_parameters:
                    prop = dict(workflow.input_parameters[param_name])
                    # "required" is not a valid JSON Schema property keyword;
                    # promote it to the top-level required array instead.
                    if prop.pop("required", False):
                        if param_name not in required_params:
                            required_params.append(param_name)
                    properties[param_name] = prop
                else:
                    properties[param_name] = cls._infer_parameter_info(param_name)
                if param_name in ["url", "target_url", "login_url"]:
                    if param_name not in required_params:
                        required_params.append(param_name)

            # Only include params that actually appear in properties.
            required_params = [p for p in required_params if p in properties]

            workflow_schemas.append(
                CanonicalToolSchema(
                    name=f"workflow_{workflow.workflow_id}",
                    description=f"{workflow.description} (multi-step workflow with {len(workflow.steps)} steps)",
                    json_schema={
                        "type": "object",
                        "properties": properties,
                        "required": required_params,
                    },
                )
            )

            logger.debug(
                "Exported canonical workflow schema",
                workflow_id=workflow.workflow_id,
                param_count=len(properties),
                required_count=len(required_params),
            )

        logger.info("Exported canonical workflow schemas", workflow_count=len(workflow_schemas))
        return workflow_schemas

    @staticmethod
    def _infer_parameter_info(param_name: str) -> Dict[str, Any]:
        if "url" in param_name.lower():
            return {"type": "string", "description": f"URL for {param_name.replace('_', ' ')}"}
        if "email" in param_name.lower():
            return {"type": "string", "description": f"Email address for {param_name.replace('_', ' ')}"}
        if "selector" in param_name.lower():
            return {
                "type": "string",
                "description": f"CSS selector for {param_name.replace('_selector', '').replace('_', ' ')}",
            }
        if "name" in param_name.lower():
            return {
                "type": "string",
                "description": f"Name for {param_name.replace('_name', '').replace('_', ' ')}",
            }
        if param_name in ["username", "password", "text", "query", "destination", "location"]:
            return {"type": "string", "description": f"{param_name.replace('_', ' ').capitalize()}"}
        if "count" in param_name.lower() or "limit" in param_name.lower():
            return {"type": "integer", "description": f"{param_name.replace('_', ' ').capitalize()}"}
        return {"type": "string", "description": f"{param_name.replace('_', ' ').capitalize()}"}

