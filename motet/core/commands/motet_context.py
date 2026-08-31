"""
Motet - MotetContext Runtime

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-31

Description:
    Runtime MotetContext and resource helpers for decorator-based commands
    (GitHub issue #158). The SDK exposes a Protocol stub; the
    runtime bridge in bundle_reload injects this implementation.

Dependencies:
    - pydantic / typing: Models and annotations
    - DistributedCommand: Command composition target
    - WorkerLocal: Pool-agnostic context storage
    - Event observers: TemporaryObserver for observe_events
    - Builtin commands: Lazy-imported from helper methods

Usage:
    from motet.core.commands.motet_context import MotetContext, get_motet_context
    # Prefer re-export via decorator for existing call sites:
    from motet.core.commands.decorator import MotetContext, get_motet_context

Notes:
    - Must not import decorator at module level (avoids circular imports).
    - Builtin command imports stay lazy inside Motet*Helper methods.
    - decorator.distributed_command constructs MotetContext and sets WorkerLocal.
    - ``motet.join`` / ``motet.apply`` unwrap gather/map children via
      ``parse_command_envelope``. ``join`` unwraps both the success list and
      ``GatherExecutionError.partial_results`` — same domain shape either way.
      Map/gather emit full ``BaseCommandResponse`` children
      (``child_command_envelope``).
    - ``do`` / ``join`` / ``apply`` / ``dispatch`` inherit remaining parent
      timeout unless the caller sets ``timeout_seconds``.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
import structlog
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional, Type, Union, Tuple, Set, get_type_hints

from pydantic import BaseModel, Field, ConfigDict

from motet.core.security.encrypted_stream_codec import encode_encrypted_message_data
from motet.core.security.encryption_contexts import EncryptionContext

from motet.core.commands.distributed import DistributedCommand

from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.command_data_classes import BaseCommandData
from motet.core.commands.utils import require_context_field, require_context_identity
from motet.core.workers.observers import EventPriority, Observer, EventFilter, Event

from motet.core.workers.concurrency_primitives import WorkerLocal, WorkerLock
from motet.core.types import PoolType, serialize_memory_items

logger = structlog.get_logger(__name__)


def remaining_command_timeout_seconds(command: Any) -> Optional[int]:
    """Seconds left on a command's Celery/communicator budget."""
    if command is None:
        return None
    ctx = getattr(command, "distributed_context", None)
    raw_total = getattr(ctx, "timeout_seconds", None) if ctx is not None else None
    try:
        total = int(raw_total) if raw_total is not None else None
    except (TypeError, ValueError):
        return None
    if total is None or total < 1:
        return None
    started = getattr(command, "distribution_started_at", None)
    if started is None and ctx is not None:
        started = getattr(ctx, "created_at", None)
    return max(1, int(total - _elapsed_seconds(started)))


