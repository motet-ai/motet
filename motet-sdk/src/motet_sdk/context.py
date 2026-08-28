"""
Motet SDK - MotetContext protocol (type stubs for IDE and type checking).

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Bundle authors type-hint the second parameter of commands as MotetContext.
At runtime the actual implementation is injected by the Motet runtime.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, Tuple


class MotetContext(Protocol):
    """
    Protocol for the context injected into distributed commands.

    Use this type for the second parameter of your command function so that
    IDEs and type checkers understand motet.*. The real implementation
    is provided by the Motet runtime.
    """

    # --- Context identifiers ---
    @property
    def task_id(self) -> str:
        """Current task identifier."""
        ...

    @property
    def conversation_id(self) -> str:
        """Current conversation identifier."""
        ...

    @property
    def command_id(self) -> str:
        """Current command identifier."""
        ...

    @property
    def tenant_id(self) -> str:
        """Tenant context."""
        ...

    @property
    def principal_id(self) -> str:
        """User/principal context."""
        ...

    @property
    def motet_id(self) -> str:
        """Motet/environment identifier."""
        ...

    @property
    def metadata(self) -> Dict[str, Any]:
        """Command metadata (e.g. model_provider, model_name)."""
        ...

    @property
    def stream_key(self) -> str:
        """Redis stream key for this command (streaming)."""
        ...

    @property
    def redis(self) -> Any:
        """Redis connection (for state/streaming)."""
        ...

    # --- Resource access ---
    @property
    def stack(self) -> Any:
        """Core stack instance (advanced)."""
        ...

    @property
    def memory(self) -> Any:
        """Memory helper. store()/recall()/tag()/forget(); delegates to memory commands when context exists."""
        ...

    @property
    def tools(self) -> Any:
        """Tools helper. execute()/get()/list(); execute delegates to tool_execution when context exists; use canonical tool names."""
        ...

    @property
    def agents(self) -> Any:
        """Agents helper. list()/get()/turn(); turn delegates to agent_turn command."""
        ...

    @property
    def models(self) -> Any:
        """Models helper. list()/get()/infer()/stream(); infer/stream delegate to model commands."""
        ...

    @property
    def workflows(self) -> Any:
        """Workflows helper. list()/get()/run(); run delegates to workflow_execution command."""
        ...

    @property
    def schedules(self) -> Any:
        """Schedules helper. create()/list(); create uses ScheduleCommand."""
        ...

    @property
    def commands(self) -> Any:
        """Commands helper. list()/get()/run() for discovery and run-by-type (dynamic dispatch)."""
        ...

    @property
    def conversations(self) -> Any:
        """Conversations helper. list()/get()/clear()/register()/rename(); delegates to conversation commands when context exists."""
        ...

    @property
    def vault(self) -> Any:
        """Vault client for secure credentials."""
        ...

    @property
    def event_bus(self) -> Any:
        """Event bus for publishing custom events."""
        ...

    @property
    def artifact_store(self) -> Any:
        """Artifact store with pre-bound isolation context."""
        ...

    @property
    def distributed_context(self) -> Any:
        """Full distributed context (advanced)."""
        ...

    # --- Helpers ---
    def resolve_conversation_id(self, explicit_id: Optional[str] = None) -> str:
        """Resolve conversation ID (explicit override or context fallback)."""
        ...

    def log_fields(self, **extra: Any) -> Dict[str, Any]:
        """Standard logging fields for distributed context."""
        ...

    # --- Event observation ---
    def observe_events(
        self,
        event_types: Any,  # Set[str] at runtime
        callback: Any,     # Callable[[Any], None] at runtime
        priority: Optional[int] = None,
        custom_filter: Optional[Any] = None,
    ) -> Any:
        """Observe specific events during command execution (context manager)."""
        ...

    # --- Streaming ---
    def ensure_stream(self, ttl_seconds: int = 3600, stream_key: Optional[str] = None) -> None:
        """Ensure stream exists with TTL."""
        ...

    def stream_event(self, event_type: str, stream_key: Optional[str] = None, **fields: Any) -> None:
        """Emit an event to the command stream."""
        ...

    def stream_token(self, token: str, stream_key: Optional[str] = None) -> None:
        """Stream an LLM token to the command stream."""
        ...

    def publish_event(self, event: Dict[str, Any]) -> None:
        """Publish event to the event bus."""
        ...

    # --- Response helpers ---
    def add_warning(self, message: str) -> None:
        """Attach a non-fatal warning copied onto the command envelope."""
        ...

    @property
    def last_metadata(self) -> Optional[Any]:
        """Metadata from the most recent do / join / apply / maybe."""
        ...

    def dispatch(
        self,
        commands: List[Any],
        max_parallel: Optional[int] = None,
        **distributed_kwargs: Any,
    ) -> List[str]:
        """Dispatch commands without waiting (fire-and-forget). Returns command IDs."""
        ...

    # --- Command composition ---
    def do(
        self,
        command: Any,
        data: Any,
        *,
        command_template: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Execute command and return unwrapped data, or raise on error."""
        ...

    def join(
        self,
        command_data_pairs: List[Tuple[Any, Any]],
        *,
        raise_on_first_error: bool = True,
    ) -> List[Any]:
        """Execute multiple commands in parallel; return list of unwrapped results."""
        ...

    def apply(
        self,
        command: Any,
        inputs: List[Any],
        *,
        command_template: Optional[Dict[str, Any]] = None,
        batch_size: Optional[int] = None,
    ) -> List[Any]:
        """Apply the same command to multiple inputs in parallel; returns unwrapped list."""
        ...

    def maybe(
        self,
        command: Any,
        data: Any,
        *,
        command_template: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
        """Execute command; return (data, error) tuple. No exception on failure."""
        ...
