"""
Motet - Decorator-Based Commands

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Decorator-based command system for the Motet distributed framework.
    Prefer ``@motet.command`` at call sites; ``distributed_command`` remains the
    implementation and backward-compatible alias.
    Provides unified distributed commands using function decorators for simplified
    command creation. Implements automatic command lifecycle events
    (command_start, command_complete, command_error) and unified task-level streaming.
    All commands in a task write to the same unified task stream
    (``{tenant}:task:{task_id}:response`` when tenant is usable).

    Registration stores a first-class ``description`` on ``CommandRegistration``
    (from an explicit decorator ``description=`` or the function docstring) for
    function discovery / ``core.help`` (#194).

Dependencies:
    - functools: Function decorators and wrapping
    - inspect: Function introspection and metadata
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - Distributed command system
    - Event observation and management

Usage:
    from motet import motet
    # Prefer @motet.command; @distributed_command remains a compatible alias.
    
    # Basic decorator-based command
    @motet.command(
        timeout_seconds=30,
        required_capabilities=[WorkerCapability.MODEL_INFERENCE]
    )
    def my_command(data: MyCommandData, motet: MotetContext) -> Dict[str, Any]:
        # Command implementation
        return {"result": "success"}
    
    # Command with dynamic capability inference
    def _infer_capabilities(data: ToolData) -> List[WorkerCapability]:
        capabilities = [WorkerCapability.TOOL_EXECUTION]
        if data.tool_name.startswith('http_'):
            capabilities.append(WorkerCapability.HTTP_OPERATIONS)
        return capabilities
    
    @motet.command(
        timeout_seconds=30,
        capability_inference=_infer_capabilities  # Affects worker routing
    )
    def tool_execution(data: ToolData, motet: MotetContext) -> Dict[str, Any]:
        return motet.tools.execute(data.tool_name, data.params)

Notes:
    - Supports decorator-based command creation with minimal boilerplate
    - Includes automatic event observation and lifecycle management
    - Provides capability-based worker routing and filtering
    - Supports dynamic capability inference from command data (affects routing)
    - Supports comprehensive distributed execution and coordination
    - MotetContext / helpers live in motet_context.py (issue #158); re-exported here
    - Includes temporary observer management for event capture
    - Integrates with distributed command system and worker routing
    - Supports both sync and async command implementations
"""


from __future__ import annotations

import inspect
import json
import structlog
from contextlib import contextmanager
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

# Lazy imports for tool execution delegation (ADR-0084) to avoid circular import
# tool_execution and ToolExecutionData imported inside MotetToolsHelper.execute
from motet.core.workers.concurrency_primitives import WorkerLocal, WorkerLock
from motet.core.types import PoolType

logger = structlog.get_logger(__name__)


# Bundle command/tool namespaces (ADR-0089) — stay with decorator registration
_bundle_command_namespace = WorkerLocal()

@contextmanager
def bundle_command_namespace(namespace: Optional[str]) -> Iterator[None]:
    """
    Temporarily set the active bundle namespace for decorator-time command typing.

    This is used by bundle loaders while importing command modules so
    @motet.command can generate final namespaced command types
    (e.g. "calculator.calculate") at decoration time.
    """
    previous = getattr(_bundle_command_namespace, "current", None)
    if namespace:
        _bundle_command_namespace.current = namespace
    elif hasattr(_bundle_command_namespace, "current"):
        del _bundle_command_namespace.current
    try:
        yield
    finally:
        if previous:
            _bundle_command_namespace.current = previous
        elif hasattr(_bundle_command_namespace, "current"):
            del _bundle_command_namespace.current


def _get_bundle_command_namespace() -> Optional[str]:
    """Internal: Return active bundle namespace for command type generation."""
    return getattr(_bundle_command_namespace, "current", None)


# Worker-local storage for bundle_id when loading bundle tools/*.py (ADR-0089)
_bundle_tool_namespace = WorkerLocal()