def _elapsed_seconds(started: Any) -> float:
    if started is None:
        return 0.0
    parsed = started
    if isinstance(started, str):
        try:
            parsed = datetime.fromisoformat(started.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    if not isinstance(parsed, datetime):
        return 0.0
    if parsed.tzinfo is not None:
        now = datetime.now(timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
    else:
        now = datetime.utcnow()
    return max(0.0, (now - parsed).total_seconds())


# Worker-local storage for MotetContext (pool-agnostic - ADR-0033)
# Uses WorkerLocal instead of contextvars for compatibility with gevent/eventlet
_motet_context = WorkerLocal()

def get_motet_context() -> 'MotetContext':
    """
    Get the current MotetContext from worker-local storage.
    
    This allows decorated commands to access MotetContext without requiring
    it as an explicit parameter. The context is automatically set by the
    @motet.command decorator during execution.
    
    Uses WorkerLocal (pool-agnostic) instead of contextvars for compatibility
    with gevent/eventlet worker pools (ADR-0033).
    
    Returns:
        MotetContext: The current motet context
        
    Raises:
        RuntimeError: If called outside of a @motet.command
        
    Example:
        @motet.command()
        def my_command(data: MyData) -> Dict[str, Any]:
            # Access motet context without parameter
            motet = get_motet_context()
            task_id = motet.task_id
            result = motet.do(other_command, ...)
            return {"result": result}
            
        # Or with helper functions
        def helper_function():
            motet = get_motet_context()
            return motet.task_id
    """
    if not hasattr(_motet_context, 'current'):
        raise RuntimeError(
            "MotetContext not available. "
            "This function must be called within a @motet.command execution context."
        )
    return _motet_context.current


# Canonical location: motet.core.workers.invoker_context
# Re-exported here for backward compatibility.
from motet.core.workers.invoker_context import resolve_current_identity as resolve_current_identity  # noqa: F401


def _set_motet_context(motet: 'MotetContext') -> None:
    """
    Internal: Set the current MotetContext in worker-local storage.
    
    This is called automatically by the @motet.command decorator.
    Do not call this directly in user code.
    
    Args:
        motet: MotetContext to set
    """
    _motet_context.current = motet


def _clear_motet_context() -> None:
    """
    Internal: Clear the current MotetContext from worker-local storage.
    
    This is called automatically by the @motet.command decorator
    to clean up after execution. Do not call this directly in user code.
    Prevents memory leaks in worker pools.
    """
    if hasattr(_motet_context, 'current'):
        del _motet_context.current

class TemporaryObserver:
    """
    Context manager for temporary event observation.
    
    Automatically registers an observer on entry and unregisters on exit,
    ensuring clean lifecycle management.
    
    ADR-0030: Decorator-Based Command Pattern - Phase 2
    """
    
    def __init__(
        self,
        observer_manager: Any,
        event_types: Set[str],
        callback: Callable[[Event], None],
        priority: int,
        custom_filter: Optional[Callable[[Event], bool]],
        command_id: str
    ):
        """
        Initialize temporary observer.
        
        Args:
            observer_manager: EventObserverManager instance
            event_types: Set of event types to observe
            callback: Function to call when events match
            priority: Minimum priority level
            custom_filter: Optional additional filter
            command_id: Command ID for observer naming
        """
        self.observer_manager = observer_manager
        self.event_types = event_types
        self.callback = callback
        self.priority = priority
        self.custom_filter = custom_filter
        self.command_id = command_id
        self.observer = None
    
    def __enter__(self):
        """Register observer on context entry."""
        if not self.observer_manager:
            logger.warning(
                "temporary_observer_no_manager",
                message="No observer_manager available, events will not be captured",
            )
            return self
        
        # Create inline observer class
        class InlineObserver(Observer):
            """Inline observer for temporary event capture."""
            
            def __init__(self, name: str, callback: Callable, event_types: Set[str], priority: int, custom_filter: Optional[Callable]):
                super().__init__(name)
                self._callback = callback
                self._event_types = event_types
                self._priority = priority
                self._custom_filter = custom_filter

            def get_event_filter(self) -> EventFilter:
                """Return event filter for this observer."""
                return EventFilter(
                    event_types=self._event_types,
                    min_priority=self._priority if isinstance(self._priority, EventPriority) else EventPriority(self._priority),
                    custom_filter=self._custom_filter
                )

            def on_event(self, event: Event) -> None:
                """Handle matching event."""
                try:
                    self._callback(event)
                except Exception as e:
                    logger.error(
                        "temporary_observer_callback_error",
                        error=str(e),
                        exc_info=True,
                    )
        
        # Create and register observer
        observer_name = f"temp_observer_{self.command_id[:8]}"
        self.observer = InlineObserver(
            name=observer_name,
            callback=self.callback,
            event_types=self.event_types,
            priority=self.priority,
            custom_filter=self.custom_filter
        )
        
        self.observer_manager.register_observer(self.observer)
        logger.debug(
            "temporary_observer_registered",
            event_types=list(self.event_types),
            command_id=self.command_id[:8],
        )
        
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Unregister observer on context exit."""
        if self.observer and self.observer_manager:
            self.observer_manager.unregister_observer(self.observer)
            logger.debug("temporary_observer_unregistered", command_id=self.command_id[:8])
        
        # Don't suppress exceptions
        return False

class MotetToolsHelper:
    """
    Helper that delegates motet.tools.execute to tool_execution command when context exists (ADR-0084).

    When MotetContext has task_id and the current command is not tool_execution, execute()
    delegates via motet.do(tool_execution, data=...). Otherwise uses registry._execute_tool_only.
    Proxies get, list, and other registry methods to the underlying registry.
    """

    __slots__ = ("_motet", "_registry")

    def __init__(self, motet: "MotetContext", registry: Any) -> None:
        self._motet = motet
        self._registry = registry

    def get(self, name: str) -> Any:
        """Return registered tool by name (delegates to registry)."""
        return self._registry.get(name)

    def list(self) -> Dict[str, Any]:
        """Return all registered tools (delegates to registry.list_items)."""
        return self._registry.list_items()

    def execute(
        self,
        name: str,
        params: Dict[str, Any],
        *,
        allow: Optional[set] = None,
        deny: Optional[set] = None,
        timeout: Optional[float] = None,
        role: Optional[str] = None,
        persist_observation: bool = True,
    ) -> Dict[str, Any]:
        """
        Execute a tool. Delegates to tool_execution command when context exists (ADR-0084).

        When allow/deny/role are provided, uses inner path only (registry._execute_tool_only)
        since ToolExecutionData does not carry those; otherwise delegates when not inside
        tool_execution and task_id is set.
        """
        kwargs = dict(
            allow=allow, deny=deny, timeout=timeout, role=role,
            persist_observation=persist_observation,
        )
        # Inner path when policy args are passed (command does not accept them)
        if allow is not None or deny is not None or role is not None:
            return self._registry._execute_tool_only(name, params, **kwargs)
        # Inside tool_execution: use inner path to avoid recursion (accept bare or core.*)
        cmd = getattr(self._motet, "_command", None)
        if cmd and getattr(cmd, "get_command_type", None):
            ct = cmd.get_command_type()
            if ct == "tool_execution" or ct == "core.tool_execution":
                return self._registry._execute_tool_only(name, params, **kwargs)
        # Delegate to command when we have task context
        if getattr(self._motet, "task_id", None):
            from motet.core.commands.builtin.tool import tool_execution
            from motet.core.commands.command_data_classes import ToolExecutionData
            data = ToolExecutionData(tool_name=name, parameters=params)
            try:
                result = self._motet.do(tool_execution, data=data)
            except Exception as e:
                from motet.core.commands.response_models import CommandExecutionError
                if isinstance(e, CommandExecutionError):
                    return {"status": "error", "error": e.message or str(e)}
                raise
            # Unwrap: tool_execution returns { tool_name, result, executed }
            if isinstance(result, dict) and "result" in result:
                return result["result"]
            return result if isinstance(result, dict) else {"status": "success", "data": result}
        return self._registry._execute_tool_only(name, params, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Proxy other registry attributes (e.g. describe, parse_line, estimate)."""
        return getattr(self._registry, name)


# Memory command types for inner-path detection (ADR-0084)
_MEMORY_COMMAND_TYPES = frozenset(
    {
        "memory_store",
        "memory_vector_index",
        "memory_search",
        "memory_recall",
        "memory_tag",
        "memory_forget",
    }
)


def _is_memory_command_type(command_type: str) -> bool:
    """True if command_type is a memory command (bare or namespaced, e.g. core.memory_store)."""
    if command_type in _MEMORY_COMMAND_TYPES:
        return True
    if "." in command_type:
        return command_type.split(".")[-1] in _MEMORY_COMMAND_TYPES
    return False


class MotetMemoryHelper:
    """
    Helper that delegates motet.memory.store/recall/tag/forget to memory commands when context exists (ADR-0084).
    When inside a memory command or no task_id, uses manager methods directly (inner path).
    """

    __slots__ = ("_motet",)

    def __init__(self, motet: "MotetContext") -> None:
        self._motet = motet

    @property
    def _worker_context(self) -> Dict[str, Any]:
        return getattr(self._motet, "_worker_context", {}) or {}

    def _inside_memory_command(self) -> bool:
        cmd = getattr(self._motet, "_command", None)
        if cmd and getattr(cmd, "get_command_type", None):
            return _is_memory_command_type(cmd.get_command_type())
        return False

    def _should_delegate(self) -> bool:
        if self._inside_memory_command():
            return False
        return bool(getattr(self._motet, "task_id", None))

    def store(self, content: str, type: str = "note", tags: Optional[List[str]] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Dict[str, Any]:
        """Store memory. Delegates to memory_store command when context exists (ADR-0084)."""
        manager = self._worker_context.get("memory_manager")
        if not manager:
            raise ValueError("Memory manager not available")
        if self._should_delegate():
            from motet.core.commands.builtin.memory import memory_store
            from motet.core.commands.command_data_classes import MemoryStoreData
            scope_type = kwargs.get("scope_type") or kwargs.get("scope")
            if scope_type is not None and hasattr(scope_type, "value"):
                scope_type = scope_type.value  # type: ignore[union-attr]
            long_term = kwargs.get("long_term")
            data = MemoryStoreData.model_validate({
                "content": content,
                "type": type,
                "tags": tags or [],
                "metadata": metadata or {},
                "scope_type": scope_type,
                "long_term": long_term,
            })
            try:
                result = self._motet.do(memory_store, data=data)
            except Exception as e:
                from motet.core.commands.response_models import CommandExecutionError
                if isinstance(e, CommandExecutionError):
                    return {"stored": False, "error": e.message or str(e)}
                raise
            return result if isinstance(result, dict) else {"memory_id": result, "stored": True}
        result = manager.store_memory(content=content, type=type, tags=tags or [], metadata=metadata or {}, **kwargs)
        return {"memory_id": result.get("id"), "stored": True}

    def recall(self, query: str = "", limit: int = 10, tags: Optional[List[str]] = None, **kwargs: Any) -> Any:
        """Recall memories. Delegates to memory_recall command when context exists (ADR-0084)."""
        manager = self._worker_context.get("memory_manager")
        if not manager:
            raise ValueError("Memory manager not available")
        min_relevance = float(kwargs.get("min_relevance", 0.5))
        if self._should_delegate():
            from motet.core.commands.builtin.memory import memory_recall
            from motet.core.commands.command_data_classes import MemoryRecallData
            data = MemoryRecallData(
                query=query,
                limit=limit,
                tags=tags or [],
                min_relevance=min_relevance,
                conversation_id=kwargs.get("conversation_id"),
            )
            try:
                result = self._motet.do(memory_recall, data=data)
            except Exception as e:
                from motet.core.commands.response_models import CommandExecutionError
                if isinstance(e, CommandExecutionError):
                    return []
                raise
            return result.get("items", result) if isinstance(result, dict) else result
        if query and hasattr(manager, "hybrid_retrieve"):
            # Do not implicitly force current conversation when searching by query.
            # Only scope by conversation when caller explicitly requests it.
            explicit_conversation_id = kwargs.get("conversation_id")
            # Keyword relevance is query coverage (head-biased), so the default
            # floor is meaningful for tagged long reports as well as untagged ones.
            items = manager.hybrid_retrieve(
                query=query,
                limit=limit,
                min_relevance=min_relevance,
                conversation_id=explicit_conversation_id,
                tags=tags or [],
                motet_context=self._motet,
            )
            return serialize_memory_items(items)
        items = manager.recall(tags=tags or [], limit=limit, **kwargs)
        return serialize_memory_items(items)

    def tag(self, tags: Optional[List[str]] = None, op: str = "add", memory_ids: Optional[List[str]] = None, conversation_id: Optional[str] = None, filter_tag: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:
        """Tag memory. Delegates to memory_tag command when context exists (ADR-0084)."""
        manager = self._worker_context.get("memory_manager")
        if not manager:
            raise ValueError("Memory manager not available")
        if self._should_delegate():
            from motet.core.commands.builtin.memory import memory_tag
            from motet.core.commands.command_data_classes import MemoryTagData
            data = MemoryTagData(
                memory_id=memory_ids[0] if memory_ids else "",
                memory_ids=memory_ids,
                tags=tags or [],
                operation=op,
                filter_tag=filter_tag,
                conversation_id=conversation_id,
            )
            try:
                result = self._motet.do(memory_tag, data=data)
            except Exception as e:
                from motet.core.commands.response_models import CommandExecutionError
                if isinstance(e, CommandExecutionError):
                    return {"updated": 0, "ids": []}
                raise
            return result if isinstance(result, dict) else {"updated": 0, "ids": []}
        result = manager.retag(
            tags=tags or [],
            op=op,
            memory_ids=memory_ids,
            conversation_id=conversation_id,
            filter_tag=filter_tag,
            motet_context=kwargs.get("motet_context"),
        )
        return {"updated": result.get("updated", 0), "ids": result.get("ids", [])}

    def forget(self, memory_ids: Optional[List[str]] = None, conversation_id: Optional[str] = None, filter_tag: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:
        """Delete targeted memories. Delegates to memory_forget when context exists (ADR-0084)."""
        manager = self._worker_context.get("memory_manager")
        if not manager:
            raise ValueError("Memory manager not available")
        if self._should_delegate():
            from motet.core.commands.builtin.memory import memory_forget
            from motet.core.commands.command_data_classes import MemoryForgetData
            data = MemoryForgetData(
                memory_ids=memory_ids,
                conversation_id=conversation_id,
                filter_tag=filter_tag,
            )
            try:
                result = self._motet.do(memory_forget, data=data)
            except Exception as e:
                from motet.core.commands.response_models import CommandExecutionError
                if isinstance(e, CommandExecutionError):
                    return {"deleted": 0, "ids": [], "vector_deleted": 0}
                raise
            if isinstance(result, dict):
                return {
                    "deleted": result.get("deleted", 0),
                    "ids": result.get("ids", []),
                    "vector_deleted": result.get("vector_deleted", 0),
                }
            return {"deleted": 0, "ids": [], "vector_deleted": 0}
        result = manager.forget(
            memory_ids=memory_ids,
            conversation_id=conversation_id,
            filter_tag=filter_tag,
            **kwargs,
        )
        return {
            "deleted": result.get("deleted", 0),
            "ids": result.get("ids", []),
            "vector_deleted": result.get("vector_deleted", 0),
        }

    def __getattr__(self, name: str) -> Any:
        """Proxy other memory manager attributes (e.g. hybrid_retrieve)."""
        manager = self._worker_context.get("memory_manager")
        if manager is None:
            raise AttributeError(f"Memory manager not available (no attribute {name})")
        return getattr(manager, name)


class MotetAgentsHelper:
    """Facade over agent config and agent_turn command (list/get/turn)."""

    __slots__ = ("_motet",)

    def __init__(self, motet: "MotetContext") -> None:
        self._motet = motet

    def list(self, principal_roles: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """List visible agents. Delegates to agent_list when task_id set."""
        if getattr(self._motet, "task_id", None):
            from motet.core.commands.builtin.agents import agent_list
            from motet.core.commands.command_data_classes import AgentListData
            data = AgentListData(principal_roles=principal_roles or [])
            try:
                result = self._motet.do(agent_list, data=data)
                return result.get("agents", [])
            except Exception:
                pass  # agent list from stack optional; fallback to discovery
        from motet.core.agents.discovery import list_visible_agents
        return list_visible_agents(principal_roles=principal_roles)

    def get(self, agent_id: str) -> Optional[Any]:
        """Resolve agent config by id (or None)."""
        try:
            from motet.core.agents import get_agent_registry, resolve_agent_id
            reg = get_agent_registry()
            qid = resolve_agent_id(agent_id)
            return reg.get(qid)
        except Exception:
            return None

    def turn(self, agent_id: str, messages: List[Any], **kwargs: Any) -> Any:
        """Run a turn with the named agent. Delegates to agent_turn command (ADR-0084)."""
        if not getattr(self._motet, "task_id", None):
            raise RuntimeError("motet.agents.turn requires task context; use motet.do(agent_turn, data=...) when outside a command")
        from motet.core.orchestration.turn import agent_turn
        from motet.core.commands.command_data_classes import AgentTurnData
        data = AgentTurnData(agent_id=agent_id, messages=messages, context=kwargs.get("context"))
        return self._motet.do(agent_turn, data=data)


class MotetModelsHelper:
    """Facade over model registry and model_inference/model_stream commands."""

    __slots__ = ("_motet",)

    def __init__(self, motet: "MotetContext") -> None:
        self._motet = motet

    def list(self, provider: Optional[str] = None) -> List[Any]:
        """List configured model specs (from model registry)."""
        try:
            from motet.core.models.registry import model_registry
            return model_registry.list(provider=provider)
        except Exception:
            return []

    def get(self, provider: str, name: str) -> Optional[Any]:
        """Resolve model spec by provider and name."""
        try:
            from motet.core.models.registry import get_model_spec
            return get_model_spec(provider, name)
        except Exception:
            return None

    def infer(self, provider: str, model_name: str, messages: List[Any], **kwargs: Any) -> Any:
        """Run model inference. Delegates to model_inference command (ADR-0084)."""
        if not getattr(self._motet, "task_id", None):
            raise RuntimeError("motet.models.infer requires task context; use motet.do(model_inference, data=...) when outside a command")
        from motet.core.commands.builtin.model import model_inference
        from motet.core.commands.command_data_classes import ModelInferenceData
        ms = dict(kwargs.get("model_settings") or {}, provider=provider, model_name=model_name)
        for k in ("temperature", "max_tokens", "stop", "enable_thinking", "reasoning_effort"):
            if k in kwargs:
                ms[k] = kwargs[k]
        data = ModelInferenceData(messages=messages, model_settings=ms, tools=kwargs.get("tools"), request_context=kwargs.get("request_context"), stream=kwargs.get("stream", False))
        return self._motet.do(model_inference, data=data)

    def stream(self, provider: str, model_name: str, messages: List[Any], **kwargs: Any) -> Any:
        """Run model streaming. Delegates to model_stream command (ADR-0084)."""
        if not getattr(self._motet, "task_id", None):
            raise RuntimeError("motet.models.stream requires task context; use motet.do(model_stream, data=...) when outside a command")
        from motet.core.commands.builtin.model import model_stream
        from motet.core.commands.command_data_classes import ModelStreamData
        ms = dict(kwargs.get("model_settings") or {}, provider=provider, model_name=model_name)
        for k in ("temperature", "max_tokens", "stop", "enable_thinking", "reasoning_effort"):
            if k in kwargs:
                ms[k] = kwargs[k]
        data = ModelStreamData(messages=messages, model_settings=ms, stream_key=kwargs.get("stream_key", ""), tools=kwargs.get("tools"), request_context=kwargs.get("request_context"))
        return self._motet.do(model_stream, data=data)


class MotetWorkflowsHelper:
    """Facade over workflow registry and workflow_execution command."""

    __slots__ = ("_motet",)

    def __init__(self, motet: "MotetContext") -> None:
        self._motet = motet

    def list(self) -> List[Any]:
        """List workflow ids visible to the current tenant."""
        try:
            from motet.core.workflow.user_catalog import list_visible_workflows

            tenant_id = str(getattr(self._motet, "tenant_id", "") or "").strip()
            return [w.workflow_id for w in list_visible_workflows(tenant_id)]
        except Exception:
            return []

    def get(self, workflow_id: str) -> Optional[Any]:
        """Resolve workflow definition by id (tenant-scoped for user.*)."""
        try:
            from motet.core.workflow import WorkflowRegistry
            from motet.core.workflow.user_catalog import (
                is_user_workflow_id,
                resolve_user_workflow_for_tenant,
            )

            if is_user_workflow_id(workflow_id):
                tenant_id = str(getattr(self._motet, "tenant_id", "") or "").strip()
                return resolve_user_workflow_for_tenant(workflow_id, tenant_id)
            return WorkflowRegistry.get(workflow_id)
        except Exception:
            return None

    def run(self, workflow_id: str, context: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Any:
        """Run a workflow. Delegates to workflow_execution command (ADR-0084)."""
        if not getattr(self._motet, "task_id", None):
            raise RuntimeError("motet.workflows.run requires task context; use motet.do(workflow_execution, data=...) when outside a command")
        from motet.core.commands.builtin.workflow import workflow_execution
        from motet.core.commands.command_data_classes import WorkflowExecutionData
        data = WorkflowExecutionData(workflow_id=workflow_id, context=context or {}, **kwargs)
        return self._motet.do(workflow_execution, data=data)


class MotetSchedulesHelper:
    """Facade over schedule commands (create/list)."""

    __slots__ = ("_motet",)

    def __init__(self, motet: "MotetContext") -> None:
        self._motet = motet

    def create(self, target_command_type: str, target_command_data: Dict[str, Any], schedule_type: str, **kwargs: Any) -> Any:
        """Schedule a command. Uses ScheduleCommand (class-based) via motet.do()."""
        if not getattr(self._motet, "task_id", None):
            raise RuntimeError("motet.schedules.create requires task context; use motet.do(ScheduleCommand, data=...) when outside a command")
        from motet.core.commands.builtin.schedule import ScheduleCommand
        from motet.core.commands.command_data_classes import ScheduleData
        data = ScheduleData(target_command_type=target_command_type, target_command_data=target_command_data, schedule_type=schedule_type, **kwargs)
        # Propagate identity so scheduled rows stamp the caller's tenant/principal
        # (otherwise DistributedCommand defaults tenant_id to "default" and
        # lifecycle tools reject cancel/suspend as cross-tenant).
        return self._motet.do(
            ScheduleCommand(
                task_id=self._motet.task_id,
                conversation_id=getattr(self._motet, "conversation_id", "") or "",
                tenant_id=getattr(self._motet, "tenant_id", "") or "",
                principal_id=getattr(self._motet, "principal_id", "") or "",
                motet_id=getattr(self._motet, "motet_id", "") or "",
                data=data,
            )
        )

    def list(self) -> List[Any]:
        """List schedules from worker context schedule manager if available."""
        manager = getattr(self._motet, "_worker_context", {}).get("schedule_manager")
        if manager and hasattr(manager, "list_schedules"):
            return manager.list_schedules()
        return []


class MotetCommandsHelper:
    """
    ADR-0084: Facade for command discovery and run-by-type (dynamic dispatch).

    Use when you have a command type string (e.g. from API or workflow config) and want to
    list available commands or execute by type without importing each command.
    """

    __slots__ = ("_motet",)

    def __init__(self, motet: "MotetContext") -> None:
        self._motet = motet

    def list(self, bundle_id: Optional[str] = None) -> List[str]:
        """List registered command type identifiers (optionally filtered by bundle_id)."""
        from motet.core.commands.command_type_registry import command_type_registry
        return command_type_registry.get_command_types(bundle_id=bundle_id)

    def get(self, command_type: str) -> Optional[Any]:
        """Resolve command type to implementation (function or class). Returns None if not registered."""
        from motet.core.commands.command_type_registry import command_type_registry
        from motet.core.commands.distributed import DistributedCommand
        DistributedCommand._ensure_commands_registered()
        # Accept bare name (e.g. "tool_execution") or qualified ("core.tool_execution")
        key = command_type
        if not key.startswith("core.") and command_type_registry.get(key) is None:
            key = "core." + key
        reg = command_type_registry.get(key)
        return reg.implementation if reg else None

    def run(self, command_type: str, data: Any, **kwargs: Any) -> Any:
        """Execute a command by type. Delegates to motet.do(implementation, data=...). Requires task context."""
        if not getattr(self._motet, "task_id", None):
            raise RuntimeError("motet.commands.run requires task context; use motet.do(cmd, data=...) when outside a command")
        impl = self.get(command_type)
        if impl is None:
            available = self.list()
            raise ValueError(f"Unknown command type: {command_type}. Available: {available[:20]}{'...' if len(available) > 20 else ''}")
        return self._motet.do(impl, data=data, **kwargs)


class MotetConversationsHelper:
    """
    ADR-0084: Facade over conversation commands (list/get/clear/register/rename).

    Delegates to conversations_list, conversation_get, conversation_clear,
    conversation_register, conversation_rename when task context exists.
    list() uses inner path (list_conversations_sync) when no task_id; get/clear/register/rename
    require task context and raise if absent.
    """

    __slots__ = ("_motet",)

    def __init__(self, motet: "MotetContext") -> None:
        self._motet = motet

    def _require_identity(self) -> tuple[str, str, str]:
        """Require non-empty motet/tenant/principal identity in MotetContext."""
        motet_id, tenant_id, principal_id = require_context_identity(
            self._motet,
            operation="motet.conversations.list",
        )
        return motet_id, tenant_id, principal_id

    def list(
        self,
        limit: int = 100,
        agent_id: Optional[str] = None,
        surface_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List conversations. Delegates to conversations_list when task_id set; else uses list_conversations_sync."""
        if getattr(self._motet, "task_id", None):
            from motet.core.commands.builtin.conversation import conversations_list
            from motet.core.commands.command_data_classes import ListConversationsData
            result = self._motet.do(
                conversations_list,
                data=ListConversationsData.model_validate(
                    {
                        "limit": limit,
                        "agent_id": agent_id,
                        "surface_id": surface_id,
                    }
                ),
            )
            return result.get("conversations", [])
        from motet.core.agents import resolve_agent_id
        from motet.core.conversations.registry import list_conversations_sync
        motet_id, tenant_id, principal_id = self._require_identity()
        effective_agent_id = resolve_agent_id(agent_id)
        convs = list_conversations_sync(
            motet_id=motet_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            limit=limit,
            agent_id=effective_agent_id,
            surface_id=surface_id,
        )
        return convs

    def get(self, conversation_id: str) -> Dict[str, Any]:
        """Get one conversation (history + counts). Delegates to conversation_get. Requires task context."""
        if not getattr(self._motet, "task_id", None):
            raise RuntimeError(
                "motet.conversations.get requires task context; use motet.do(conversation_get, data=...) when outside a command"
            )
        from motet.core.commands.builtin.conversation import conversation_get
        from motet.core.commands.command_data_classes import GetConversationData
        return self._motet.do(conversation_get, data=GetConversationData(conversation_id=conversation_id))

    def clear(self, conversation_id: str) -> Dict[str, Any]:
        """Clear a conversation and isolated descendants (registry + memory/vector). Delegates to conversation_clear. Requires task context."""
        if not getattr(self._motet, "task_id", None):
            raise RuntimeError(
                "motet.conversations.clear requires task context; use motet.do(conversation_clear, data=...) when outside a command"
            )
        from motet.core.commands.builtin.conversation import conversation_clear
        from motet.core.commands.command_data_classes import ClearConversationData
        return self._motet.do(conversation_clear, data=ClearConversationData(conversation_id=conversation_id))

    def register(
        self,
        conversation_id: str,
        title: Optional[str] = None,
        agent_id: Optional[str] = None,
        surface_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Register or touch a conversation. Delegates to conversation_register. Requires task context."""
        if not getattr(self._motet, "task_id", None):
            raise RuntimeError(
                "motet.conversations.register requires task context; use motet.do(conversation_register, data=...) when outside a command"
            )
        from motet.core.commands.builtin.conversation import conversation_register
        from motet.core.commands.command_data_classes import RegisterConversationData
        return self._motet.do(
            conversation_register,
            data=RegisterConversationData(
                conversation_id=conversation_id,
                title=title,
                agent_id=agent_id,
                surface_id=surface_id,
            ),
        )

    def rename(self, conversation_id: str, title: str) -> Dict[str, Any]:
        """Rename a conversation. Delegates to conversation_rename. Requires task context."""
        if not getattr(self._motet, "task_id", None):
            raise RuntimeError(
                "motet.conversations.rename requires task context; use motet.do(conversation_rename, data=...) when outside a command"
            )
        from motet.core.commands.builtin.conversation import conversation_rename
        from motet.core.commands.command_data_classes import UpdateConversationTitleData
        return self._motet.do(
            conversation_rename,
            data=UpdateConversationTitleData(conversation_id=conversation_id, title=title),
        )


def _resolve_stream_agent_id_plaintext(motet: "MotetContext") -> Optional[str]:
    """
    Qualified agent_id for task stream plaintext (ADR-0083); matches memory scope resolution.
    """
    from motet.core.agents import resolve_agent_id

    raw: Optional[str] = None
    for attr in ("agent_id", "configured_agent_id"):
        v = getattr(motet, attr, None)
        if v and str(v).strip():
            raw = str(v).strip()
            break
    if not raw:
        meta = motet.metadata if hasattr(motet, "metadata") else {}
        if isinstance(meta, dict):
            for key in ("agent_id", "configured_agent_id", "configured_agent_qualified_id"):
                v = meta.get(key)
                if v and str(v).strip():
                    raw = str(v).strip()
                    break
    if not raw:
        return None
    try:
        return resolve_agent_id(raw)
    except Exception:
        return raw


def _resolve_stream_parent_agent_id_plaintext(motet: "MotetContext") -> Optional[str]:
    """Immediate parent agent_id for nested loops, when metadata carries one."""
    from motet.core.agents import resolve_agent_id

    meta = motet.metadata if hasattr(motet, "metadata") else {}
    if not isinstance(meta, dict):
        return None
    raw = meta.get("parent_agent_id")
    if not raw or not str(raw).strip():
        return None
    parent = str(raw).strip()
    try:
        return resolve_agent_id(parent)
    except Exception:
        return parent


def _error_dict_from_envelope(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Extract a plain error dict from an ADR-0029 envelope dict or model dump."""
    error_info = envelope.get("error") or {}
    if not isinstance(error_info, dict):
        error_info = {}
    return {
        "type": error_info.get("type", "UnknownError"),
        "message": error_info.get("message", "Command execution failed"),
        "details": error_info.get("details", {}),
        "recoverable": error_info.get("recoverable", False),
        "retry_recommended": error_info.get("retry_recommended", False),
    }


def unwrap_child_envelope(result: Any) -> Any:
    """Unwrap one gather/map child: data on success, ``{_error: True, ...}`` on failure.

    Used by ``join`` and ``apply`` so authors never see child envelopes.
    ``join`` attaches the same unwrapped list to ``GatherExecutionError.partial_results``.
    """
    from motet.core.commands.response_models import parse_command_envelope
    from pydantic import ValidationError

    try:
        envelope = parse_command_envelope(result)
    except (ValidationError, TypeError, ValueError):
        return result
    if envelope.status == "success":
        return envelope.data
    error_info = _error_dict_from_envelope(
        envelope.model_dump(mode="json")
    )
    return {
        "_error": True,
        "error_type": error_info["type"],
        "message": error_info["message"],
        "details": error_info["details"],
    }


class MotetContext:
    """
    Unified context for decorated distributed commands.
    
    Provides strongly-typed access to command execution context and helper methods
    for command composition, resource access, streaming, and response formatting.
    
    ADR-0030: Decorator-Based Command Pattern
    
    Attributes:
        task_id: Current task identifier
        conversation_id: Current conversation identifier
        command_id: Current command identifier
        tenant_id: Tenant context
        principal_id: User/principal context
        metadata: Command metadata
        stream_key: Redis stream key (for streaming commands)
        redis: Redis connection (for state/streaming)
        
    Resource Access:
        stack: MotetStack instance for core functionality
        agent: LLM agent for model inference
        memory: Memory manager for conversation context
        tools: Tool registry for tool execution
        vault: Vault client for secure credentials
        event_bus: Event bus for publishing custom events
        observer_manager: Event observer manager for observing events (advanced)
        
    Command Composition:
        call(): Execute another command and wait for result (blocking)
        gather(): Execute multiple commands in parallel and wait (blocking)
        dispatch(): Dispatch commands without waiting (fire-and-forget)
        map(): Apply same command to multiple inputs (batch processing)
        
    Concise Command Composition Helpers (ADR-0052):
        do(): Execute command and unwrap data or raise exception
        join(): Execute commands in parallel and unwrap all results or raise exception
        apply(): Apply command to multiple inputs and unwrap results
        maybe(): Execute command and return (data, error) tuple for optional handling
        
    Streaming Helpers:
        ensure_stream(): Ensure stream exists with TTL (unified task stream by default)
        stream_event(): Stream events to unified task stream (default) or custom stream
        stream_token(): Stream LLM tokens (convenience)
        reset_stream(): Reset (delete) command-specific stream (for class-based streams)
        forward_stream_events(): Forward events from another stream (for class-based streams)
        finalize_stream(): Finalize command-specific stream (for class-based streams)
        
    Event Bus Helpers (Pub/Sub):
        publish_event(): Publish event to EventBus (convenience wrapper)
        observe_events(): Observe events from EventBus during execution (context manager)
        
    Response Helpers:
        add_warning(): Attach a non-fatal warning to the decorator envelope
        last_metadata: CommandMetadata from the most recent do/join/apply/maybe
    """
    
    def __init__(
        self,
        command_instance: Optional[DistributedCommand] = None,
        redis: Optional[Any] = None,
        worker_context: Optional[Dict[str, Any]] = None,
        # Fallback parameters for testing (deprecated - use command_instance in production)
        task_id: str = "",
        conversation_id: str = "",
        command_id: str = "",
        tenant_id: str = "",
        principal_id: str = "",
        motet_id: str = "",
        metadata: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize MotetContext.
        
        In production, only command_instance should be provided. The context
        properties will delegate to the command instance's distributed_context.
        
        For testing, fallback parameters can be provided, but this is deprecated.
        
        Args:
            command_instance: Reference to the command instance (provides all context)
            redis: Redis connection
            worker_context: Full worker context dict
            task_id: Fallback task identifier (for testing only)
            conversation_id: Fallback conversation identifier (for testing only)
            command_id: Fallback command identifier (for testing only)
            tenant_id: Fallback tenant identifier (for testing only)
            principal_id: Fallback principal identifier (for testing only)
            motet_id: Fallback motet/environment identifier (for testing only)
            metadata: Fallback metadata (for testing only)
        """
        self._command = command_instance
        self.redis = redis
        self._worker_context = worker_context or {}
        
        # Fallbacks for testing (only used when command_instance is None)
        self._task_id_fallback = task_id
        self._conversation_id_fallback = conversation_id
        self._command_id_fallback = command_id
        self._tenant_id_fallback = tenant_id
        self._principal_id_fallback = principal_id
        self._motet_id_fallback = motet_id
        self._metadata_fallback = metadata or {}

        # Initialize token buffer state per-instance
        self._token_buffer_lock = WorkerLock()
        self._token_buffer_parts = []
        self._token_buffer_chars = 0
        self._token_last_flush_s = 0.0
        self._warnings: List[str] = []
        self._last_metadata: Optional[Any] = None
    
    # === Context Properties (delegate to command instance) ===
    
    @property
    def task_id(self) -> str:
        """Task identifier (delegates to command if available, else uses fallback)."""
        if self._command:
            return self._command.distributed_context.task_id
        return self._task_id_fallback
    
    @property
    def conversation_id(self) -> str:
        """Conversation identifier (delegates to command if available, else uses fallback)."""
        if self._command:
            return self._command.distributed_context.conversation_id
        return self._conversation_id_fallback
    
    @property
    def command_id(self) -> str:
        """Command identifier (delegates to command if available, else uses fallback)."""
        if self._command:
            return self._command.command_id
        return self._command_id_fallback
    
    @property
    def tenant_id(self) -> str:
        """Tenant identifier (delegates to command if available, else uses fallback)."""
        if self._command:
            return self._command.distributed_context.tenant_id
        return self._tenant_id_fallback
    
    @property
    def principal_id(self) -> str:
        """Principal/user identifier (delegates to command if available, else uses fallback)."""
        if self._command:
            return self._command.distributed_context.principal_id
        return self._principal_id_fallback
    
    @property
    def motet_id(self) -> str:
        """Motet/environment identifier (delegates to command if available, else uses fallback)."""
        if self._command:
            return self._command.distributed_context.motet_id
        return self._motet_id_fallback
    
    @property
    def metadata(self) -> Dict[str, Any]:
        """
        Metadata (delegates to command if available, else uses fallback).

        Always returns a live mutable dict. Do not use ``metadata or {}`` —
        an empty ``{}`` is falsy and would return a throwaway new dict, so
        callers stamping keys (e.g. model_provider for web_search) would
        silently lose updates.
        """
        if self._command:
            meta = self._command.distributed_context.metadata
            if meta is None:
                self._command.distributed_context.metadata = {}
                return self._command.distributed_context.metadata
            return meta
        return self._metadata_fallback

    @property
    def cancel_scopes(self) -> List[str]:
        """Cancel scopes this command lives under (ADR-0131). Empty when no command."""
        if self._command:
            return list(
                getattr(self._command.distributed_context, "cancel_scopes", None) or []
            )
        return []

    def push_cancel_scope(self, scope_id: str) -> None:
        """Mark this command as a cancellable subtree and inherit the scope downward."""
        if not self._command:
            return
        from motet.core.distributed.task_control import push_own_cancel_scope

        push_own_cancel_scope(self._command, scope_id)

    def _composition_cancel_kwargs(self) -> Dict[str, Any]:
        scopes = self.cancel_scopes
        return {"cancel_scopes": scopes} if scopes else {}

    def _remaining_timeout_seconds(self) -> Optional[int]:
        return remaining_command_timeout_seconds(self._command)

    def _with_composition_timeout(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Inherit remaining parent budget unless the caller set a timeout."""
        out = dict(kwargs)
        if out.get("timeout_seconds") is None:
            remaining = self._remaining_timeout_seconds()
            if remaining is not None:
                out["timeout_seconds"] = remaining
        return out
    
    @property
    def stream_key(self) -> str:
        """
        Redis stream key for this command (single source of truth for streaming).
        
        Returns the stream key from the command instance if available,
        otherwise defaults to unified task stream (ADR-0050).
        
        Stream key is set during command initialization with priority:
        1. data.stream_key (runtime, highest priority)
        2. @motet.command(stream_key=...) (decorator config)
        3. Unified task stream (default: {tenant}:task:{task_id}:response)
        
        Returns:
            Stream key (unified task stream by default)
            
        Example:
            # Access stream key (uses command's stream_key or unified task stream)
            stream_key = motet.stream_key
            
            # All stream methods use this automatically
            motet.stream_event("event")  # Uses motet.stream_key
        """
        if self._command and hasattr(self._command, 'stream_key') and self._command.stream_key:
            return self._command.stream_key
        # Default to unified task stream (ADR-0050 / issue #228)
        from motet.core.distributed.tenant_keys import task_response_stream

        return task_response_stream(self.tenant_id, self.task_id)
    
    @property
    def distributed_context(self) -> Any:
        """
        Full distributed context (advanced use only).
        
        Provides access to the complete DistributedCommandContext including
        routing, priority, retry settings, and tracing information.
        
        For most use cases, prefer the convenience properties like motet.task_id,
        motet.conversation_id, etc.
        
        Returns:
            DistributedCommandContext instance or None if no command
            
        Example:
            # Advanced: Access routing hints
            target_worker = motet.distributed_context.target_worker_id
            priority = motet.distributed_context.priority
            
            # Advanced: Access retry settings
            max_retries = motet.distributed_context.max_retries
            
            # Preferred: Use convenience properties
            task_id = motet.task_id  # Simpler than motet.distributed_context.task_id
        """
        return self._command.distributed_context if self._command else None
    
    # === Resource Access (strongly typed) ===
    
    @property
    def memory(self) -> Any:
        """
        Memory helper for conversation memory (ADR-0084: delegates store/recall/tag/forget to commands when context exists).
        
        Returns:
            MotetMemoryHelper when memory_manager present, else None
            
        Example:
            motet.memory.store(content="preference", type="note", tags=["user"])
            items = motet.memory.recall(query="preference", limit=5)
        """
        if self._worker_context.get("memory_manager") is None:
            return None
        return MotetMemoryHelper(self)
    
    @property
    def stack(self) -> Any:
        """
        MotetStack instance for accessing core functionality.
        
        Returns:
            MotetStack instance from worker context
            
        Example:
            tool_registry = motet.stack.tool_registry
            memory_manager = motet.stack.memory_manager
        """
        return self._worker_context.get("stack")
    
    @property
    def tools(self) -> Any:
        """
        Tool registry for accessing available tools (ADR-0084: delegates execute to tool_execution when context exists).
        
        Returns:
            MotetToolsHelper wrapping the registry when registry is present, else None
            
        Example:
            weather_tool = motet.tools.get("weather")
            result = motet.tools.execute("core.weather", {"location": "San Francisco"})
        """
        reg = self._worker_context.get("tool_registry")
        if reg is None:
            return None
        return MotetToolsHelper(self, reg)

    @property
    def agents(self) -> "MotetAgentsHelper":
        """Agent facade (list/get/turn); turn delegates to agent_turn command."""
        return MotetAgentsHelper(self)

    @property
    def models(self) -> "MotetModelsHelper":
        """Model facade (list/get/infer/stream); infer/stream delegate to model commands."""
        return MotetModelsHelper(self)

    @property
    def workflows(self) -> "MotetWorkflowsHelper":
        """Workflow facade (list/get/run); run delegates to workflow_execution command."""
        return MotetWorkflowsHelper(self)

    @property
    def schedules(self) -> "MotetSchedulesHelper":
        """Schedule facade (create/list); create uses ScheduleCommand."""
        return MotetSchedulesHelper(self)

    @property
    def commands(self) -> "MotetCommandsHelper":
        """Command discovery and run-by-type (list/get/run) for dynamic dispatch."""
        return MotetCommandsHelper(self)

    @property
    def conversations(self) -> "MotetConversationsHelper":
        """Conversations helper (list/get/clear/register/rename); delegates to conversation commands when context exists."""
        return MotetConversationsHelper(self)

    @property
    def function_discovery_store(self) -> Optional[Any]:
        """
        Function discovery vector store for semantic search (ADR-0051).
        
        Returns:
            FunctionDiscoveryVectorStore instance from worker context, or None if not available
            
        Example:
            if motet.function_discovery_store:
                results = motet.function_discovery_store.search_functions("get weather")
        """
        return self._worker_context.get("function_discovery_store")
    
    @property
    def vault(self) -> Any:
        """
        Vault client for secure credential access.
        
        Returns:
            Vault client instance (if command has one)
            
        Example:
            api_key = motet.vault.get_secret("api_keys/openai")
        """
        if self._command:
            return self._command.get_vault_client()
        return None
    
    @property
    def event_bus(self) -> Any:
        """
        Event bus for publishing custom events.
        
        Returns:
            EventBus instance from worker context
            
        Example:
            motet.event_bus.publish({
                "kind": "payment_initiated",
                "source": "payment_service",
                "data": {"amount": 100, "currency": "USD"},
                "timestamp": datetime.utcnow().isoformat(),
                "priority": 5,
                "correlation_id": motet.command_id,
                "tags": ["payment", "audit"],
                "metadata": {}
            })
        """
        return self._worker_context.get("event_bus")
    
    @property
    def observer_manager(self) -> Any:
        """
        Event observer manager for registering custom observers.
        
        Advanced use - most commands should use motet.event_bus for simple
        event publishing. Use this when you need to observe/react to events
        from other commands during execution.
        
        For common cases, prefer motet.observe_events() context manager.
        
        Returns:
            EventObserverManager instance from worker context
            
        Example:
            import structlog
            from motet.core.workers.observers import Observer, EventFilter
            
            logger = structlog.get_logger(__name__)
            
            class MyObserver(Observer):
                def get_event_filter(self):
                    return EventFilter(event_types={"payment_completed"})
                
                def on_event(self, event):
                    logger.info("payment_completed", data=event.data)
            
            observer = MyObserver("my_observer")
            motet.observer_manager.register_observer(observer)
            try:
                # Do work that triggers events
                result = process_payments(...)
            finally:
                motet.observer_manager.unregister_observer(observer)
        """
        return self._worker_context.get("observer_manager")
    
    @property
    def artifact_store(self) -> Any:
        """
        Artifact store with pre-bound isolation context.
        
        Provides convenient access to artifact storage with automatic tenant/principal/motet
        scoping. All isolation parameters are pre-bound from the command's execution context,
        eliminating repetitive parameter passing.
        
        Returns:
            ScopedArtifactStore instance with pre-bound isolation context
            
        Example:
            # Get artifact metadata (no isolation params needed)
            meta = motet.artifact_store.get_metadata(artifact_id)
            
            # Store new artifact
            new_id = motet.artifact_store.put(
                payload=b"data",
                content_type="text/plain",
                kind=ArtifactKind.USER_UPLOAD
            )
            
            # Find derived artifact
            existing = motet.artifact_store.find_derived(
                source_id,
                ArtifactKind.DERIVED_IMAGE_BASE
            )
        """
        # Lazy initialization on first access
        if not hasattr(self, "_scoped_artifact_store"):
            from motet.core.artifacts.scoped_store import ScopedArtifactStore
            from motet.core.artifacts import get_artifact_store
            
            self._scoped_artifact_store = ScopedArtifactStore(
                store=get_artifact_store(),
                tenant_id=self.tenant_id,
                principal_id=self.principal_id,
                motet_id=self.motet_id,
            )
        
        return self._scoped_artifact_store
    
    def resolve_conversation_id(self, explicit_id: Optional[str] = None) -> str:
        """
        Resolve conversation ID with explicit override, context fallback, or empty string.
        
        This centralizes the common pattern of preferring an explicit conversation_id
        parameter while falling back to the command's execution context.
        
        Args:
            explicit_id: Explicit conversation ID (e.g., from command data)
            
        Returns:
            Resolved conversation ID (never None, empty string if not available)
            
        Example:
            # In command with optional conversation_id in data
            conversation_id = motet.resolve_conversation_id(data.conversation_id)
            
            # Store artifact with resolved conversation_id
            artifact_id = motet.artifact_store.put(
                payload=data.payload,
                metadata={"conversation_id": conversation_id}
            )
        """
        return explicit_id or self.conversation_id or ""
    
    def log_fields(self, **extra) -> Dict[str, Any]:
        """
        Get standard logging fields for distributed context.
        
        Provides consistent structured logging fields across all commands,
        reducing boilerplate in log statements.
        
        Args:
            **extra: Additional fields to merge into the result
            
        Returns:
            Dict with standard distributed context fields plus extras
            
        Example:
            import structlog
            logger = structlog.get_logger(__name__)
            
            # Standard pattern (old):
            logger.info(
                "create_artifact_started",
                artifact_id=artifact_id,
                bytes=len(payload),
                tenant_id=motet.tenant_id,
                principal_id=motet.principal_id,
                motet_id=motet.motet_id,
                task_id=motet.task_id,
                command_id=motet.command_id,
            )
            
            # With log_fields helper (new):
            logger.info(
                "create_artifact_started",
                **motet.log_fields(artifact_id=artifact_id, bytes=len(payload))
            )
        """
        return {
            "tenant_id": self.tenant_id,
            "principal_id": self.principal_id,
            "motet_id": self.motet_id,
            "task_id": self.task_id,
            "command_id": self.command_id,
            **extra
        }
    
    def observe_events(
        self,
        event_types: Set[str],
        callback: Callable[[Any], None],
        priority: Optional[int] = None,
        custom_filter: Optional[Callable[[Any], bool]] = None
    ) -> "TemporaryObserver":
        """
        Observe specific events during command execution (context manager).
        
        Automatically registers and unregisters observer for clean lifecycle.
        This is the recommended way to observe events - simpler than manually
        managing observer_manager registration/unregistration.
        
        Args:
            event_types: Set of event types to observe (e.g., {"payment_completed"})
            callback: Function to call when events match (receives Event object)
            priority: Minimum priority level (default: EventPriority.NORMAL)
            custom_filter: Optional additional filter function
            
        Returns:
            Context manager that handles observer lifecycle
            
        Example:
            import structlog
            logger = structlog.get_logger(__name__)
            # Observe payment events during order processing
            def handle_payment(event):
                logger.info("payment", amount=event.data.get('amount'))
            
            with motet.observe_events(
                event_types={"payment_completed", "payment_failed"},
                callback=handle_payment
            ):
                # Events are captured during this block
                result = process_payment_batch(...)
            # Observer automatically unregistered here
            
        Advanced example with custom filter:
            import structlog
            logger = structlog.get_logger(__name__)
            def handle_large_payment(event):
                logger.info("large_payment", data=event.data)
            
            with motet.observe_events(
                event_types={"payment_completed"},
                callback=handle_large_payment,
                custom_filter=lambda event: event.data.get("amount", 0) > 1000
            ):
                process_payments(...)
        """
        from motet.core.workers.observers import EventPriority
        
        return TemporaryObserver(
            observer_manager=self.observer_manager,
            event_types=event_types,
            callback=callback,
            priority=priority if priority is not None else EventPriority.NORMAL,
            custom_filter=custom_filter,
            command_id=self.command_id
        )
    
    # === Streaming Helpers ===
    
    def ensure_stream(self, ttl_seconds: int = 3600, stream_key: Optional[str] = None) -> None:
        """
        Ensure stream exists with proper TTL (uses motet.stream_key by default).
        
        Sets TTL on the stream without deleting it. Safe to call multiple times.
        This is different from reset_stream() which deletes command-specific streams.
        
        Args:
            ttl_seconds: Time-to-live for stream in seconds (default: 1 hour)
            stream_key: Optional stream key. If None, uses motet.stream_key (single source of truth).
            
        Example:
            # At start of task execution (uses motet.stream_key)
            motet.ensure_stream(ttl_seconds=3600)
            
            # Custom stream (overrides motet.stream_key)
            motet.ensure_stream(ttl_seconds=3600, stream_key="custom:stream:key")
            
            # Stream events
            motet.stream_event("start", command_type="agent_turn")
        """
        if not self.redis:
            return
        
        # Use motet.stream_key as single source of truth if not explicitly provided
        if stream_key is None:
            stream_key = self.stream_key
        
        try:
            # Set TTL without deleting (stream may already exist with events)
            self.redis.expire(stream_key, ttl_seconds)
        except Exception as e:
            logger.warning("stream_ttl_failed",
                          stream_key=stream_key,
                          error=str(e))
    
    # === Token streaming buffer (perf) ===
    #
    # Problem: model_stream currently emits one Redis XADD (plus encryption) per token.
    # That becomes the bottleneck before the model in high-throughput scenarios.
    #
    # Approach: buffer token payloads and flush as chunked "token" events, either when:
    # - the buffer reaches a max size, or
    # - enough time has elapsed since the last flush (default ~50ms)
    #
    # Notes:
    # - This is intentionally "best-effort realtime": we flush opportunistically when tokens arrive,
    #   and always flush before emitting non-token events (e.g. end/stream_complete/error).
    # - Uses WorkerLock to remain pool-agnostic (ADR-0033).

    def _token_buffer_config(self) -> tuple[int, float]:
        """Return (max_chars, flush_interval_seconds)."""
        import os

        max_chars = int(os.getenv("MOTET_STREAM_TOKEN_MAX_CHARS", "1024") or "1024")
        flush_ms = float(os.getenv("MOTET_STREAM_TOKEN_FLUSH_MS", "50") or "50")
        flush_interval_s = max(0.0, flush_ms / 1000.0)
        # Safety caps (allow small values for testing/tuning; defaults are sane for prod)
        if max_chars < 1:
            max_chars = 1
        if max_chars > 32_768:
            max_chars = 32_768
        if flush_interval_s < 0.0:
            flush_interval_s = 0.0
        return max_chars, flush_interval_s

    def flush_token_buffer(self, stream_key: Optional[str] = None) -> None:
        """Force-flush any buffered tokens as a single `token` stream event."""
        if not self.redis:
            return
        if stream_key is None:
            stream_key = self.stream_key

        with self._token_buffer_lock:
            if not self._token_buffer_parts:
                return
            chunk = "".join(self._token_buffer_parts)
            self._token_buffer_parts = []
            self._token_buffer_chars = 0
            self._token_last_flush_s = 0.0

        # Use raw event emission to avoid recursively re-buffering.
        self._stream_event_raw("token", stream_key=stream_key, data=chunk)

    def _stream_event_raw(self, event_type: str, stream_key: Optional[str] = None, **fields) -> None:
        """
        Low-level stream event emission (no token buffering).

        This contains the original `stream_event` implementation body.
        """
        if not self.redis:
            return

        import time

        if stream_key is None:
            stream_key = self.stream_key

        # ADR-0056: Do not store sensitive stream bodies (tokens/content) in plaintext.
        # Keep minimal routing/ops metadata plaintext; encrypt event-specific fields into `_envelope`.
        try:
            tenant_id = require_context_field(
                self,
                field_name="tenant_id",
                operation="command stream encryption",
                error_template="{field} is required for {operation}",
            )
        except ValueError:
            logger.error(
                "stream_event_missing_tenant_id",
                task_id=self.task_id,
                command_id=self.command_id,
                event_type=event_type,
            )
            raise
        try:
            motet_id = require_context_field(
                self,
                field_name="motet_id",
                operation="command stream encryption",
                error_template="{field} is required for {operation}",
            )
        except ValueError:
            logger.error(
                "stream_event_missing_motet_id",
                task_id=self.task_id,
                command_id=self.command_id,
                event_type=event_type,
            )
            raise

        plaintext_fields: Dict[str, Any] = {
            "event": event_type,
            "timestamp": time.time(),
            "command_id": self.command_id,
            "task_id": self.task_id,
            # Include tenant_id and motet_id in plaintext for AAD reconstruction during decryption
            "tenant_id": tenant_id,
            "motet_id": motet_id,
        }
        stream_agent_id = _resolve_stream_agent_id_plaintext(self)
        if stream_agent_id:
            plaintext_fields["agent_id"] = stream_agent_id
        stream_parent_id = _resolve_stream_parent_agent_id_plaintext(self)
        if stream_parent_id:
            plaintext_fields["parent_agent_id"] = stream_parent_id
        payload: Dict[str, Any] = {k: v for k, v in fields.items() if v is not None}

        # AAD binding (ADR-0056 Phase 6): bind ciphertext to stream + ids to prevent cut-and-paste.
        from motet.core.security.aad_helpers import compute_command_stream_aad

        aad = compute_command_stream_aad(
            stream_key=stream_key,
            event=event_type,
            task_id=self.task_id,
            command_id=self.command_id,
            tenant_id=tenant_id,
            motet_id=motet_id,
        )

        try:
            encrypted_fields = encode_encrypted_message_data(
                message_data=payload,
                tenant_id=tenant_id,
                motet_id=motet_id,
                context=EncryptionContext.COMMAND_STREAM.value,
                include_plaintext=plaintext_fields,
                aad=aad,
            )
            self.redis.xadd(stream_key, encrypted_fields, maxlen=10000)
        except Exception as e:
            logger.warning(
                "stream_event_failed",
                task_id=self.task_id,
                event_type=event_type,
                stream_key=stream_key,
                error=str(e),
            )
    
    def stream_event(self, event_type: str, stream_key: Optional[str] = None, **fields) -> None:
        """
        Stream an event to Redis stream (uses motet.stream_key by default).
        
        Stream key resolution (when stream_key=None):
        - Uses motet.stream_key (single source of truth)
        - motet.stream_key is set during command initialization with priority:
          1. data.stream_key (runtime, highest priority)
          2. @motet.command(stream_key=...) (decorator config)
          3. Unified task stream (default: {tenant}:task:{task_id}:response)
        
        Args:
            event_type: Event type (e.g., "token", "reasoning_step", "turn")
            stream_key: Optional custom stream key. If None, uses motet.stream_key.
            **fields: Event-specific fields (e.g., data="...", state="PREPARING")
            
        Example:
            # Use motet.stream_key (default - respects data or decorator config)
            motet.stream_event("start", command_type="agent_turn")
            motet.stream_event("turn", state="PREPARING")
            
            # Override with explicit stream key
            motet.stream_event("debug", stream_key="debug:stream:key", info="...")
        """
        if not self.redis:
            return
        
        # Use motet.stream_key as single source of truth if not explicitly provided
        if stream_key is None:
            stream_key = self.stream_key
        
        # Route token events through the buffering path even if called via stream_event().
        if event_type == "token":
            token = fields.get("data")
            if token is not None:
                self.stream_token(str(token), stream_key=stream_key)
                return

        # Preserve ordering: flush any buffered tokens before emitting non-token events.
        self.flush_token_buffer(stream_key=stream_key)

        self._stream_event_raw(event_type, stream_key=stream_key, **fields)
    
    def stream_token(self, token: str, stream_key: Optional[str] = None) -> None:
        """
        Stream a token (convenience for LLM responses).
        
        Args:
            token: Token/text to stream
            
        Example:
            for token in llm_response:
                motet.stream_token(token)
        """
        if not self.redis:
            return
        if stream_key is None:
            stream_key = self.stream_key

        if not token:
            return

        import time

        max_chars, flush_interval_s = self._token_buffer_config()
        now = time.monotonic()

        # Warn if a single token is huge (potential full-text emission bug)
        if len(token) > 1000:
            logger.warning(
                "large_token_emitted",
                length=len(token),
                preview=token[:100],
                stream_key=stream_key
            )

        with self._token_buffer_lock:
            # self._token_buffer_parts is initialized in __init__
            self._token_buffer_parts.append(token)
            self._token_buffer_chars += len(token)

            should_flush = False
            if self._token_buffer_chars >= max_chars:
                should_flush = True
            elif flush_interval_s > 0.0 and (now - (self._token_last_flush_s or now)) >= flush_interval_s:
                should_flush = True

            if not should_flush:
                return

            chunk = "".join(self._token_buffer_parts)
            self._token_buffer_parts = []
            self._token_buffer_chars = 0
            self._token_last_flush_s = now
        
        # Emit outside the lock.
        self._stream_event_raw("token", stream_key=stream_key, data=chunk)
    
    def reset_stream(self) -> None:
        """
        Reset (delete) the Redis stream for this command (only if streaming enabled).
        
        WARNING: This deletes the stream and all existing events. Only use for
        command-specific streams, not unified task streams.
        
        Raises:
            ValueError: If called on a command configured with unified task stream.
        
        Call this at the start of streaming commands to clear any existing data.
        
        Example:
            motet.reset_stream()  # Deletes command-specific stream
        """
        if self._command and hasattr(self._command, '_stream_enabled') and self._command._stream_enabled:
            # Safety check: prevent deletion of unified task streams
            stream_key = self.stream_key
            from motet.core.distributed.tenant_keys import is_unified_task_response_stream

            if is_unified_task_response_stream(stream_key):
                raise ValueError(
                    f"Cannot reset unified task stream '{stream_key}'. "
                    f"This would delete events from other commands. "
                    f"Use ensure_stream() for TTL management instead."
                )
            
            if hasattr(self._command, '_reset_stream'):
                self._command._reset_stream(self.redis)
    
    def finalize_stream(self, ttl_seconds: int = 3600) -> None:
        """
        Finalize the Redis stream for this command (only if streaming enabled).
        
        NOTE: For unified task streams, use ensure_stream() instead, which is
        safe to call multiple times and doesn't interfere with other commands.
        
        Raises:
            ValueError: If called on a command configured with unified task stream.
        
        Args:
            ttl_seconds: Time-to-live for stream in seconds (default: 1 hour)
            
        Example:
            motet.finalize_stream(ttl_seconds=3600)  # 1 hour TTL (command-specific only)
        """
        if self._command and hasattr(self._command, '_stream_enabled') and self._command._stream_enabled:
            # Safety check: prevent finalization of unified task streams
            stream_key = self.stream_key
            from motet.core.distributed.tenant_keys import is_unified_task_response_stream

            if is_unified_task_response_stream(stream_key):
                raise ValueError(
                    f"Cannot finalize unified task stream '{stream_key}'. "
                    f"Use ensure_stream(ttl_seconds={ttl_seconds}) instead, "
                    f"which is safe for unified task streams."
                )
            
            if hasattr(self._command, '_finalize_stream'):
                self._command._finalize_stream(self.redis, ttl_seconds)
    
    def forward_stream_events(
        self, 
        source_stream_key: str, 
        event_mapping: Optional[Dict[str, str]] = None
    ) -> None:
        """
        Forward events from another stream to this command's stream (only if streaming enabled).
        
        Useful for composing commands that delegate to other streaming commands.
        This is a blocking operation that reads until the source stream ends.
        
        Args:
            source_stream_key: The stream key to read events from
            event_mapping: Optional mapping of source event types to target event types
                         Example: {"token": "sub_token", "end": None} (None = don't forward)
            
        Example:
            # Forward model stream events to turn stream
            model_stream_key = f"model_stream:{motet.task_id}:response"
            motet.forward_stream_events(model_stream_key, event_mapping={"end": None})
        """
        if self._command and hasattr(self._command, '_stream_enabled') and self._command._stream_enabled:
            if hasattr(self._command, '_forward_stream_events'):
                self._command._forward_stream_events(self.redis, source_stream_key, event_mapping)
    
    # === Event Publishing Helpers ===
    
    def publish_event(self, event: Dict[str, Any]) -> None:
        """
        Publish event to EventBus (convenience wrapper for motet.event_bus.publish).
        
        This is a convenience method that wraps motet.event_bus.publish() for consistency
        with other motet helper methods like stream_event() and ensure_stream().
        
        Args:
            event: Event dictionary with fields like 'kind', 'data', 'timestamp', etc.
            
        Example:
            # Publish a custom event
            motet.publish_event({
                "kind": "custom_event",
                "task_id": motet.task_id,
                "data": {"status": "processing"},
                "timestamp": datetime.utcnow().isoformat()
            })
            
            # Equivalent to:
            # motet.event_bus.publish({...})
        """
        if self.event_bus:
            self.event_bus.publish(event)
    
    # === Response Helpers (ADR-0029) ===

    def add_warning(self, message: str) -> None:
        """Attach a non-fatal warning; the decorator copies these onto the envelope.

        Command bodies should ``return data`` (or raise) and call this for
        warnings instead of ``return motet.create_response(..., warnings=...)``.
        """
        text = str(message).strip()
        if text:
            self._warnings.append(text)

    @property
    def last_metadata(self) -> Optional[Any]:
        """``CommandMetadata`` from the most recent ``do`` / ``join`` / ``apply`` / ``maybe``."""
        return self._last_metadata

    def _transport_payload(self, execution_result: Any) -> Any:
        from motet.core.commands.response_models import strip_transport_envelope

        return strip_transport_envelope(execution_result)

    def _parse_envelope(self, payload: Any) -> Any:
        from motet.core.commands.response_models import (
            CommandExecutionError,
            parse_command_envelope,
        )
        from pydantic import ValidationError

        try:
            envelope = parse_command_envelope(payload)
        except (ValidationError, TypeError, ValueError) as exc:
            raise CommandExecutionError(
                error_type="EnvelopeValidationError",
                message=(
                    "Command result was not a BaseCommandResponse. "
                    f"{exc}"
                ),
                details={"payload_type": type(payload).__name__},
                recoverable=False,
                command_type="unknown",
                command_id="",
            ) from exc
        self._last_metadata = envelope.metadata
        return envelope

    # === Sequential Execution (transport; authors use do / join / apply) ===
    
    def _extract_from_instance(
        self,
        instance: 'DistributedCommand',
        data_override: Optional[Any] = None,
        **distributed_kwargs
    ) -> Tuple[Type['DistributedCommand'], Any, Dict[str, Any]]:
        """
        Extract command class, data, and context from a pre-configured instance.
        
        Priority order for context:
            distributed_kwargs > motet context > instance context
        Priority order for data:
            data_override > instance.data
            
        Returns:
            (command_class, command_data, merged_context_kwargs)
        """
        command_class = instance.__class__
        command_data = data_override if data_override is not None else instance.data
        
        # Build merged context with proper priority
        merged_context = {}
        
        # 1. Start with instance context (lowest priority)
        if hasattr(instance, 'distributed_context') and instance.distributed_context:
            ctx = instance.distributed_context
            merged_context.update({
                'task_id': ctx.task_id,
                'conversation_id': ctx.conversation_id,
                'tenant_id': ctx.tenant_id,
                'principal_id': ctx.principal_id,
                'motet_id': ctx.motet_id,
                'parent_command_id': ctx.parent_command_id,
                'cancel_scopes': list(getattr(ctx, 'cancel_scopes', None) or []),
                'metadata': ctx.metadata.copy() if ctx.metadata else {},
            })
            # DEBUG: Log what we extracted
            cmd_principal = self._command.distributed_context.principal_id if self._command else self._principal_id_fallback
            logger.debug(
                "extract_from_instance_principal",
                instance_principal_id=ctx.principal_id,
                motet_principal_id=cmd_principal,
            )
            # Include routing hints if present
            if hasattr(ctx, 'target_worker_id') and ctx.target_worker_id:
                merged_context['target_worker_id'] = ctx.target_worker_id
        
        # 2. Override with motet context (medium priority)
        # Only override if INSTANCE values are empty to preserve instance values (ADR-0046)
        motet_context = {
            'task_id': self.task_id,
            'parent_command_id': self.command_id,  # Always update parent
        }
        # Only override conversation_id, tenant_id, principal_id, motet_id if instance has empty values
        if not merged_context.get('conversation_id'):
            motet_context['conversation_id'] = self.conversation_id
        if not merged_context.get('tenant_id'):
            motet_context['tenant_id'] = self.tenant_id
        if not merged_context.get('principal_id'):
            motet_context['principal_id'] = self.principal_id
        if not merged_context.get('motet_id'):
            motet_context['motet_id'] = self.motet_id
        merged_context.update(motet_context)
        
        # 3. Override with explicit kwargs (highest priority)
        merged_context.update(distributed_kwargs)
        
        return command_class, command_data, merged_context

    def _sanitize_distributed_kwargs(self, distributed_kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate identity overrides early for clear errors and argument hygiene.

        DistributedCommand enforces immutable identity as the canonical guardrail.
        This pre-check keeps error messages actionable and avoids duplicate keyword
        argument collisions in gather/dispatch tuple paths.
        """
        sanitized = dict(distributed_kwargs or {})
        immutable_fields = ("tenant_id", "motet_id", "principal_id")
        for field in immutable_fields:
            if field not in sanitized:
                continue
            requested_raw = sanitized.pop(field)
            current_raw = getattr(self, field, None)
            requested = str(requested_raw).strip() if requested_raw is not None else ""
            current = str(current_raw).strip() if current_raw is not None else ""
            if current and requested and requested != current:
                raise ValueError(
                    f"{field} override is not allowed in nested command composition "
                    f"(current={current!r}, requested={requested!r})"
                )
        return sanitized

    def _call(
        self,
        command_func_or_class_or_instance: Union[Callable, Type['DistributedCommand'], 'DistributedCommand'],
        data: Optional[Any] = None,
        **distributed_kwargs
    ) -> Any:
        """
        Transport: invoke a command and strip the invoker envelope (ADR-0133).

        Authors use ``motet.do()``. This method is the Redis/invoker primitive
        that ``do`` / ``maybe`` wrap with typed ``BaseCommandResponse`` unwrap.
        """
        from motet.core.workers.invoker_context import get_distributed_invoker
        distributed_kwargs = self._sanitize_distributed_kwargs(distributed_kwargs)
        
        # Check if it's a command instance
        if isinstance(command_func_or_class_or_instance, DistributedCommand):
            # ✅ Instance path - extract class, data, and merged context
            command_class, command_data, context_kwargs = self._extract_from_instance(
                command_func_or_class_or_instance,
                data_override=data,
                **distributed_kwargs
            )
        else:
            # Check if it's a decorated function (has __command_type__ attribute)
            command_type = getattr(command_func_or_class_or_instance, '__command_type__', None)
            
            if command_type:
                # ✅ Decorated command path - use new CommandTypeRegistry
                DistributedCommand._ensure_commands_registered()
                from motet.core.commands.command_type_registry import command_type_registry
                registration = command_type_registry.get(command_type)
                if not registration:
                    available_types = command_type_registry.get_command_types()
                    raise ValueError(f"Command type '{command_type}' not registered. Available: {', '.join(available_types)}")
                command_class = registration.implementation
            
            elif isinstance(command_func_or_class_or_instance, type) and issubclass(command_func_or_class_or_instance, DistributedCommand):
                # ✅ Class-based command path
                command_class = command_func_or_class_or_instance
            
            else:
                # ❌ Neither decorated function, class, nor instance
                raise ValueError(
                    f"Expected decorated command function, DistributedCommand class, or instance; "
                    f"got {type(command_func_or_class_or_instance).__name__}. "
                    f"Use @motet.command decorator or pass a DistributedCommand class/instance."
                )
            
            # For function/class paths, data is required
            if data is None:
                raise ValueError(f"data parameter is required when using decorated functions or command classes")
            
            # Get data class and create instance (decorated commands have _get_data_class on the class)
            data_class = getattr(command_class, "_get_data_class", lambda: None)()
            if data_class is None:
                raise ValueError(f"Command class {getattr(command_class, '__name__', command_class)} has no _get_data_class")
            if isinstance(data, dict):
                command_data = data_class(**data)
            elif isinstance(data, data_class):
                command_data = data
            elif type(data).__name__ == data_class.__name__:
                # Handle case where same class is imported from different paths
                # Just use it as-is since it has the right structure
                command_data = data
            else:
                raise ValueError(f"Expected {data_class.__name__} or dict, got {type(data).__name__}")
            
            # Build context for function/class paths (include motet_id for memory/encryption scope)
            context_kwargs = self._with_composition_timeout({
                'task_id': self.task_id,
                'conversation_id': self.conversation_id,
                'tenant_id': self.tenant_id,
                'principal_id': self.principal_id,
                'motet_id': self.motet_id,
                'parent_command_id': self.command_id,
                **self._composition_cancel_kwargs(),
                **distributed_kwargs
            })
            # Propagate metadata so child commands inherit model_provider/model_name (e.g. web_search LLM path)
            if self.metadata and 'metadata' not in distributed_kwargs:
                context_kwargs['metadata'] = self.metadata
        
        # Create command with proper context inheritance
        cmd = command_class(
            data=command_data,
            **context_kwargs
        )
        
        # Execute via distributed invoker (blocks)
        # Result is already fully rehydrated by the invoker
        invoker = get_distributed_invoker()
        execution_result = invoker.execute_command(cmd)
        return self._transport_payload(execution_result)
    
    # === Parallel Execution ===
    
    def _gather(
        self,
        commands: List[Union[DistributedCommand, Tuple[Union[Callable, Type['DistributedCommand']], Any]]],
        aggregation_strategy: str = "all_results",
        fail_fast: bool = False,
        **distributed_kwargs
    ) -> Dict[str, Any]:
        """
        Execute multiple commands in parallel and gather results (blocking).
        
        Supports pre-created command instances and (function/class, data) tuples
        for convenient command creation with automatic context propagation.
        
        Args:
            commands: List of either:
                      - DistributedCommand instances (pre-created)
                      - (function, data) tuples for decorated commands
                      - (DistributedCommand class, data) tuples for class-based commands
            aggregation_strategy: How to aggregate results
            fail_fast: Stop on first failure
            **distributed_kwargs: Additional parameters for tuple syntax
            
        Returns:
            Aggregated results from all commands
            
        Example 1: Tuple syntax with decorated functions
            results = motet._gather([
                (fetch_weather, {"city": "NYC"}),
                (fetch_stock, {"symbol": "AAPL"}),
                (fetch_news, {"topic": "tech"})
            ])
            
        Example 2: Tuple syntax with decorator commands
            from motet.core.commands.builtin.tool import tool_execution
            from motet.core.commands.builtin.model import model_inference
            from motet.core.commands.command_data_classes import ToolExecutionData, ModelInferenceData
            results = motet._gather([
                (tool_execution, ToolExecutionData(tool_name="search", ...)),
                (model_inference, ModelInferenceData(...))
            ])
            
        Example 3: Mixed usage
            results = motet._gather([
                my_decorated_cmd_instance,  # Pre-created
                (fetch_data, {"source": "api"}),  # Decorated function tuple
                (tool_execution, ToolExecutionData(...))  # Decorator-based tuple
            ])
        """
        from motet.core.commands.concurrency import GatherCommand
        from motet.core.workers.invoker_context import get_distributed_invoker
        distributed_kwargs = self._sanitize_distributed_kwargs(distributed_kwargs)
        
        # Convert tuples to command instances
        command_instances = []
        for item in commands:
            if isinstance(item, DistributedCommand):
                # ✅ Pass through pre-created DistributedCommand instances
                # Re-parent them to this command for proper hierarchy
                command_class, command_data, context_kwargs = self._extract_from_instance(
                    item,
                    **distributed_kwargs
                )
                cmd = command_class(
                    data=command_data,
                    **context_kwargs
                )
                command_instances.append(cmd)
            elif isinstance(item, tuple):
                func_or_class, data = item
                
                # Check if it's a decorated function (has __command_type__ attribute)
                command_type = getattr(func_or_class, '__command_type__', None)
                
                if command_type:
                    # ✅ Decorated command path - use new CommandTypeRegistry
                    DistributedCommand._ensure_commands_registered()
                    from motet.core.commands.command_type_registry import command_type_registry
                    registration = command_type_registry.get(command_type)
                    if not registration:
                        available_types = command_type_registry.get_command_types()
                        raise ValueError(f"Command type '{command_type}' not registered. Available: {', '.join(available_types)}")
                    command_class = registration.implementation
                
                elif isinstance(func_or_class, type) and issubclass(func_or_class, DistributedCommand):
                    # ✅ Class-based command path
                    command_class = func_or_class
                
                else:
                    # ❌ Neither - raise clear error
                    raise ValueError(
                        f"Expected decorated command function or DistributedCommand class in tuple, "
                        f"got {type(func_or_class).__name__}"
                    )
                
                # Create command instance with proper context (decorated commands have _get_data_class on the class)
                data_class = getattr(command_class, "_get_data_class", lambda: None)()
                if not data_class:
                    raise ValueError(f"Command class has no _get_data_class")
                if isinstance(data, dict):
                    command_data = data_class(**data)
                elif isinstance(data, data_class):
                    command_data = data
                else:
                    raise ValueError(f"Expected {data_class.__name__} or dict, got {type(data).__name__}")
                
                # Propagate metadata so children inherit model/chat context
                # (e.g., model_provider/model_name/model_profile_name).
                child_kwargs = self._with_composition_timeout(dict(distributed_kwargs))
                if self.metadata and 'metadata' not in child_kwargs:
                    child_kwargs['metadata'] = self.metadata

                cmd = command_class(
                    task_id=self.task_id,
                    data=command_data,
                    conversation_id=self.conversation_id,
                    tenant_id=self.tenant_id,
                    principal_id=self.principal_id,
                    motet_id=self.motet_id,
                    parent_command_id=self.command_id,
                    **self._composition_cancel_kwargs(),
                    **child_kwargs
                )
                command_instances.append(cmd)
            else:
                raise ValueError(
                    f"Expected DistributedCommand instance or (function/class, data) tuple, "
                    f"got {type(item).__name__}"
                )
        
        # Create GatherCommand
        gather_cmd = GatherCommand.create(
            commands=command_instances,
            aggregation_strategy=aggregation_strategy,
            fail_fast=fail_fast,
            task_id=self.task_id,
            conversation_id=self.conversation_id,
            tenant_id=self.tenant_id,
            principal_id=self.principal_id,
            parent_command_id=self.command_id,
            metadata=self.metadata or {},
            **self._with_composition_timeout(self._composition_cancel_kwargs()),
        )
        
        # Execute via distributed invoker (blocks until all complete)
        invoker = get_distributed_invoker()
        execution_result = invoker.execute_command(gather_cmd)
        return self._transport_payload(execution_result)
    
    def _group(self, commands: List[DistributedCommand], **kwargs) -> Dict[str, Any]:
        """Transport alias for ``_gather`` (Celery ``group`` naming)."""
        return self._gather(commands, **kwargs)  # type: ignore[arg-type]
    
    def dispatch(
        self,
        commands: List[Union[DistributedCommand, Tuple[Union[Callable, Type['DistributedCommand']], Any]]],
        max_parallel: Optional[int] = None,
        **distributed_kwargs
    ) -> List[str]:
        """
        Dispatch multiple commands without waiting (fire-and-forget).
        
        Uses DispatchCommand under the hood. Returns immediately with command IDs.
        
        Args:
            commands: List of either:
                      - DistributedCommand instances (pre-created)
                      - (function, data) tuples for decorated commands
                      - (DistributedCommand class, data) tuples for class-based commands
            max_parallel: Limit concurrent execution
            **distributed_kwargs: Additional parameters for tuple syntax
            
        Returns:
            List of dispatched command IDs
            
        Example 1: Tuple syntax with decorated functions
            task_ids = motet.dispatch([
                (send_email, {"to": "user@example.com"}),
                (update_cache, {"key": "results"})
            ])
            
        Example 2: Tuple syntax with class-based commands
            from motet.core.commands.builtin.tool import ToolExecutionCommand
            task_ids = motet.dispatch([
                (ToolExecutionCommand, ToolExecutionData(tool_name="search", ...)),
                (ModelInferenceCommand, ModelInferenceData(...))
            ])
            
        Example 3: Mixed usage
            task_ids = motet.dispatch([
                my_cmd_instance,  # Pre-created
                (send_notification, {"msg": "..."}),  # Decorated function
                (ToolExecutionCommand, ToolExecutionData(...))  # Class-based
            ])
        """
        from motet.core.commands.concurrency import DispatchCommand
        from motet.core.workers.invoker_context import get_distributed_invoker
        distributed_kwargs = self._sanitize_distributed_kwargs(distributed_kwargs)
        
        # Convert tuples to commands (same dual-path logic as gather)
        command_instances = []
        for item in commands:
            if isinstance(item, DistributedCommand):
                # ✅ Pass through pre-created DistributedCommand instances
                # Re-parent them to this command for proper hierarchy
                command_class, command_data, context_kwargs = self._extract_from_instance(
                    item,
                    **distributed_kwargs
                )
                cmd = command_class(
                    data=command_data,
                    **context_kwargs
                )
                command_instances.append(cmd)
            elif isinstance(item, tuple):
                func_or_class, data = item
                
                # Check if it's a decorated function (has __command_type__ attribute)
                command_type = getattr(func_or_class, '__command_type__', None)
                
                if command_type:
                    # ✅ Decorated command path - use new CommandTypeRegistry
                    DistributedCommand._ensure_commands_registered()
                    from motet.core.commands.command_type_registry import command_type_registry
                    registration = command_type_registry.get(command_type)
                    if not registration:
                        available_types = command_type_registry.get_command_types()
                        raise ValueError(f"Command type '{command_type}' not registered. Available: {', '.join(available_types)}")
                    command_class = registration.implementation
                
                elif isinstance(func_or_class, type) and issubclass(func_or_class, DistributedCommand):
                    # ✅ Class-based command path
                    command_class = func_or_class
                
                else:
                    # ❌ Neither - raise clear error
                    raise ValueError(
                        f"Expected decorated command function or DistributedCommand class in tuple, "
                        f"got {type(func_or_class).__name__}"
                    )
                
                # Create command instance with proper context (decorated commands have _get_data_class on the class)
                data_class = getattr(command_class, "_get_data_class", lambda: None)()
                if not data_class:
                    raise ValueError(f"Command class has no _get_data_class")
                if isinstance(data, dict):
                    command_data = data_class(**data)
                elif isinstance(data, data_class):
                    command_data = data
                else:
                    raise ValueError(f"Expected {data_class.__name__} or dict, got {type(data).__name__}")
                
                # Propagate metadata so children inherit model/chat context
                # (e.g., model_provider/model_name/model_profile_name).
                child_kwargs = self._with_composition_timeout(dict(distributed_kwargs))
                if self.metadata and 'metadata' not in child_kwargs:
                    child_kwargs['metadata'] = self.metadata

                cmd = command_class(
                    task_id=self.task_id,
                    data=command_data,
                    conversation_id=self.conversation_id,
                    tenant_id=self.tenant_id,
                    principal_id=self.principal_id,
                    motet_id=self.motet_id,
                    parent_command_id=self.command_id,
                    **self._composition_cancel_kwargs(),
                    **child_kwargs
                )
                command_instances.append(cmd)
            else:
                raise ValueError(
                    f"Expected DistributedCommand instance or (function/class, data) tuple, "
                    f"got {type(item).__name__}"
                )
        
        # Create DispatchCommand
        dispatch_cmd = DispatchCommand.create(
            commands=command_instances,
            max_parallel=max_parallel,
            task_id=self.task_id,
            conversation_id=self.conversation_id,
            tenant_id=self.tenant_id,
            principal_id=self.principal_id,
            parent_command_id=self.command_id,
            metadata=self.metadata or {},
            **self._with_composition_timeout(self._composition_cancel_kwargs()),
        )
        
        # Execute via distributed invoker (returns immediately)
        invoker = get_distributed_invoker()
        result = invoker.execute_command(dispatch_cmd)
        payload = self._transport_payload(result)
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"].get("dispatched", [])
        return []
    
    def _map(
        self,
        command_func_or_class_or_instance: Union[Callable, Type['DistributedCommand'], 'DistributedCommand'],
        inputs: List[Dict[str, Any]],
        command_template: Optional[Dict[str, Any]] = None,
        batch_size: Optional[int] = None,
        **distributed_kwargs
    ) -> Dict[str, Any]:
        """
        Apply the same command to multiple inputs (batch processing).
        
        Uses MapCommand under the hood for efficient bulk operations.
        Supports decorated functions, DistributedCommand classes, and instances as templates.
        
        Args:
            command_func_or_class_or_instance: Either:
                - Decorated command function to apply
                - DistributedCommand class to instantiate
                - DistributedCommand instance (as template for routing/config)
            inputs: List of input variations
            command_template: Base parameters shared across all instances
            batch_size: Limit concurrent execution
            **distributed_kwargs: Additional parameters
            
        Returns:
            Batch results with statistics (success rate, failures, etc.)
            
        Example 1: Decorated command
            results = motet._map(
                extract_text,
                inputs=[{"file": f"doc{i}.pdf"} for i in range(100)],
                command_template={"format": "markdown"},
                batch_size=10
            )
            
        Example 2: Class-based command
            results = motet._map(
                ToolExecutionCommand,
                inputs=[
                    {"tool_name": "extract", "file": "doc1.pdf"},
                    {"tool_name": "extract", "file": "doc2.pdf"},
                ],
                batch_size=10
            )
            
        Example 3: Instance as template (NEW)
            # Pre-configure routing, priority, timeout for entire batch
            template_cmd = ToolExecutionCommand(
                data=None,  # Will be replaced per-input
                target_worker_id="worker_123",
                priority=10
            )
            results = motet._map(
                template_cmd,
                inputs=[{"tool_name": "extract", "file": f"doc{i}.pdf"} for i in range(100)],
                batch_size=10
            )
        """
        from motet.core.commands.concurrency import MapCommand
        from motet.core.workers.invoker_context import get_distributed_invoker
        distributed_kwargs = self._sanitize_distributed_kwargs(distributed_kwargs)
        
        # Check if it's an instance (use as template)
        if isinstance(command_func_or_class_or_instance, DistributedCommand):
            # ✅ Instance path - extract command type and template config
            # Find the command type from the instance's class
            instance_class = command_func_or_class_or_instance.__class__
            
            DistributedCommand._ensure_commands_registered()
            from motet.core.commands.command_type_registry import command_type_registry
            
            # Find the command type by checking the registry
            all_registrations = command_type_registry.get_all_registrations()
            command_type = None
            for cmd_type, registration in all_registrations.items():
                if registration.implementation == instance_class:
                    command_type = cmd_type
                    break
            
            if not command_type:
                raise ValueError(
                    f"Command class {instance_class.__name__} not found in registry. "
                    f"Make sure the command is properly registered."
                )
            
            # Extract template config from instance (routing hints, priorities, etc.)
            # Note: data is ignored - it will be replaced per-input
            if command_template is None:
                command_template = {}
            
            # Merge instance's context into template
            if hasattr(command_func_or_class_or_instance, 'distributed_context') and command_func_or_class_or_instance.distributed_context:
                ctx = command_func_or_class_or_instance.distributed_context
                if hasattr(ctx, 'target_worker_id') and ctx.target_worker_id:
                    command_template.setdefault('target_worker_id', ctx.target_worker_id)
                if hasattr(ctx, 'metadata') and ctx.metadata:
                    command_template.setdefault('metadata', ctx.metadata)
        else:
            # Check if it's a decorated function (has __command_type__ attribute)
            command_type = getattr(command_func_or_class_or_instance, '__command_type__', None)
            
            if command_type:
                # ✅ Decorated command path
                pass  # command_type already extracted
            
            elif isinstance(command_func_or_class_or_instance, type) and issubclass(command_func_or_class_or_instance, DistributedCommand):
                # ✅ Class-based command path - use new CommandTypeRegistry
                DistributedCommand._ensure_commands_registered()
                from motet.core.commands.command_type_registry import command_type_registry
                
                # Find the command type by checking the registry
                all_registrations = command_type_registry.get_all_registrations()
                command_type = None
                for cmd_type, registration in all_registrations.items():
                    if registration.implementation == command_func_or_class_or_instance:
                        command_type = cmd_type
                        break
                
                if not command_type:
                    raise ValueError(
                        f"Command class {command_func_or_class_or_instance.__name__} not found in registry. "
                        f"Make sure the command is properly registered."
                    )
            
            else:
                # ❌ Neither - raise clear error
                raise ValueError(
                    f"Expected decorated command function, DistributedCommand class, or instance; "
                    f"got {type(command_func_or_class_or_instance).__name__}. "
                    f"Use @motet.command decorator or pass a DistributedCommand class/instance."
                )
        
        # Create MapCommand
        map_kwargs = self._with_composition_timeout(dict(distributed_kwargs))
        if self.metadata and "metadata" not in map_kwargs:
            map_kwargs["metadata"] = self.metadata

        map_cmd = MapCommand.create(
            command_type=command_type,
            inputs=inputs,
            command_template=command_template or {},
            batch_size=batch_size,
            task_id=self.task_id,
            conversation_id=self.conversation_id,
            tenant_id=self.tenant_id,
            principal_id=self.principal_id,
            parent_command_id=self.command_id,
            **self._composition_cancel_kwargs(),
            **map_kwargs
        )
        
        # Execute via distributed invoker (blocks until all complete)
        invoker = get_distributed_invoker()
        execution_result = invoker.execute_command(map_cmd)
        return self._transport_payload(execution_result)
    
    # === Concise Command Composition Helpers (ADR-0052) ===
    
    def do(
        self,
        command_func_or_class_or_instance: Union[Callable, Type['DistributedCommand'], 'DistributedCommand'],
        data: Optional[Any] = None,
        **distributed_kwargs
    ) -> Any:
        """
        Execute command and unwrap data or raise (ADR-0133).

        Returns domain data on success. Raises CommandExecutionError on failure.

        Example:
            data = motet.do(fetch_data, FetchData(source="api"))
        """
        from motet.core.commands.response_models import CommandExecutionError

        response = self._call(command_func_or_class_or_instance, data=data, **distributed_kwargs)
        envelope = self._parse_envelope(response)

        if envelope.status == "success":
            return envelope.data

        error_info = _error_dict_from_envelope(envelope.model_dump(mode="json"))
        metadata = envelope.metadata
        raise CommandExecutionError(
            error_type=error_info["type"],
            message=error_info["message"],
            details=error_info["details"],
            recoverable=error_info["recoverable"],
            command_type=metadata.command_type if metadata else "unknown",
            command_id=metadata.command_id if metadata else "",
        )
    
    def join(
        self,
        commands: List[Union[DistributedCommand, Tuple[Union[Callable, Type['DistributedCommand']], Any]]],
        fail_fast: bool = False,
        **distributed_kwargs
    ) -> List[Any]:
        """
        Execute commands in parallel and unwrap all results or raise (ADR-0133).

        Returns domain data in submission order. Raises GatherExecutionError
        on failure, with ``partial_results`` already unwrapped to the same
        shape as a successful join (domain data or ``{_error: True, ...}``).

        Example:
            weather, stock = motet.join([
                (fetch_weather, {"city": "NYC"}),
                (fetch_stock, {"symbol": "AAPL"})
            ])
        """
        from motet.core.commands.response_models import GatherExecutionError

        response = self._gather(commands, fail_fast=fail_fast, **distributed_kwargs)
        envelope = self._parse_envelope(response)
        results_data = envelope.data or {}
        results = results_data.get("results", []) if isinstance(results_data, dict) else []

        unwrapped = [unwrap_child_envelope(result) for result in results]
        if envelope.status == "success":
            return unwrapped

        error_info = _error_dict_from_envelope(envelope.model_dump(mode="json"))
        metadata = envelope.metadata
        raise GatherExecutionError(
            error_type=error_info["type"],
            message=error_info["message"],
            details=error_info["details"],
            recoverable=error_info["recoverable"],
            command_type=metadata.command_type if metadata else "core.gather",
            command_id=metadata.command_id if metadata else "",
            partial_results=unwrapped,
        )
    
    def apply(
        self,
        command_func_or_class_or_instance: Union[Callable, Type['DistributedCommand'], 'DistributedCommand'],
        inputs: List[Dict[str, Any]],
        command_template: Optional[Dict[str, Any]] = None,
        batch_size: Optional[int] = None,
        **distributed_kwargs
    ) -> List[Any]:
        """
        Apply a command to many inputs and unwrap results (ADR-0133).

        Returns domain data from successful items. Raises ApplyExecutionError
        if every item fails.

        Example:
            data_list = motet.apply(
                extract_text, inputs=[{"file": f"doc{i}.pdf"} for i in range(10)]
            )
        """
        from motet.core.commands.response_models import ApplyExecutionError

        response = self._map(
            command_func_or_class_or_instance,
            inputs=inputs,
            command_template=command_template,
            batch_size=batch_size,
            **distributed_kwargs
        )
        envelope = self._parse_envelope(response)
        status = envelope.status
        results_data = envelope.data or {}
        results = results_data.get("results", []) if isinstance(results_data, dict) else []
        metadata = envelope.metadata
        error_info = _error_dict_from_envelope(envelope.model_dump(mode="json"))
        command_type = metadata.command_type if metadata else "core.map"
        command_id = metadata.command_id if metadata else ""

        if status in ("success", "partial_success"):
            unwrapped = []
            successful = 0
            failed = 0
            for result in results:
                child = unwrap_child_envelope(result)
                unwrapped.append(child)
                if isinstance(child, dict) and child.get("_error"):
                    failed += 1
                else:
                    successful += 1

            if successful == 0 and failed > 0:
                raise ApplyExecutionError(
                    error_type=error_info.get("type") or "BatchExecutionError",
                    message=error_info.get("message") or "All commands in batch failed",
                    details=error_info.get("details") or {},
                    recoverable=error_info.get("recoverable", False),
                    command_type=command_type,
                    command_id=command_id,
                    total_inputs=len(inputs),
                    successful=successful,
                    failed=failed,
                )
            return unwrapped

        successful = sum(
            1
            for r in results
            if not (
                isinstance((child := unwrap_child_envelope(r)), dict) and child.get("_error")
            )
        )
        failed = len(results) - successful
        raise ApplyExecutionError(
            error_type=error_info.get("type") or "BatchExecutionError",
            message=error_info.get("message") or "All commands in batch failed",
            details=error_info.get("details") or {},
            recoverable=error_info.get("recoverable", False),
            command_type=command_type,
            command_id=command_id,
            total_inputs=len(inputs),
            successful=successful,
            failed=failed,
        )
    
    def maybe(
        self,
        command_func_or_class_or_instance: Union[Callable, Type['DistributedCommand'], 'DistributedCommand'],
        data: Optional[Any] = None,
        **distributed_kwargs
    ) -> tuple[Any, Optional[Dict[str, Any]]]:
        """
        Execute command and return (data, error) tuple (ADR-0052).
        
        Convenience wrapper for optional error handling that returns errors as values
        instead of raising exceptions. Useful for graceful degradation patterns.
        
        Args:
            command_func_or_class_or_instance: Command function, class, or instance
            data: Command data (optional for instances)
            **distributed_kwargs: Additional parameters
            
        Returns:
            Tuple of (data, error) where:
            - data: Unwrapped data on success, None on failure
            - error: None on success, error dict on failure
            
        Example:
            # Before (verbose):
            result = motet._call(fetch_data, FetchData(source="api"))
            if result.get("status") != "success":
                error = result.get("error", {})
                return None, error
            return result.get("data", {}), None
            
            # After (concise):
            data, error = motet.maybe(fetch_data, FetchData(source="api"))
            if error:
                # Handle error gracefully
                return fallback_value
            return data
        """
        from motet.core.commands.response_models import CommandExecutionError

        response = self._call(command_func_or_class_or_instance, data=data, **distributed_kwargs)
        try:
            envelope = self._parse_envelope(response)
        except CommandExecutionError as exc:
            return None, {
                "type": exc.error_type,
                "message": exc.message,
                "details": exc.details,
                "recoverable": exc.recoverable,
                "retry_recommended": False,
            }

        if envelope.status == "success":
            return envelope.data, None

        return None, _error_dict_from_envelope(envelope.model_dump(mode="json"))

