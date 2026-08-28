"""
Motet SDK - Mock MotetContext for unit testing bundles.

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Use MockMotetContext when testing bundle commands without a running Motet
runtime.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


class MockMotetContext:
    """
    Test double for MotetContext.

    Use in unit tests for bundle commands. Inject the resource helpers your
    command touches, or patch methods as needed:

        motet = MockMotetContext(
            models=Mock(infer=Mock(return_value={"content": "..."})),
            tools=Mock(execute=Mock(return_value={"main_content": "..."})),
        )
        result = my_command(MyData(topic="x"), motet)

    Resource helpers left as None stay None, so a command that reaches for one
    it was not given fails loudly in the test rather than silently no-opping.
    """

    def __init__(
        self,
        task_id: str = "test-task",
        conversation_id: str = "test-conv",
        command_id: str = "test-cmd",
        tenant_id: str = "test-tenant",
        principal_id: str = "test-principal",
        motet_id: str = "test-motet",
        metadata: Optional[Dict[str, Any]] = None,
        stream_key: str = "task:test-task:response",
        redis: Any = None,
        stack: Any = None,
        memory: Any = None,
        tools: Any = None,
        models: Any = None,
        agents: Any = None,
        workflows: Any = None,
        schedules: Any = None,
        commands: Any = None,
        conversations: Any = None,
        vault: Any = None,
        event_bus: Any = None,
        artifact_store: Any = None,
        distributed_context: Any = None,
    ):
        self._task_id = task_id
        self._conversation_id = conversation_id
        self._command_id = command_id
        self._tenant_id = tenant_id
        self._principal_id = principal_id
        self._motet_id = motet_id
        self._metadata = metadata or {}
        self._stream_key = stream_key
        self._redis = redis
        self._stack = stack
        self._memory = memory
        self._tools = tools
        self._models = models
        self._agents = agents
        self._workflows = workflows
        self._schedules = schedules
        self._commands = commands
        self._conversations = conversations
        self._vault = vault
        self._event_bus = event_bus
        self._artifact_store = artifact_store
        self._distributed_context = distributed_context

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def conversation_id(self) -> str:
        return self._conversation_id

    @property
    def command_id(self) -> str:
        return self._command_id

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    @property
    def principal_id(self) -> str:
        return self._principal_id

    @property
    def motet_id(self) -> str:
        return self._motet_id

    @property
    def metadata(self) -> Dict[str, Any]:
        return self._metadata

    @property
    def stream_key(self) -> str:
        return self._stream_key

    @property
    def redis(self) -> Any:
        return self._redis

    @property
    def stack(self) -> Any:
        return self._stack

    @property
    def memory(self) -> Any:
        return self._memory

    @property
    def tools(self) -> Any:
        return self._tools

    @property
    def models(self) -> Any:
        return self._models

    @property
    def agents(self) -> Any:
        return self._agents

    @property
    def workflows(self) -> Any:
        return self._workflows

    @property
    def schedules(self) -> Any:
        return self._schedules

    @property
    def commands(self) -> Any:
        return self._commands

    @property
    def conversations(self) -> Any:
        return self._conversations

    @property
    def vault(self) -> Any:
        return self._vault

    @property
    def event_bus(self) -> Any:
        return self._event_bus

    @property
    def artifact_store(self) -> Any:
        return self._artifact_store

    @property
    def distributed_context(self) -> Any:
        return self._distributed_context

    def resolve_conversation_id(self, explicit_id: Optional[str] = None) -> str:
        return explicit_id or self._conversation_id or ""

    def log_fields(self, **extra: Any) -> Dict[str, Any]:
        return {
            "tenant_id": self._tenant_id,
            "principal_id": self._principal_id,
            "motet_id": self._motet_id,
            "task_id": self._task_id,
            "command_id": self._command_id,
            **extra,
        }

    def observe_events(
        self,
        event_types: Any,
        callback: Any,
        priority: Optional[int] = None,
        custom_filter: Optional[Any] = None,
    ) -> Any:
        from contextlib import nullcontext
        return nullcontext()

    def ensure_stream(self, ttl_seconds: int = 3600, stream_key: Optional[str] = None) -> None:
        pass

    def stream_event(self, event_type: str, stream_key: Optional[str] = None, **fields: Any) -> None:
        pass

    def stream_token(self, token: str, stream_key: Optional[str] = None) -> None:
        pass

    def publish_event(self, event: Dict[str, Any]) -> None:
        pass

    def add_warning(self, message: str) -> None:
        pass

    @property
    def last_metadata(self) -> Optional[Any]:
        return None

    def dispatch(self, commands: List[Any], max_parallel: Optional[int] = None, **kwargs: Any) -> List[str]:
        raise NotImplementedError("MockMotetContext.dispatch() — stub in tests")

    def do(
        self,
        command: Any,
        data: Any,
        *,
        command_template: Optional[Dict[str, Any]] = None,
    ) -> Any:
        raise NotImplementedError("MockMotetContext.do() — stub in tests")

    def join(
        self,
        command_data_pairs: List[Tuple[Any, Any]],
        *,
        raise_on_first_error: bool = True,
    ) -> List[Any]:
        raise NotImplementedError("MockMotetContext.join() — stub in tests")

    def apply(
        self,
        command: Any,
        inputs: List[Any],
        *,
        command_template: Optional[Dict[str, Any]] = None,
        batch_size: Optional[int] = None,
    ) -> List[Any]:
        raise NotImplementedError("MockMotetContext.apply() — stub in tests")

    def maybe(
        self,
        command: Any,
        data: Any,
        *,
        command_template: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
        return None, {"message": "MockMotetContext.maybe() — stub in tests"}