@contextmanager
def bundle_tool_namespace(bundle_id: Optional[str]) -> Iterator[None]:
    """
    Temporarily set the active bundle ID for @motet.tool registration (ADR-0089).

    Used by bundle loaders while importing tool modules so @motet.tool
    can register tools under {bundle_id}.{tool_name}.
    """
    previous = getattr(_bundle_tool_namespace, "current", None)
    if bundle_id:
        _bundle_tool_namespace.current = bundle_id
    elif hasattr(_bundle_tool_namespace, "current"):
        del _bundle_tool_namespace.current
    try:
        yield
    finally:
        if previous is not None:
            _bundle_tool_namespace.current = previous
        elif hasattr(_bundle_tool_namespace, "current"):
            del _bundle_tool_namespace.current


def _get_bundle_tool_namespace() -> Optional[str]:
    """Internal: Return active bundle ID for tool registration (ADR-0089)."""
    return getattr(_bundle_tool_namespace, "current", None)

class DecoratedCommandConfig(BaseModel):
    """Configuration for decorated command behavior."""
    timeout_seconds: Optional[int] = None
    priority: Optional[int] = None
    required_capabilities: Optional[List[WorkerCapability]] = None
    capability_inference: Optional[Callable] = None  # Function to infer capabilities from data
    streaming_enabled: bool = False
    stream_key: Optional[str] = None  # Stream key pattern: None=unified task stream, "auto"=command-specific, or custom pattern
    can_undo: bool = False
    preferred_pool_type: Optional[PoolType] = None  # ADR-0033: PoolType.HIGH_CONCURRENCY or PoolType.PROCESS
    
    model_config = ConfigDict(arbitrary_types_allowed=True)  # Allow Callable type

# Re-export MotetContext surface for backward-compatible imports (issue #158)
from motet.core.commands.motet_context import (  # noqa: E402
    MotetContext,
    MotetToolsHelper,
    MotetMemoryHelper,
    MotetAgentsHelper,
    MotetModelsHelper,
    MotetWorkflowsHelper,
    MotetSchedulesHelper,
    MotetCommandsHelper,
    MotetConversationsHelper,
    TemporaryObserver,
    get_motet_context,
    _set_motet_context,
    _clear_motet_context,
)

from motet.core.commands.motet_context import resolve_current_identity  # noqa: E402, F401

