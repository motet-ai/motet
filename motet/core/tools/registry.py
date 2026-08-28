"""
Motet - Tool Registry

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-13

Description:
    Comprehensive tool registry system for the Motet distributed framework.
    Provides centralized tool registration, discovery, and execution with
    comprehensive observability, resilience patterns, and performance monitoring.
    Includes tool validation, error handling, and distributed coordination.

Dependencies:
    - pydantic: Data validation and model definitions
    - structlog: Structured logging and observability
    - typing: Type hints and annotations
    - Observability and metrics system
    - Resilience patterns and circuit breakers

Usage:
    from motet.core.tools.registry import ToolRegistry, register_tool

    # Register tool
    @register_tool("my_tool", "Description of my tool")
    def my_tool(params: Dict[str, Any]) -> Dict[str, Any]:
        return {"result": "success"}

    # Get registry
    registry = ToolRegistry()
    tools = registry.get_tools()

Notes:
    - Provides centralized tool registration and discovery
    - Includes comprehensive observability and metrics
    - Supports resilience patterns and circuit breakers
    - Includes tool validation and error handling
    - Supports distributed tool execution
    - Integrates with observability and tracing systems
    - Includes performance monitoring and latency tracking
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from pydantic import BaseModel, Field, ValidationError
from typing import Any, Callable, Coroutine, Dict, Optional, List, Set, Tuple, Union, cast, get_args

import structlog

from ..config import Config
from ..observability.metrics import observe_tool_latency, increment_tool_errors, increment_tool_requests
from ..observability.tracing import get_tracer
from ..registry import RegistryScope, ScopeFilter, RegistryEntry, ScopedRegistry, scope_from_qualified_name
from ..resilience import get_breaker
from ..workers.concurrency_primitives import worker_sleep, WorkerLocal, WorkerLock


ToolFunc = Callable[[Dict[str, Any]], Dict[str, Any]]  # ADR-0033: Synchronous tools


# Worker-local storage for runtime stack (pool-agnostic - ADR-0033)
# Uses WorkerLocal instead of global variable for thread-safety in threads/gevent pools
_runtime_stack_local = WorkerLocal()


def set_runtime_stack(stack: Any) -> None:
    """
    Set the runtime stack for the current worker/thread/greenlet.
    
    Uses WorkerLocal for thread-safety across all Celery pool types:
    - Fork pool: Process-isolated (each process has its own)
    - Threads pool: Thread-local (each thread has its own)
    - Gevent pool: Greenlet-local (each greenlet has its own)
    
    Args:
        stack: MotetStack instance with memory, tools, etc.
    """
    _runtime_stack_local.stack = stack


def get_runtime_stack() -> Optional[Any]:
    """
    Get the runtime stack for the current worker/thread/greenlet.
    
    Returns:
        MotetStack instance if set, None otherwise
    """
    return getattr(_runtime_stack_local, 'stack', None)


def _coerce_boolean_null_params(
    schema: type[BaseModel], params: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[str]]:
    """Coerce boolean arguments to ``None`` for ``string | null`` schema fields.

    Small local models occasionally emit ``false``/``true`` for optional string
    parameters (e.g. ``execute_js: false`` where the schema is ``string | null``,
    ADR-0115), which fails Pydantic validation and burns a reasoning iteration on
    the retry. When a field's annotation accepts ``None`` and ``str`` but no
    boolean or numeric type, a boolean argument can only mean "not provided", so
    it is coerced to ``None`` instead of rejecting the whole call.

    Returns the (possibly copied) params and the list of coerced field names.
    """
    coerced: Optional[Dict[str, Any]] = None
    changed: List[str] = []
    for field_name, field in schema.model_fields.items():
        if not isinstance(params.get(field_name), bool):
            continue
        args = get_args(field.annotation)
        if not args or type(None) not in args or str not in args:
            continue
        if any(t in args for t in (bool, int, float)):
            continue
        if coerced is None:
            coerced = dict(params)
        coerced[field_name] = None
        changed.append(field_name)
    return (coerced if coerced is not None else params), changed


class RegisteredTool(BaseModel):
    name: str
    description: str
    func: ToolFunc
    tool_schema: Optional[Union[type[BaseModel], Dict[str, Any]]] = Field(
        default=None,
        alias="schema",
    )  # Pydantic model or MCP JSON Schema dict
    
    model_config = {"arbitrary_types_allowed": True, "populate_by_name": True}
    triggers: List[str] = Field(default_factory=list)
    priority: int = 10
    estimate_tokens: Optional[Callable[[Dict[str, Any]], int]] = None
    parse_params: Optional[Callable[[str, str], Dict[str, Any]]] = None
    observation_formatter: Optional[Callable[[Dict[str, Any]], str]] = None
    breaker_failure_threshold: Optional[int] = None
    breaker_reset_timeout_seconds: Optional[float] = None
    max_retries: int = 0
    retry_backoff_seconds: float = 0.5
    category: str = "general"
    contextualize_observation: Optional[bool] = None
    # Optional planner hints
    default_timeout_seconds: Optional[float] = None
    suggested_max_calls: Optional[int] = None
    cost_class: Optional[str] = None  # "low"|"medium"|"high" or custom

    # Presentation hints (used by higher-level reasoning to decide if an additional
    # post-processing LLM call is required, and how to render outputs deterministically).
    # Kept intentionally unopinionated (dict) so MCP services can supply metadata.
    #
    # Example:
    # {
    #   "user_facing": true,
    #   "requires_llm": false,
    #   "content_kind": "list"
    # }
    presentation: Optional[Dict[str, Any]] = None
    
    # Data capabilities for trace-based reasoning
    data_types: List[str] = Field(default_factory=list)  # Types of data this tool can provide
    keywords: List[str] = Field(default_factory=list)    # Keywords that indicate this tool should be used

    # Routing hints (worker capabilities)
    # These are declared by the registry at registration time (built-ins and MCP tools).
    # Stored as strings to avoid import cycles into orchestration command modules.
    required_capabilities: List[str] = Field(default_factory=list)

    # ADR-0110 artifact preparation manifest. Tools with a prep manifest are
    # hidden from ordinary agent tool export by default while remaining
    # available to the preparation selector/planner surface.
    prep_manifest: Optional[Any] = None
    expose_to_agents: bool = True
    
    # Context management
    context_requirement: Optional[Any] = None  # ContextRequirement for this tool



class ToolRegistry(ScopedRegistry[RegisteredTool]):
    """Thread-safe tool registry (ADR-0069: watcher thread mutates while tasks read)."""

    def __init__(self) -> None:
        super().__init__(registry_name="tool_registry")
        self._log = structlog.get_logger()
        self._cfg: Optional[Config] = None
        self._role_policies: Optional[Dict[str, Set[str]]] = None  # Cached role policies
        self._trigger_index: Dict[str, tuple[str, str]] = {}  # trigger -> (tool_name, trigger) for O(1) lookup
        self._triggers_sorted: List[str] = []  # Triggers sorted by length (longest first) for efficient prefix matching
        self._index_lock = WorkerLock()  # ADR-0069: thread-safe trigger index mutations

    def _default_required_capabilities(self, *, tool_name: str, category: str) -> List[str]:
        """
        Determine default routing capabilities for a tool based on its declared category.

        This is evaluated once at registration time so capability inference does not depend on
        naming conventions (item 4) and works for both built-in and MCP-registered tools.
        """
        cat = (category or "general").lower().strip()
        # Always require tool execution for any tool call
        caps = ["TOOL_EXECUTION"]

        if cat in {"http", "search"}:
            caps.append("HTTP_OPERATIONS")
        elif cat in {"memory"}:
            caps.append("MEMORY_OPERATIONS")
        elif cat in {"filesystem", "file"}:
            caps.append("FILE_OPERATIONS")
        elif cat in {"browser"}:
            caps.append("BROWSER_OPERATIONS")

        # MCP tools are handled by TOOL_EXECUTION by default; service-specific routing can be
        # added later by setting required_capabilities explicitly at registration.
        return caps

    # ADR-0061 hard cutover:
    # Tool observation persistence is removed in favor of ToolInvocation + ToolArtifact.
    # The registry no longer writes MemoryItem(type="tool_observation").

    def set_config(self, cfg: Config) -> None:
        self._cfg = cfg
        # Parse and cache role policies at config time (performance optimization)
        if cfg.tool_role_policies_json:
            try:
                parsed = json.loads(cfg.tool_role_policies_json)
                self._role_policies = {role: set(tools) for role, tools in parsed.items()}
            except Exception as exc:
                self._log.warning("tool_policy_parse_error", error=str(exc), component="tool")
                self._role_policies = None
        else:
            self._role_policies = None

    def set_runtime_stack(self, stack: Any) -> None:
        """
        Set the runtime stack for the current worker/thread/greenlet.
        
        Delegates to the module-level set_runtime_stack() function.
        """
        try:
            set_runtime_stack(stack)
        except Exception as e:
            # best-effort set; stack may be unavailable in some contexts
            self._log.debug("set_runtime_stack_failed", error=str(e))

    def register(  # type: ignore[override]  # domain-specific signature (RegistryProtocol §152)
        self,
        name: str,
        item: Optional[RegisteredTool] = None,
        *,
        scope: Optional[RegistryScope] = None,
        metadata: Optional[Dict[str, Any]] = None,
        # Tool-specific registration args (ADR-0089)
        description: Optional[str] = None,
        func: Optional[ToolFunc] = None,
        tool_schema: Optional[type[BaseModel]] = None,
        # Backwards-compatible alias used by older call sites/tests.
        schema: Optional[type[BaseModel]] = None,
        triggers: Optional[List[str]] = None,
        priority: int = 10,
        estimate_tokens: Optional[Callable[[Dict[str, Any]], int]] = None,
        parse_params: Optional[Callable[[str, str], Dict[str, Any]]] = None,
        observation_formatter: Optional[Callable[[Dict[str, Any]], str]] = None,
        breaker_failure_threshold: Optional[int] = None,
        breaker_reset_timeout_seconds: Optional[float] = None,
        max_retries: int = 0,
        retry_backoff_seconds: float = 0.5,
        category: str = "general",
        contextualize_observation: Optional[bool] = None,
        default_timeout_seconds: Optional[float] = None,
        suggested_max_calls: Optional[int] = None,
        cost_class: Optional[str] = None,
        data_types: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
        context_requirement: Optional[Any] = None,
        presentation: Optional[Dict[str, Any]] = None,
        required_capabilities: Optional[List[str]] = None,
        prep_manifest: Optional[Any] = None,
        expose_to_agents: Optional[bool] = None,
    ) -> None:
        if tool_schema is None and schema is not None:
            tool_schema = schema
        trigger_list = list(triggers or [])

        if item is None:
            if description is None or func is None:
                raise ValueError("register requires either item or both description and func")

            caps = list(required_capabilities or [])
            if not caps:
                caps = self._default_required_capabilities(tool_name=name, category=category)
            resolved_expose_to_agents = bool(expose_to_agents) if expose_to_agents is not None else prep_manifest is None

            item = RegisteredTool(
                name=name,
                description=description,
                func=func,
                schema=tool_schema,
                triggers=trigger_list,
                priority=priority,
                estimate_tokens=estimate_tokens,
                parse_params=parse_params,
                observation_formatter=observation_formatter,
                breaker_failure_threshold=breaker_failure_threshold,
                breaker_reset_timeout_seconds=breaker_reset_timeout_seconds,
                max_retries=max_retries,
                retry_backoff_seconds=retry_backoff_seconds,
                category=category,
                contextualize_observation=contextualize_observation,
                default_timeout_seconds=default_timeout_seconds,
                suggested_max_calls=suggested_max_calls,
                cost_class=cost_class,
                data_types=list(data_types or []),
                keywords=list(keywords or []),
                required_capabilities=caps,
                context_requirement=context_requirement,
                presentation=presentation,
                prep_manifest=prep_manifest,
                expose_to_agents=resolved_expose_to_agents,
            )
        else:
            trigger_list = list(item.triggers or [])

        with self._index_lock:
            resolved_scope = scope or scope_from_qualified_name(name)
            super().register(name, item, scope=resolved_scope, metadata=metadata)
        
            # Build trigger index for O(1) lookup (performance optimization)
            for trigger in trigger_list:
                self._trigger_index[trigger] = (name, trigger)
            
            # Update sorted trigger list for efficient prefix matching (longest first)
            self._triggers_sorted = sorted(self._trigger_index.keys(), key=len, reverse=True)

    # get / get_entry / list_items / list_entries inherited from ScopedRegistry

    def supports(self, name: str) -> bool:
        return self.get(name) is not None

    def get_all_tool_names(self) -> List[str]:
        """Return a sorted list of all registered tool names."""
        return sorted(self.list_items().keys())

    def unregister(self, key: str) -> bool:
        """
        Remove a single tool by exact name (registry key).
        Returns True if the tool was found and removed, False otherwise.
        """
        with self._index_lock:
            tool = self.get(key)
            if tool is None:
                return False
            super().unregister(key)
            if tool.triggers:
                for t in tool.triggers:
                    self._trigger_index.pop(t, None)
            self._triggers_sorted = sorted(self._trigger_index.keys(), key=len, reverse=True)
            return True

    def unregister_by_prefix(self, prefix: str) -> int:
        """
        Remove all tools whose name starts with prefix (ADR-0069).
        Used to unregister all tools for an MCP service: prefix = f"mcp.{service_id}.".
        Returns the number of tools removed. Rebuilds trigger index after removal.
        """
        with self._index_lock:
            all_tools = self.list_items()
            to_remove = [n for n in all_tools if n.startswith(prefix)]
            for name in to_remove:
                tool = all_tools.get(name)
                super().unregister(name)
                if tool and tool.triggers:
                    for t in tool.triggers:
                        self._trigger_index.pop(t, None)
            self._triggers_sorted = sorted(self._trigger_index.keys(), key=len, reverse=True)
            return len(to_remove)

    def get_scope(self, name: str) -> Optional[RegistryScope]:
        """Return scope metadata for a tool name."""
        entry = self.get_entry(name)
        return entry.scope if entry else None

    def list_visible(self, scope_filter: ScopeFilter) -> Dict[str, RegisteredTool]:
        """Return tools visible to the given request context."""
        return super().list_visible(scope_filter)

    def list_visible_entries(self, scope_filter: ScopeFilter) -> List[RegistryEntry[RegisteredTool]]:
        """Return visible entries with scope metadata."""
        return super().list_visible_entries(scope_filter)

    def unregister_namespace(self, namespace: str) -> List[str]:
        """
        Remove all tools in a namespace and return removed keys.

        Visibility namespace is authoritative; key-prefix fallback is kept for legacy names.
        """
        with self._index_lock:
            all_entries = self.list_entries()
            removed_keys = sorted(
                entry.key
                for entry in all_entries
                if entry.scope.namespace == namespace or entry.key.startswith(f"{namespace}.")
            )
            all_tools = self.list_items()
            for name in removed_keys:
                tool = all_tools.get(name)
                super().unregister(name)
                if tool and tool.triggers:
                    for trigger in tool.triggers:
                        self._trigger_index.pop(trigger, None)
            self._triggers_sorted = sorted(self._trigger_index.keys(), key=len, reverse=True)
            return removed_keys

    def find_tools_for_trace(self, trace: str) -> List[tuple[str, RegisteredTool, float]]:
        """Find tools that can provide data for a given trace, scored by relevance."""
        trace_lower = trace.lower()
        scored_tools = []
        tools_snapshot = self.list_items()
        for name, tool in tools_snapshot.items():
            score = 0.0
            
            # Keyword matching (higher weight)
            for keyword in tool.keywords:
                if keyword.lower() in trace_lower:
                    score += 3.0
            
            # Data type matching (medium weight)  
            for data_type in tool.data_types:
                if data_type.lower() in trace_lower:
                    score += 2.0
            
            # Category matching (lower weight)
            if tool.category != "general" and tool.category.lower() in trace_lower:
                score += 1.0
            
            # Priority bonus (normalized)
            score += tool.priority / 10.0
            
            if score > 0:
                scored_tools.append((name, tool, score))
        
        # Sort by score descending
        scored_tools.sort(key=lambda x: x[2], reverse=True)
        return scored_tools
    
    def _apply_context_management(
        self, 
        result: Dict[str, Any], 
        tool: RegisteredTool, 
        tool_name: str
    ) -> Dict[str, Any]:
        """Apply context management to tool results if configured."""
        try:
            # Debug logging for web search
            if tool_name in ("web_search", "core.web_search"):
                try:
                    import structlog
                    logger = structlog.get_logger()
                    logger.info("web_search_context_debug",
                               tool_name=tool_name,
                               has_context_requirement=bool(tool.context_requirement),
                               should_skip=self._should_skip_context_processing(result),
                               result_size=len(str(result)),
                               main_content_size=len(str(result.get("main_content", ""))))
                except Exception as e:
                    # optional debug logging; failure non-critical
                    self._log.debug("web_search_debug_log_failed", error=str(e))
            
            # Respect explicit contextualize_observation=False (user wants full output)
            if tool.contextualize_observation is False:
                return result
            
            # Only apply context management if tool has specific requirements
            # or if the result is large enough to warrant processing
            if not tool.context_requirement and self._should_skip_context_processing(result):
                return result
            
            # Import here to avoid circular dependencies
            from .context_manager import ContextManager
            
            # Get or create context manager
            if not hasattr(self, '_context_manager'):
                from .registry import get_runtime_stack
                stack = get_runtime_stack()
                self._context_manager = ContextManager(stack=stack)
            
            # Process the result (synchronous - ADR-0033)
            processed_result = self._context_manager.process_tool_response(
                response=result,
                tool_name=tool_name,
                tool_category=tool.category,
                available_context_tokens=self._get_available_context_tokens(tool)
            )
            
            return processed_result
            
        except Exception as e:
            # If context management fails, return original result
            try:
                import structlog
                structlog.get_logger().warning("context_management_failed", 
                                              tool=tool_name, error=str(e))
            except Exception as inner_e:
                # best-effort error reporting; avoid raising during warning emit
                self._log.debug("context_management_warning_emit_failed", error=str(inner_e))
            return result
    
    def _should_skip_context_processing(self, result: Dict[str, Any]) -> bool:
        """Determine if context processing should be skipped for small results."""
        # Skip for small results or error responses
        if "error" in result:
            return True
        
        # Estimate result size
        result_str = str(result)
        if len(result_str) < 1000:  # Less than ~250 tokens
            return True
        
        return False
    
    def _get_available_context_tokens(self, tool: RegisteredTool) -> int:
        """Get available context tokens for a tool."""
        if tool.context_requirement:
            return getattr(tool.context_requirement, 'max_tokens', 4000)
        
        # Default based on category
        category_defaults = {
            "http": 8000,
            "filesystem": 16000,
            "memory": 6000,
            "math": 1000,
            "system": 4000
        }
        
        return category_defaults.get(tool.category, 4000)

    def parse_line(self, line: str) -> Optional[Dict[str, Any]]:
        """Parse line using trigger index for O(T) lookup instead of O(N*M) where T=total triggers.
        
        Performance optimization: Uses pre-sorted trigger list and index for fast prefix matching.
        Tries longest triggers first to handle cases like "math_advanced:" vs "math:".
        """
        ln = line.strip()
        if not ln:
            return None
        
        # Use cached sorted trigger list for fast lookup (avoid sorting on every call)
        for trigger in self._triggers_sorted:
            if ln.startswith(trigger):
                tool_name, _ = self._trigger_index[trigger]
                rt = self.get(tool_name)
                if not rt:
                    continue
                
                params: Dict[str, Any] = {}
                if rt.parse_params:
                    params = rt.parse_params(ln, trigger)
                else:
                    params = {"text": ln[len(trigger):].strip()}
                return {"name": tool_name, "params": params, "priority": rt.priority}
        
        return None

    def estimate(self, name: str, params: Dict[str, Any]) -> int:
        rt = self.get(name)
        if not rt or not rt.estimate_tokens:
            return 10
        try:
            return int(rt.estimate_tokens(params))
        except Exception as e:
            # fallback to default token estimate
            self._log.debug("estimate_tokens_failed", tool=name, error=str(e))
            return 10

    def format_observation(self, name: str, result: Dict[str, Any]) -> Optional[str]:
        rt = self.get(name)
        if not rt:
            return None
        if rt.observation_formatter:
            try:
                return rt.observation_formatter(result)
            except Exception as e:
                # fallback when formatter raises; caller handles None
                self._log.debug("observation_formatter_failed", tool=name, error=str(e))
                return None
        if "status" in result and "error" in result:
            return f"{name}(status={result['status']}, error={result['error']})"
        if "status" in result:
            return f"{name}(status={result['status']})"
        if "result" in result:
            return f"{name}(result={result['result']})"
        if "error" in result:
            return f"{name}(error={result['error']})"
        return f"{name}(ok)"

    def describe(self, *, audience: str = "agent") -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for rt in self.list_items().values():
            if audience == "agent" and not rt.expose_to_agents:
                continue
            if audience == "prep_planner" and rt.prep_manifest is None:
                continue
            schema_json = None
            try:
                if isinstance(rt.tool_schema, type) and issubclass(rt.tool_schema, BaseModel):
                    schema_json = rt.tool_schema.model_json_schema()
                elif isinstance(rt.tool_schema, dict):
                    schema_json = dict(rt.tool_schema)
                else:
                    schema_json = None
            except Exception as e:
                # best-effort schema extraction; fallback to None for describe output
                self._log.debug("schema_extraction_failed", tool=rt.name, error=str(e))
                schema_json = None
            item = {
                "name": rt.name,
                "description": rt.description,
                "category": rt.category,
                "triggers": list(rt.triggers or []),
                "priority": rt.priority,
                "schema": schema_json,
                "data_types": list(rt.data_types or []),
                "keywords": list(rt.keywords or []),
                "expose_to_agents": rt.expose_to_agents,
            }
            # Attach x-extensions for non-standard metadata (for MCP/other consumers)
            try:
                x = {
                    "observation": {
                        "contextualize": rt.contextualize_observation,
                    },
                    "planner_hints": {
                        "default_timeout_seconds": rt.default_timeout_seconds,
                        "suggested_max_calls": rt.suggested_max_calls,
                        "cost_class": rt.cost_class,
                    },
                    "resilience": {
                        "max_retries": rt.max_retries,
                        "retry_backoff_seconds": rt.retry_backoff_seconds,
                    },
                }
                if rt.presentation is not None:
                    x["presentation"] = rt.presentation
                if rt.prep_manifest is not None:
                    manifest = rt.prep_manifest
                    x["prep_manifest"] = (
                        manifest.model_dump(mode="json")
                        if hasattr(manifest, "model_dump")
                        else manifest
                    )
                    x["required_capabilities"] = list(rt.required_capabilities or [])
                item["x-imf"] = x
            except Exception as e:
                # non-critical x-extensions enrichment
                self._log.debug("x_extensions_build_failed", tool=rt.name, error=str(e))
            out.append(item)
        return out

    def _execute_tool_only(
        self,
        name: str,
        params: Dict[str, Any],
        *,
        allow: Optional[set[str]] = None,
        deny: Optional[set[str]] = None,
        timeout: Optional[float] = None,
        role: Optional[str] = None,
        persist_observation: bool = True,
    ) -> Dict[str, Any]:
        """
        Execute a tool in-process only (no command delegation). ADR-0084 inner path.

        Used by tool_execution command and by motet.tools.execute when not delegating
        (no context or already inside tool_execution). Same contract as execute().
        """
        reg = self.get(name)
        status = "success"
        if not reg:
            status = "not_found"
            return {"status": status, "error": "tool not found"}
        if deny and name in deny:
            status = "denied_by_denylist"
            return {"status": status, "error": "tool denied"}
        if allow is not None and name not in allow:
            status = "denied_by_allowlist"
            return {"status": status, "error": "tool not allowed"}

        if self._role_policies and role:
            allowed_for_role = self._role_policies.get(role)
            if allowed_for_role and name not in allowed_for_role:
                status = "denied_by_role"
                return {"status": status, "error": "tool not allowed for role"}

        if reg.tool_schema is not None:
            try:
                if isinstance(reg.tool_schema, type) and issubclass(reg.tool_schema, BaseModel):
                    params, coerced_fields = _coerce_boolean_null_params(reg.tool_schema, params)
                    if coerced_fields:
                        self._log.info(
                            "tool_params_boolean_coerced_to_null",
                            tool=reg.name,
                            fields=coerced_fields,
                        )
                    validated = reg.tool_schema(**params)
                    params = validated.model_dump()
                elif isinstance(reg.tool_schema, dict):
                    # JSON Schema dict is exported metadata; runtime validator remains optional.
                    params = dict(params)
            except ValidationError as exc:
                status = "validation_error"
                return {"status": status, "error": f"validation error: {exc.errors()}"}

        tracer = get_tracer("imf.tools")
        start = time.perf_counter()

        try:
            increment_tool_requests(reg.name)
        except Exception as e:
            # best-effort metrics; don't fail tool execution for observability
            self._log.debug("increment_tool_requests_failed", tool=reg.name, error=str(e))

        with tracer.start_as_current_span(f"tool:{reg.name}") as span:
            try:
                use_breaker = (
                    reg.breaker_failure_threshold is not None
                    or reg.category in {"http", "mcp", "external"}
                )

                def _call_once():
                    out = reg.func(params)
                    if asyncio.iscoroutine(out):
                        from ..utils.async_helpers import run_async_safe
                        return run_async_safe(cast(Coroutine[Any, Any, Any], out))
                    if inspect.isawaitable(out):
                        from ..utils.async_helpers import run_async_safe

                        async def _await_any() -> Any:
                            return await cast(Any, out)

                        return run_async_safe(_await_any())
                    return out

                if use_breaker:
                    cfg = self._cfg or Config()
                    failure_threshold = reg.breaker_failure_threshold or int(
                        getattr(cfg, "breaker_tool_failure_threshold", 5) or 5
                    )
                    reset_timeout = reg.breaker_reset_timeout_seconds or float(
                        getattr(cfg, "breaker_tool_reset_timeout_seconds", 30.0) or 30.0
                    )
                    br = get_breaker(
                        f"tool:{name}",
                        failure_threshold=failure_threshold,
                        reset_timeout_seconds=reset_timeout,
                    )
                    attempt = 0
                    last_exc: Optional[Exception] = None
                    while True:
                        try:
                            result = br.call(_call_once)
                            break
                        except Exception as exc:
                            last_exc = exc
                            if attempt >= max(0, reg.max_retries):
                                raise
                            attempt += 1
                            worker_sleep(max(0.0, reg.retry_backoff_seconds))
                    if hasattr(span, "set_attribute"):
                        span.set_attribute("imf.tool.breaker_state", br.state)
                else:
                    attempt = 0
                    while True:
                        try:
                            result = _call_once()
                            break
                        except Exception:
                            if attempt >= max(0, reg.max_retries):
                                raise
                            attempt += 1
                            worker_sleep(max(0.0, reg.retry_backoff_seconds))

                if isinstance(result, dict):
                    if "status" not in result and (
                        "error" in result or "result" in result or "path" in result or "text" in result
                    ):
                        result = {"status": status, **result}
                    result = self._apply_context_management(result, reg, name)
                    return result

                final_result = {"status": status, "data": result}
                final_result = self._apply_context_management(final_result, reg, name)
                return final_result
            except RuntimeError as exc:
                if str(exc) in {"circuit_open", "circuit_half_open_probe_in_flight"}:
                    status = str(exc)
                    if hasattr(span, "set_attribute"):
                        span.set_attribute("imf.tool.breaker_blocked", True)
                    try:
                        increment_tool_errors(reg.name, status)
                    except Exception as e:
                        # best-effort metrics; don't fail on observability
                        self._log.debug("increment_tool_errors_failed", tool=reg.name, error=str(e))
                    return {"status": status, "error": status}
                raise
            finally:
                duration = time.perf_counter() - start
                try:
                    observe_tool_latency(reg.name, duration)
                except Exception as e:
                    # best-effort observability; don't fail tool execution
                    self._log.debug("observe_tool_latency_failed", tool=reg.name, error=str(e))
                try:
                    self._log.info(
                        "tool_exec", name=reg.name, status=status, role=role,
                        duration=duration, component="tool"
                    )
                except Exception as e:
                    # best-effort execution log; don't fail tool for log failure
                    pass  # avoid recursive log if structlog itself fails

    def execute(
        self,
        name: str,
        params: Dict[str, Any],
        *,
        allow: Optional[set[str]] = None,
        deny: Optional[set[str]] = None,
        timeout: Optional[float] = None,
        role: Optional[str] = None,
        persist_observation: bool = True,
    ) -> Dict[str, Any]:
        """Execute a tool. When called directly on the registry, uses inner path only (ADR-0084)."""
        return self._execute_tool_only(
            name, params,
            allow=allow, deny=deny, timeout=timeout, role=role,
            persist_observation=persist_observation,
        )


registry = ToolRegistry()

__all__ = [
    "registry",
    "ToolRegistry",
    "RegisteredTool",
    "set_runtime_stack",
    "get_runtime_stack",
]