def distributed_command(
    timeout_seconds: Optional[int] = None,
    priority: Optional[int] = None,
    required_capabilities: Optional[List[WorkerCapability]] = None,
    capability_inference: Optional[Callable[[BaseModel], List[WorkerCapability]]] = None,
    streaming_enabled: bool = False,
    stream_key: Optional[str] = None,
    can_undo: bool = False,
    preferred_pool_type: Optional[PoolType] = None,
    description: Optional[str] = None,
    namespace: Optional[str] = None,
) -> Callable[[Callable[..., Any]], Callable[..., DistributedCommand]]:
    """
    Decorator for creating distributed commands from functions.
    
    Transforms a simple function into a full-fledged DistributedCommand with:
    - Automatic command registration
    - MotetContext injection
    - ADR-0029 response standardization
    - Concurrency helper methods
    - Dynamic capability inference (optional)
    - Smart context-aware defaults (auto-fills params from parent context)
    
    ADR-0030: Decorator-Based Command Pattern
    
    Args:
        timeout_seconds: Command timeout (default: 60)
        priority: Execution priority (default: NORMAL)
        required_capabilities: Worker capabilities needed (default capabilities)
        capability_inference: Function to infer capabilities from command data.
                            Called during __init__ with data parameter, returns List[WorkerCapability].
                            Overrides required_capabilities if provided.
        streaming_enabled: Enable streaming support
        stream_key: Stream key pattern. None=unified task stream (default, recommended),
                   "auto"=command-specific stream ({command_type}_stream:{task_id}:events),
                   or custom pattern string. Only used if streaming_enabled=True.
        can_undo: Whether command supports undo
        preferred_pool_type: Preferred worker pool type (ADR-0033): PoolType.HIGH_CONCURRENCY for I/O-heavy,
                           PoolType.PROCESS for CPU-heavy, or None for no preference (default)
        description: Discovery prose for help/search (#194). Defaults to the first
                   non-empty line of the function docstring (then data-class docstring).
        namespace: Explicit namespace prefix (e.g. "core" → "core.func_name"). When None (default),
                   auto-detected in this order: (1) functions in "motet.core.*" use "core",
                   (2) command modules loaded under bundle context use the active bundle_id,
                   (3) otherwise no namespace. Pass empty string "" to suppress auto-detection
                   and register with no prefix.
        
    Returns:
        Decorated function that creates a DistributedCommand
        
    Example:
        # Pattern 1 (preferred): Get MotetContext internally
        @motet.command(timeout_seconds=120)
        def process_document(data: DocData) -> Dict[str, Any]:
            motet = get_motet_context()
            # Extract text from document
            text = extract_text(data.file)
            return {"text": text, "pages": data.pages}
        
        # Pattern 2 (optional): Explicit motet parameter
        @motet.command(timeout_seconds=120)
        def process_document(data: DocData, motet: MotetContext) -> Dict[str, Any]:
            # Extract text from document
            text = extract_text(data.file)
            return {"text": text, "pages": data.pages}
        
        # Pattern 3: Dynamic capability inference
        def _infer_capabilities(data: ToolData) -> List[WorkerCapability]:
            capabilities = [WorkerCapability.TOOL_EXECUTION]
            if data.tool_name.startswith('http_'):
                capabilities.append(WorkerCapability.HTTP_OPERATIONS)
            return capabilities
        
        @motet.command(
            timeout_seconds=30,
            capability_inference=_infer_capabilities
        )
        def tool_execution(data: ToolData, motet: MotetContext) -> Dict[str, Any]:
            # Capabilities are automatically inferred from data during __init__
            return {"result": motet.tools.execute(data.tool_name, data.params)}
        
        # Pattern 4: Smart context-aware defaults (inside decorated commands)
        @motet.command()
        def parent_command(data: ParentData, motet: MotetContext) -> Dict[str, Any]:
            # Call child command - context auto-filled (task_id, conversation_id, etc.)
            child_result = motet.do(tool_execution, data=ToolData(...))
            
            # Or create command directly - still auto-fills context
            command = tool_execution(data=ToolData(...))  # No need to pass task_id, etc.
            result = motet.do(command)
            
            return {"result": result}
        
        # Pattern 5: Command-specific streams (for isolated debugging/testing)
        @motet.command(
            streaming_enabled=True,
            stream_key="auto"  # Uses {command_type}_stream:{task_id}:events
        )
        def isolated_command(data: IsolatedData) -> Dict[str, Any]:
            motet = get_motet_context()
            # Events go to command-specific stream, not unified task stream
            motet.stream_event("debug", info="isolated")
            return {"result": "success"}
        
        # Pattern 6: Custom stream pattern
        @motet.command(
            streaming_enabled=True,
            stream_key="custom:stream:{task_id}:events"  # Custom pattern with task_id placeholder
        )
        def custom_stream_command(data: CustomData) -> Dict[str, Any]:
            motet = get_motet_context()
            # Events go to custom stream
            motet.stream_event("event", data="value")
            return {"result": "success"}
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., DistributedCommand]:
        # Extract function signature for validation
        sig = inspect.signature(func)
        params = list(sig.parameters.keys())
        
        # Validate function signature (must have at least data parameter)
        if len(params) < 1:
            raise ValueError(f"Decorated function must have at least 1 parameter: data")
        
        data_param = params[0]
        
        # Get data type hint (resolve string annotations from __future__ annotations)
        data_type_hint = sig.parameters[data_param].annotation
        if data_type_hint == inspect.Parameter.empty:
            raise ValueError(f"Data parameter '{data_param}' must have a type hint")
        
        # Resolve string annotations (from __future__ import annotations)
        if isinstance(data_type_hint, str):
            # Resolve from function's module globals
            type_hints = get_type_hints(func, globalns=func.__globals__)
            data_type_hint = type_hints.get(data_param, data_type_hint)
        
        # Ensure data type is a Pydantic model
        if not (isinstance(data_type_hint, type) and issubclass(data_type_hint, BaseModel)):
            raise ValueError(f"Data parameter must be a Pydantic BaseModel, got {data_type_hint}")
        
        # Generate command type name from function name, with optional namespace prefix.
        # Auto-detection order:
        # 1) motet.core.* modules => "core" namespace (built-ins)
        # 2) active bundle loader context => "<bundle_id>" namespace
        # 3) otherwise no namespace
        # Pass namespace="" to suppress auto-detection and force bare names.
        resolved_namespace: Optional[str] = namespace
        if resolved_namespace is None:
            # Auto-detect: assign "core" prefix for all built-in motet.core commands
            if func.__module__.startswith("motet.core."):
                resolved_namespace = "core"
            else:
                resolved_namespace = _get_bundle_command_namespace()
        command_type = f"{resolved_namespace}.{func.__name__}" if resolved_namespace else func.__name__
        
        # Create configuration
        config = DecoratedCommandConfig(
            timeout_seconds=timeout_seconds,
            priority=priority or EventPriority.NORMAL,
            required_capabilities=required_capabilities,
            capability_inference=capability_inference,
            streaming_enabled=streaming_enabled,
            stream_key=stream_key,
            can_undo=can_undo,
            preferred_pool_type=preferred_pool_type
        )
        
        # Create a DistributedCommand class dynamically
        class DecoratedCommand(DistributedCommand):
            """Dynamically generated command from decorated function."""
            
            # Store reference to original function as staticmethod to prevent binding
            _original_function = staticmethod(func)
            _data_type = data_type_hint
            _config = config
            
            def __init__(self, **kwargs):
                # Extract task_id for stream key resolution (task_id is required positional param)
                task_id = kwargs.get('task_id')
                
                # Determine initial stream_key from decorator config (before super().__init__())
                # This allows us to pass it in distributed_kwargs, but data.stream_key will override later
                initial_stream_key = None
                if config.streaming_enabled and config.stream_key is not None:
                    if config.stream_key == "auto":
                        # Use command-specific pattern (will be set by _enable_streaming)
                        initial_stream_key = None  # Let _enable_streaming() generate it
                    else:
                        # Use custom stream key (may contain {task_id} placeholder)
                        if task_id and '{task_id}' in config.stream_key:
                            initial_stream_key = config.stream_key.replace('{task_id}', task_id)
                        else:
                            initial_stream_key = config.stream_key
                elif config.streaming_enabled:
                    # Default to unified task stream (need task_id)
                    if task_id:
                        from motet.core.distributed.tenant_keys import task_response_stream

                        initial_stream_key = task_response_stream(
                            kwargs.get("tenant_id"), task_id
                        )
                
                # Pass initial stream_key in distributed_kwargs if we have one
                # (data.stream_key will override this after super().__init__() if present)
                if initial_stream_key:
                    kwargs['stream_key'] = initial_stream_key
                
                super().__init__(**kwargs)
                
                # Apply configuration - check for dynamic capability inference first
                if config.capability_inference and self.data:
                    # Dynamically infer capabilities from data
                    try:
                        inferred_capabilities = config.capability_inference(self.data)
                        self.distributed_context.required_capabilities = set(inferred_capabilities)
                    except Exception as e:
                        # Fall back to default capabilities if inference fails
                        logger.warning("capability_inference_failed",
                                      command_type=command_type,
                                      error=str(e))
                        if config.required_capabilities:
                            self.distributed_context.required_capabilities = set(config.required_capabilities)
                elif config.required_capabilities:
                    # Use static capabilities
                    self.distributed_context.required_capabilities = set(config.required_capabilities)
                
                # Set stream_key with unified priority: data.stream_key → decorator stream_key → unified task stream
                if config.streaming_enabled:
                    # Priority 1: Check data.stream_key (runtime override - highest priority)
                    # Support both Pydantic model (.stream_key) and dict (e.g. after deserialization)
                    data_stream_key = None
                    if self.data is not None:
                        if hasattr(self.data, "stream_key") and self.data.stream_key:
                            data_stream_key = self.data.stream_key
                        elif isinstance(self.data, dict) and self.data.get("stream_key"):
                            data_stream_key = self.data["stream_key"]
                    
                    # Priority 2: Use decorator stream_key or generate based on config
                    if data_stream_key:
                        # Data stream_key overrides everything - use _enable_streaming for consistency
                        self._enable_streaming(stream_key=data_stream_key)
                        logger.debug(
                            "stream_key_from_data",
                            command_type=command_type,
                            stream_key=self.stream_key,
                        )
                    elif config.stream_key == "auto":
                        # Use command-specific pattern
                        self._enable_streaming()  # Will generate {command_type}_stream:{task_id}:events
                    elif initial_stream_key:
                        # Use the resolved stream key from decorator (unified task stream or custom)
                        self._enable_streaming(stream_key=initial_stream_key)
                    else:
                        # Default to unified task stream (now we have task_id from context)
                        from motet.core.distributed.tenant_keys import task_response_stream

                        self._enable_streaming(
                            stream_key=task_response_stream(
                                self.distributed_context.tenant_id,
                                self.distributed_context.task_id,
                            )
                        )
            
            @classmethod
            def _get_data_class(cls) -> Type[BaseCommandData]:
                """Return the Pydantic data class for this command."""
                return cls._data_type  # type: ignore[return-value]
            
            def get_command_type(self) -> str:
                """Return the command type identifier."""
                return command_type
            
            def _get_default_timeout(self) -> int:
                """Return configured timeout."""
                return config.timeout_seconds if config.timeout_seconds is not None else 60
            
            def _get_default_priority(self) -> int:
                """Return configured priority."""
                return config.priority if config.priority is not None else 60
            
            def _get_preferred_pool_type(self) -> Optional[str]:
                """Return preferred worker pool type for optimal execution (ADR-0033)."""
                return config.preferred_pool_type.value if config.preferred_pool_type else None
            
            def can_undo(self) -> bool:
                """Return whether command supports undo."""
                return config.can_undo
            
            def undo(self, worker_context: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
                """Undo operation (if supported)."""
                if not config.can_undo:
                    raise NotImplementedError(f"{command_type} does not support undo")
                # TODO: Implement undo logic
                raise NotImplementedError(f"Undo not yet implemented for {command_type}")
            
            def _do_execute(self, worker_context: Dict[str, Any]) -> Dict[str, Any]:
                """
                Execute the decorated function with automatic lifecycle events (ADR-0050).
                
                Emits lifecycle events to task-level stream:
                - command_start: When command execution begins
                - command_complete: When command succeeds
                - command_error: When command fails
                
                Creates MotetContext and makes it available via both:
                1. Context variable (get_motet_context())
                2. Explicit parameter (backward compatibility)
                
                Supports both patterns:
                - Pattern 1 (preferred): def my_command(data: MyData) - uses get_motet_context()
                - Pattern 2 (optional): def my_command(data: MyData, motet: MotetContext) - auto-injected
                """
                import time

                start_time = time.time()
                
                # Create MotetContext (delegates to command instance for all context)
                motet = MotetContext(
                    command_instance=self,
                    redis=worker_context.get('redis'),
                    worker_context=worker_context
                )

                # Nested in-process ``motet.do`` / ``motet.join`` (ADR-0134 serial
                # turn work) must restore the parent context. Clearing always
                # left ``run_agentic_loop``'s second iteration without MotetContext.
                try:
                    previous_motet = get_motet_context()
                except RuntimeError:
                    previous_motet = None
                _set_motet_context(motet)
                
                # Emit command_start event (ADR-0050)
                try:
                    motet.stream_event(
                        "command_start",
                        command_type=command_type,
                        parent_command_id=self.distributed_context.parent_command_id if self.distributed_context else None
                    )
                except Exception as e:
                    logger.warning("command_start_event_failed",
                                  command_type=command_type,
                                  command_id=self.command_id,
                                  error=str(e))
                
                try:
                    # Check function signature to support both patterns
                    sig = inspect.signature(self._original_function)
                    params = list(sig.parameters.keys())
                    
                    # Determine if function expects motet as explicit parameter
                    # Pattern 1 (preferred): def my_command(data: MyData)
                    # Pattern 2 (optional): def my_command(data: MyData, motet: MotetContext)
                    expects_motet_param = (
                        len(params) >= 2 and 
                        params[1] in ('motet', 'context', 'motet_context')
                    )
                    
                    if expects_motet_param:
                        # Old style: explicit parameter injection
                        result = self._original_function(self.data, motet)
                    else:
                        # New style: context variable only
                        result = self._original_function(self.data)
                    
                    execution_time_ms = (time.time() - start_time) * 1000
                    
                    # Emit command_complete event (ADR-0050)
                    try:
                        motet.stream_event(
                            "command_complete",
                            command_type=command_type,
                            execution_time_ms=execution_time_ms,
                            status="success"
                        )
                    except Exception as e:
                        logger.warning("command_complete_event_failed",
                                      command_type=command_type,
                                      command_id=self.command_id,
                                      error=str(e))
                    
                    # Always wrap. Domain payloads may contain a `status` key
                    # (workflow `completed`, HTTP 200, tool `ok`); that is data,
                    # not an ADR-0029 envelope. Do not sniff `'status' in result`.
                    warnings = list(getattr(motet, "_warnings", None) or [])
                    return self._create_success_response(
                        data=result,
                        execution_time_ms=execution_time_ms,
                        warnings=warnings or None,
                    )
                    
                except Exception as e:
                    execution_time_ms = (time.time() - start_time) * 1000

                    # Emit command_error event (ADR-0050)
                    try:
                        motet.stream_event(
                            "command_error",
                            command_type=command_type,
                            execution_time_ms=execution_time_ms,
                            error_type=type(e).__name__,
                            error_message=str(e)
                        )
                    except Exception as stream_error:
                        logger.warning("command_error_event_failed",
                                      command_type=command_type,
                                      command_id=self.command_id,
                                      error=str(stream_error))

                    # ADR-0131: a command that gives up cancels its own scope.
                    # Roots pushed command_id (and sync task_id); workflow pushed
                    # workflow_run_id; nested leaves that pushed nothing are a no-op.
                    try:
                        from motet.core.distributed.task_control import (
                            cancel_own_scope_for_command,
                        )

                        cancel_own_scope_for_command(
                            self,
                            reason=f"command_error: {type(e).__name__}: {e}",
                            source="command_error",
                        )
                    except Exception as cancel_err:
                        logger.warning(
                            "command_error_scope_cancel_failed",
                            command_type=command_type,
                            command_id=self.command_id,
                            error=str(cancel_err),
                        )

                    return self._create_error_response(
                        error=e,
                        execution_time_ms=execution_time_ms
                    )
                finally:
                    if previous_motet is None:
                        _clear_motet_context()
                    else:
                        _set_motet_context(previous_motet)
        
        # Set command type attribute on function for composition helpers to use
        setattr(func, "__command_type__", command_type)
        
        # Wrapper must not use @wraps(func): that copies __annotations__ from the
        # user function (only ``data``), so call sites passing task_id/conversation_id
        # fail pyright. Preserve identity metadata without inheriting the narrow signature.
        def wrapper(*args: Any, **kwargs: Any) -> DistributedCommand:
            """
            Wrapper that creates command instances with smart context-aware defaults.
            
            When called from within another decorated command, automatically inherits:
            - task_id (unless explicitly overridden)
            - conversation_id (unless explicitly overridden)
            - tenant_id (unless explicitly overridden)
            - principal_id (unless explicitly overridden)
            - motet_id (unless explicitly overridden)
            - parent_command_id (set to parent's command_id)
            - trace_id (unless explicitly overridden)
            
            Example (inside decorated command):
                # Minimal - context auto-filled
                command = tool_execution(data=tool_data)
                
                # With override
                command = tool_execution(data=tool_data, timeout_seconds=60)
            
            Example (outside decorated command):
                # Explicit params required
                command = tool_execution(
                    task_id="...",
                    conversation_id="...",
                    data=tool_data
                )
            """
            # Try to get current motet context for smart defaults
            parent_motet = None
            try:
                parent_motet = get_motet_context()
            except RuntimeError:
                # Not inside a decorated command - explicit params required
                pass
            
            # Apply smart defaults from parent context
            if parent_motet:
                # Auto-fill distributed context from parent if not explicitly provided
                context_defaults = {
                    'task_id': parent_motet.task_id,
                    'conversation_id': parent_motet.conversation_id,
                    'tenant_id': parent_motet.tenant_id,
                    'principal_id': parent_motet.principal_id,
                    'motet_id': parent_motet.motet_id,
                    'parent_command_id': parent_motet.command_id,  # Parent's command_id becomes child's parent_command_id
                    'trace_id': getattr(parent_motet, 'trace_id', None),
                    'cancel_scopes': list(getattr(parent_motet, 'cancel_scopes', None) or []),
                }
                
                # Only apply defaults for keys not explicitly provided
                for key, value in context_defaults.items():
                    if key not in kwargs and value:  # Skip None/empty values
                        kwargs[key] = value
            
            # This allows: cmd = my_decorated_function(task_id="...", data=...)
            return DecoratedCommand(*args, **kwargs)

        # Introspection metadata: keep explicit wrapper typing for pyright, but restore
        # ``__wrapped__`` compatibility for tests and SDK helpers that call the underlying
        # function directly.
        wrapper.__module__ = func.__module__
        wrapper.__name__ = func.__name__
        wrapper.__qualname__ = getattr(func, "__qualname__", func.__name__)
        wrapper.__doc__ = func.__doc__
        setattr(wrapper, "__wrapped__", func)
        
        # Preserve attributes for introspection (dynamic attrs on wrapper)
        setattr(wrapper, "__command_type__", command_type)
        setattr(wrapper, "__command_class__", DecoratedCommand)
        setattr(wrapper, "__original_function__", func)
        
        # Register with new unified CommandTypeRegistry (Phase 2)
        from motet.core.commands.command_type_registry import (
            command_type_registry,
            CommandImplementationType,
            first_docstring_line,
        )
        # Prefer explicit decorator description, then authoring function docstring.
        # register_command also derives from DecoratedCommand._original_function if needed.
        resolved_description = (description or "").strip() or first_docstring_line(func)
        command_type_registry.register_command(
            command_type=command_type,
            implementation=DecoratedCommand,  # Register the command class, not the wrapper
            implementation_type=CommandImplementationType.DECORATOR_BASED,
            data_class=data_type_hint,
            description=resolved_description,
            metadata={
                "timeout_seconds": config.timeout_seconds or 60,
                "priority": config.priority,
                "required_capabilities": [cap.value for cap in (config.required_capabilities or [])],
                "streaming_enabled": config.streaming_enabled,
                "can_undo": config.can_undo
            },
            version="1.0.0"
        )
        
        # CRITICAL: Also register the data class in CommandDataRegistry
        # This enables proper serialization/deserialization via CommandDataManager
        # Use overwrite=True since decorator registration is authoritative
        from motet.core.commands.command_data_registry import register_command_data
        register_command_data(command_type, data_type_hint, overwrite=True)
        
        return wrapper
    
    return decorator


def motet_tool(
    description: str,
    name: Optional[str] = None,
    *,
    category: str = "general",
    schema: Optional[Type[BaseModel]] = None,
    **kwargs: Any,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Decorator for registering bundle tools under the Motet namespace (ADR-0089).

    Use in bundle tools/*.py. When the module is loaded within bundle_tool_namespace(bundle_id),
    the tool is registered as {bundle_id}.{name or function.__name__}. Outside bundle context
    (e.g. tests), the function is returned unchanged without registration.

    Args:
        description: Human-readable tool description (required).
        name: Tool name; defaults to the decorated function's __name__.
        category: Tool category (e.g. "general", "http", "memory").
        schema: Optional Pydantic model for tool parameters.
        **kwargs: Passed through to ToolRegistry.register (priority, triggers, etc.).

    Returns:
        Decorated function (registered when bundle_id context is set).
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        bundle_id = _get_bundle_tool_namespace()
        tool_name = name if name is not None else func.__name__
        if bundle_id:
            full_name = f"{bundle_id}.{tool_name}"
            try:
                from motet.core.tools.registry import registry as tool_registry
                tool_registry.register(
                    full_name,
                    description=description,
                    func=func,
                    tool_schema=schema,
                    category=category,
                    **kwargs,
                )
                logger.debug("motet_tool_registered", name=full_name, bundle_id=bundle_id)
            except Exception as e:
                logger.error(
                    "motet_tool_register_failed",
                    name=full_name,
                    bundle_id=bundle_id,
                    error=str(e),
                    exc_info=True,
                )
                raise RuntimeError(f"Failed to register @motet.tool {full_name}: {e}") from e
        return func

    return decorator


__all__ = [
    "distributed_command",
    "MotetContext",
    "DecoratedCommandConfig",
    "get_motet_context",
    "resolve_current_identity",
    "bundle_command_namespace",
    "bundle_tool_namespace",
    "motet_tool",
]
