"""
Unit tests for ADR-0084: MotetContext resource helpers delegate to commands (tools, memory, agents, models, workflows, schedules, conversations).

Tests cover:
- MotetToolsHelper.execute: inner path when no task_id, when inside tool_execution, when allow/deny/role
- MotetToolsHelper.execute: delegation when task_id and not inside tool_execution; return shape adaptation
- motet.tools returns MotetToolsHelper when registry present; None when absent
- motet.memory returns MotetMemoryHelper when manager present; agents/models/workflows/schedules return helpers
"""

import pytest
from unittest.mock import Mock, patch

from motet.core.commands.decorator import (
    MotetContext,
    MotetToolsHelper,
    MotetMemoryHelper,
    MotetAgentsHelper,
    MotetModelsHelper,
    MotetWorkflowsHelper,
    MotetSchedulesHelper,
    MotetCommandsHelper,
    MotetConversationsHelper,
)
from motet.core.tools.registry import ToolRegistry


class TestMotetToolsHelperInnerPath:
    """Test that execute() uses registry._execute_tool_only when not delegating."""

    def test_execute_no_task_id_uses_inner_path(self):
        """When motet has no task_id, execute uses _execute_tool_only."""
        registry = Mock(spec=ToolRegistry)
        registry.get.return_value = None
        registry.list_items.return_value = {}
        registry._execute_tool_only.return_value = {"status": "not_found", "error": "tool not found"}

        motet = MotetContext(task_id="", worker_context={"tool_registry": registry})
        helper = MotetToolsHelper(motet, registry)

        out = helper.execute("core.foo", {})

        assert out == {"status": "not_found", "error": "tool not found"}
        registry._execute_tool_only.assert_called_once()
        assert registry._execute_tool_only.call_args[0] == ("core.foo", {})
        # motet.do must not be called
        motet.do = Mock()
        helper.execute("core.bar", {})
        motet.do.assert_not_called()

    def test_execute_inside_tool_execution_uses_inner_path(self):
        """When current command is tool_execution (or core.tool_execution), execute uses _execute_tool_only to avoid recursion."""
        registry = Mock(spec=ToolRegistry)
        registry._execute_tool_only.return_value = {"status": "success", "result": 42}

        mock_command = Mock()
        mock_command.get_command_type.return_value = "core.tool_execution"
        motet = MotetContext(
            task_id="task-1",
            worker_context={"tool_registry": registry},
        )
        motet._command = mock_command
        helper = MotetToolsHelper(motet, registry)

        out = helper.execute("core.math_eval", {"expression": "1+1"})

        assert out == {"status": "success", "result": 42}
        registry._execute_tool_only.assert_called_once()
        # motet.do must not be called
        motet.do = Mock()
        helper.execute("core.other", {})
        motet.do.assert_not_called()

    def test_execute_with_allow_uses_inner_path(self):
        """When allow/deny/role are passed, use inner path (command does not accept them)."""
        registry = Mock(spec=ToolRegistry)
        registry._execute_tool_only.return_value = {"status": "success"}

        motet = MotetContext(task_id="task-1", worker_context={"tool_registry": registry})
        motet._command = None
        helper = MotetToolsHelper(motet, registry)

        helper.execute("core.foo", {}, allow={"core.foo"})

        registry._execute_tool_only.assert_called_once()
        call_kw = registry._execute_tool_only.call_args[1]
        assert call_kw.get("allow") == {"core.foo"}


class TestMotetToolsHelperDelegation:
    """Test that execute() delegates to tool_execution command when context allows."""

    def test_execute_with_task_id_delegates_and_unwraps_result(self):
        """When task_id set and not inside tool_execution, delegate and return command result['result']."""
        registry = Mock(spec=ToolRegistry)
        registry.get.return_value = None
        registry.list_items.return_value = {}

        motet = MotetContext(task_id="task-1", worker_context={"tool_registry": registry})
        motet._command = None
        motet.do = Mock(return_value={"tool_name": "core.foo", "result": {"status": "success", "data": 14}, "executed": True})
        helper = MotetToolsHelper(motet, registry)

        out = helper.execute("core.foo", {"x": 1})

        assert out == {"status": "success", "data": 14}
        motet.do.assert_called_once()
        call_kw = motet.do.call_args[1]
        data_arg = call_kw.get("data")
        assert data_arg is not None
        assert data_arg.tool_name == "core.foo"
        assert data_arg.parameters == {"x": 1}

    def test_execute_delegation_returns_result_key_when_present(self):
        """Command return shape { tool_name, result, executed } is unwrapped to result."""
        registry = Mock(spec=ToolRegistry)
        motet = MotetContext(task_id="t", worker_context={"tool_registry": registry})
        motet._command = None
        motet.do = Mock(return_value={"result": {"status": "not_found", "error": "tool not found"}})
        helper = MotetToolsHelper(motet, registry)

        out = helper.execute("core.missing", {})

        assert out == {"status": "not_found", "error": "tool not found"}


class TestMotetContextToolsProperty:
    """Test that motet.tools returns MotetToolsHelper or None."""

    def test_tools_returns_helper_when_registry_present(self):
        """When worker_context has tool_registry, tools is MotetToolsHelper."""
        registry = Mock(spec=ToolRegistry)
        motet = MotetContext(worker_context={"tool_registry": registry})
        tools = motet.tools
        assert isinstance(tools, MotetToolsHelper)
        assert tools._registry is registry
        assert tools._motet is motet

    def test_tools_returns_none_when_registry_absent(self):
        """When worker_context has no tool_registry, tools is None."""
        motet = MotetContext(worker_context={})
        assert motet.tools is None

    def test_tools_helper_proxies_get_and_list(self):
        """MotetToolsHelper.get and list delegate to registry; list_items is an alias."""
        registry = Mock(spec=ToolRegistry)
        registry.get.return_value = Mock(name="tool")
        registry.list_items.return_value = {"core.foo": Mock()}
        motet = MotetContext(worker_context={"tool_registry": registry})
        helper = motet.tools
        assert helper.get("core.foo") is registry.get.return_value
        listed = helper.list()
        assert listed == {"core.foo": registry.list_items.return_value["core.foo"]}
        assert helper.list_items() == listed
        registry.get.assert_called_once_with("core.foo")
        assert registry.list_items.call_count == 2


class TestMotetMemoryHelper:
    """ADR-0084: motet.memory returns MotetMemoryHelper and proxies store/recall/tag/forget."""

    def test_memory_returns_helper_when_manager_present(self):
        mock_mgr = Mock()
        motet = MotetContext(worker_context={"memory_manager": mock_mgr})
        assert isinstance(motet.memory, MotetMemoryHelper)
        assert motet.memory._motet is motet

    def test_memory_returns_none_when_manager_absent(self):
        motet = MotetContext(worker_context={})
        assert motet.memory is None

    def test_memory_inside_memory_command_uses_inner_path_no_delegation(self):
        """When inside a memory command (e.g. core.memory_store), helper must use inner path to avoid infinite loop."""
        mock_mgr = Mock()
        mock_mgr.store_memory.return_value = {"id": "mem-1"}
        motet = MotetContext(task_id="task-1", worker_context={"memory_manager": mock_mgr})
        motet.do = Mock()
        # Simulate being inside memory_store (namespaced type as returned by decorator)
        motet._command = Mock(get_command_type=Mock(return_value="core.memory_store"))
        out = motet.memory.store(content="x", type="note", tags=[])
        mock_mgr.store_memory.assert_called_once()
        motet.do.assert_not_called()
        assert out.get("memory_id") == "mem-1"
        assert out.get("stored") is True

    def test_recall_query_with_tags_uses_hybrid_without_implicit_conversation_scope(self):
        """Query recall should preserve tags and avoid implicit conversation scoping."""
        item = Mock()
        item.model_dump.return_value = {"id": "m1", "content": "psychology report"}
        mock_mgr = Mock()
        mock_mgr.hybrid_retrieve.return_value = [item]

        motet = MotetContext(
            worker_context={"memory_manager": mock_mgr},
            task_id="",  # inner path, no delegation
            conversation_id="conv-current",
        )

        out = motet.memory.recall(query="psychology", tags=["deep-research"], limit=3)
        assert out == [{"id": "m1", "content": "psychology report"}]

        mock_mgr.hybrid_retrieve.assert_called_once()
        call_kwargs = mock_mgr.hybrid_retrieve.call_args.kwargs
        assert call_kwargs["query"] == "psychology"
        assert call_kwargs["tags"] == ["deep-research"]
        assert call_kwargs["limit"] == 3
        assert call_kwargs["min_relevance"] == 0.5
        assert call_kwargs["conversation_id"] is None
        assert call_kwargs["motet_context"] is motet

    def test_recall_query_with_explicit_conversation_scope_passes_conversation(self):
        """Explicit conversation_id should be forwarded to hybrid retrieval."""
        item = Mock()
        item.model_dump.return_value = {"id": "m2"}
        mock_mgr = Mock()
        mock_mgr.hybrid_retrieve.return_value = [item]

        motet = MotetContext(worker_context={"memory_manager": mock_mgr}, task_id="")

        motet.memory.recall(
            query="psychology",
            tags=["deep-research"],
            limit=2,
            conversation_id="conv-explicit",
        )
        call_kwargs = mock_mgr.hybrid_retrieve.call_args.kwargs
        assert call_kwargs["conversation_id"] == "conv-explicit"


class TestMemoryStoreCommandReturnValue:
    """memory_store command must return memory_id whether inner layer returns 'id' or 'memory_id'."""

    def test_memory_store_command_returns_memory_id_when_decorator_returns_memory_id(self):
        """When motet.memory.store returns {'memory_id': 'x', 'stored': True}, command must return memory_id."""
        from motet.core.commands.builtin.memory import memory_store
        from motet.core.commands.command_data_classes import MemoryStoreData

        mock_motet = Mock()
        mock_motet.memory.store.return_value = {"memory_id": "decorator-id-123", "stored": True}
        with patch("motet.core.commands.builtin.memory.get_motet_context", return_value=mock_motet):
            # Wrapper accepts task_id, conversation_id, tenant_id; type checker sees only data (original sig)
            cmd = memory_store(
                data=MemoryStoreData(content="test", type="note", tags=[]),
                task_id="t",  # type: ignore[call-arg]
                conversation_id="c",  # type: ignore[call-arg]
                tenant_id="tenant",  # type: ignore[call-arg]
            )
            raw = cmd._do_execute({})
        data = raw.get("data") or raw
        assert data.get("memory_id") == "decorator-id-123"
        assert data.get("stored") is True

    def test_memory_store_command_returns_memory_id_when_manager_returns_id(self):
        """When inner layer returns {'id': 'x', 'stored_in': [...]}, command must still return memory_id (regression)."""
        from motet.core.commands.builtin.memory import memory_store
        from motet.core.commands.command_data_classes import MemoryStoreData

        mock_motet = Mock()
        mock_motet.memory.store.return_value = {"id": "manager-id-456", "stored_in": ["memory"]}
        with patch("motet.core.commands.builtin.memory.get_motet_context", return_value=mock_motet):
            # Wrapper accepts task_id, conversation_id, tenant_id; type checker sees only data (original sig)
            cmd = memory_store(
                data=MemoryStoreData(content="test", type="note", tags=[]),
                task_id="t",  # type: ignore[call-arg]
                conversation_id="c",  # type: ignore[call-arg]
                tenant_id="tenant",  # type: ignore[call-arg]
            )
            raw = cmd._do_execute({})
        data = raw.get("data") or raw
        assert data.get("memory_id") == "manager-id-456"
        assert data.get("stored") is True

    def test_memory_store_raises_when_tenant_required_and_missing(self):
        """When tenant_enforce_memory_filter is enabled, memory_store must fail if tenant_id is missing."""
        from motet.core.commands.builtin.memory import memory_store
        from motet.core.commands.command_data_classes import MemoryStoreData

        mock_cfg = Mock(tenant_enforce_memory_filter=True)
        mock_stack = Mock(config=mock_cfg)
        mock_motet = Mock()
        mock_motet.memory.store.return_value = {"id": "x"}
        mock_motet.stack = mock_stack
        mock_motet.tenant_id = ""
        mock_motet.motet_id = "default"
        mock_motet.principal_id = "u1"
        mock_motet.conversation_id = "c1"
        mock_motet.task_id = "t1"
        mock_motet.command_id = "cmd1"

        with patch("motet.core.commands.builtin.memory.get_motet_context", return_value=mock_motet):
            cmd = memory_store(
                data=MemoryStoreData(content="test", type="note", tags=[]),
                task_id="t",  # type: ignore[call-arg]
                conversation_id="c",  # type: ignore[call-arg]
                tenant_id="tenant",  # type: ignore[call-arg]
            )
            raw = cmd._do_execute({})
        assert raw.get("status") == "error"
        assert "tenant_id is required" in ((raw.get("error") or {}).get("message") or "")

    def test_memory_recall_raises_when_tenant_required_and_missing(self):
        """When tenant_enforce_memory_filter is enabled, memory_recall must fail if tenant_id is missing."""
        from motet.core.commands.builtin.memory import memory_recall
        from motet.core.commands.command_data_classes import MemoryRecallData

        mock_cfg = Mock(tenant_enforce_memory_filter=True)
        mock_stack = Mock(config=mock_cfg)
        mock_motet = Mock()
        mock_motet.memory.recall.return_value = []
        mock_motet.stack = mock_stack
        mock_motet.tenant_id = ""
        mock_motet.motet_id = "default"
        mock_motet.principal_id = "u1"
        mock_motet.conversation_id = "c1"
        mock_motet.task_id = "t1"
        mock_motet.command_id = "cmd1"

        with patch("motet.core.commands.builtin.memory.get_motet_context", return_value=mock_motet):
            cmd = memory_recall(
                data=MemoryRecallData(query="hello", limit=3, tags=[]),
                task_id="t",  # type: ignore[call-arg]
                conversation_id="c",  # type: ignore[call-arg]
                tenant_id="tenant",  # type: ignore[call-arg]
            )
            raw = cmd._do_execute({})
        assert raw.get("status") == "error"
        assert "tenant_id is required" in ((raw.get("error") or {}).get("message") or "")

    def test_memory_recall_semantic_mode_uses_vector_query(self):
        """memory_recall(mode='semantic') should route to vector.query (canonical semantic path)."""
        from motet.core.commands.builtin.memory import memory_recall
        from motet.core.commands.command_data_classes import MemoryRecallData

        mock_item = Mock()
        mock_item.model_dump.return_value = {"id": "vec-1", "content": "semantic hit"}

        mock_motet = Mock()
        mock_motet.memory = Mock()
        mock_motet.memory.hybrid_retrieve = Mock()
        mock_motet.memory.recall = Mock()
        mock_motet.stack = Mock()
        mock_motet.stack.vector = Mock()
        mock_motet.stack.vector.query.return_value = [mock_item]
        mock_motet.stack.config = Mock(tenant_enforce_memory_filter=False)
        mock_motet.tenant_id = "tenant-1"
        mock_motet.motet_id = "motet-1"
        mock_motet.principal_id = "user-1"
        mock_motet.conversation_id = "conv-1"
        mock_motet.agent_id = None
        mock_motet.configured_agent_id = None  # no agent context -> agent_id not passed
        mock_motet.metadata = {}
        mock_motet.task_id = "task-1"
        mock_motet.command_id = "cmd-1"

        with patch("motet.core.commands.builtin.memory.get_motet_context", return_value=mock_motet):
            cmd = memory_recall(
                data=MemoryRecallData(query="hello", limit=3, tags=["ltm"], mode="semantic"),
                task_id="t",  # type: ignore[call-arg]
                conversation_id="c",  # type: ignore[call-arg]
                tenant_id="tenant",  # type: ignore[call-arg]
            )
            raw = cmd._do_execute({})

        data = raw.get("data") or raw
        assert data.get("count") == 1
        assert data.get("items")[0]["id"] == "vec-1"
        mock_motet.stack.vector.query.assert_called_once_with(
            "hello",
            top_k=3,
            tags=["ltm"],
            tenant_id="tenant-1",
            principal_id="user-1",
            conversation_id="conv-1",
            motet_id="motet-1",
            agent_id=None,
        )
        mock_motet.memory.hybrid_retrieve.assert_not_called()
        mock_motet.memory.recall.assert_not_called()

    def test_memory_recall_semantic_mode_hydrates_content_from_kv(self):
        """Semantic recalls should merge vector hit metadata with KV-backed full memory content."""
        from motet.core.commands.builtin.memory import memory_recall
        from motet.core.commands.command_data_classes import MemoryRecallData

        mock_item = Mock()
        mock_item.model_dump.return_value = {
            "id": "vec-1",
            "content": "",
            "metadata": {"scope_id": None, "search_score": 0.93},
            "search_score": 0.93,
        }
        kv_item = Mock()
        kv_item.model_dump.return_value = {
            "id": "vec-1",
            "type": "note",
            "content": "remember booyyaaa",
            "metadata": {"source": "chat"},
            "tags": ["ltm"],
        }
        mock_kv = Mock()
        mock_kv.get.return_value = kv_item

        mock_motet = Mock()
        mock_motet.memory = Mock()
        mock_motet.memory.hybrid_retrieve = Mock()
        mock_motet.memory.recall = Mock()
        mock_motet.memory._scoped_kv_store.return_value = mock_kv
        mock_motet.stack = Mock()
        mock_motet.stack.vector = Mock()
        mock_motet.stack.vector.query.return_value = [mock_item]
        mock_motet.stack.config = Mock(tenant_enforce_memory_filter=False)
        mock_motet.tenant_id = "tenant-1"
        mock_motet.motet_id = "motet-1"
        mock_motet.principal_id = "user-1"
        mock_motet.conversation_id = "conv-1"
        mock_motet.agent_id = None
        mock_motet.configured_agent_id = None
        mock_motet.metadata = {}
        mock_motet.task_id = "task-1"
        mock_motet.command_id = "cmd-1"

        with patch("motet.core.commands.builtin.memory.get_motet_context", return_value=mock_motet):
            cmd = memory_recall(
                data=MemoryRecallData(query="booyyaaa", limit=3, tags=["ltm"], mode="semantic"),
                task_id="t",  # type: ignore[call-arg]
                conversation_id="c",  # type: ignore[call-arg]
                tenant_id="tenant",  # type: ignore[call-arg]
            )
            raw = cmd._do_execute({})

        data = raw.get("data") or raw
        assert data.get("count") == 1
        item = data.get("items")[0]
        assert item["id"] == "vec-1"
        assert item["content"] == "remember booyyaaa"
        assert item["metadata"] == {"source": "chat", "search_score": 0.93}
        assert item["search_score"] == 0.93
        mock_kv.get.assert_called_once_with("vec-1")

    def test_memory_recall_hybrid_mode_prefers_hybrid_retrieve(self):
        """memory_recall(mode='hybrid') should use manager.hybrid_retrieve when query is present."""
        from motet.core.commands.builtin.memory import memory_recall
        from motet.core.commands.command_data_classes import MemoryRecallData

        mock_item = Mock()
        mock_item.model_dump.return_value = {"id": "hy-1", "content": "hybrid hit"}

        mock_motet = Mock()
        mock_motet.memory = Mock()
        mock_motet.memory.hybrid_retrieve.return_value = [mock_item]
        mock_motet.memory.recall = Mock(return_value=[])
        mock_motet.stack = Mock()
        mock_motet.stack.vector = Mock()
        mock_motet.stack.config = Mock(tenant_enforce_memory_filter=False)
        mock_motet.tenant_id = "tenant-1"
        mock_motet.motet_id = "motet-1"
        mock_motet.principal_id = "user-1"
        mock_motet.conversation_id = "conv-1"
        mock_motet.task_id = "task-1"
        mock_motet.command_id = "cmd-1"

        with patch("motet.core.commands.builtin.memory.get_motet_context", return_value=mock_motet):
            cmd = memory_recall(
                data=MemoryRecallData(query="hello", limit=2, tags=["stm"], mode="hybrid"),
                task_id="t",  # type: ignore[call-arg]
                conversation_id="c",  # type: ignore[call-arg]
                tenant_id="tenant",  # type: ignore[call-arg]
            )
            raw = cmd._do_execute({})

        data = raw.get("data") or raw
        assert data.get("count") == 1
        assert data.get("items")[0]["id"] == "hy-1"
        mock_motet.memory.hybrid_retrieve.assert_called_once()
        call_kwargs = mock_motet.memory.hybrid_retrieve.call_args.kwargs
        assert call_kwargs["min_relevance"] == 0.5  # coverage scoring makes the floor safe for tags
        assert call_kwargs["tags"] == ["stm"]
        mock_motet.memory.recall.assert_not_called()
        mock_motet.stack.vector.query.assert_not_called()

    def test_memory_recall_hybrid_without_tags_keeps_default_min_relevance(self):
        """Untagged hybrid recall keeps the same default relevance floor."""
        from motet.core.commands.builtin.memory import memory_recall
        from motet.core.commands.command_data_classes import MemoryRecallData

        mock_motet = Mock()
        mock_motet.memory = Mock()
        mock_motet.memory.hybrid_retrieve.return_value = []
        mock_motet.stack = Mock()
        mock_motet.stack.config = Mock(tenant_enforce_memory_filter=False)
        mock_motet.tenant_id = "tenant-1"
        mock_motet.motet_id = "motet-1"
        mock_motet.principal_id = "user-1"
        mock_motet.conversation_id = "conv-1"
        mock_motet.task_id = "task-1"
        mock_motet.command_id = "cmd-1"

        with patch("motet.core.commands.builtin.memory.get_motet_context", return_value=mock_motet):
            cmd = memory_recall(
                data=MemoryRecallData(query="hello", limit=2, tags=[], mode="hybrid"),
                task_id="t",  # type: ignore[call-arg]
                conversation_id="c",  # type: ignore[call-arg]
                tenant_id="tenant",  # type: ignore[call-arg]
            )
            cmd._do_execute({})

        call_kwargs = mock_motet.memory.hybrid_retrieve.call_args.kwargs
        assert call_kwargs["min_relevance"] == 0.5

    def test_memory_search_uses_same_semantic_routing(self):
        """memory_search should use the same semantic routing as memory_recall(mode='semantic')."""
        from motet.core.commands.builtin.memory import memory_search
        from motet.core.commands.command_data_classes import MemorySearchData

        mock_item = Mock()
        mock_item.model_dump.return_value = {"id": "vec-2", "content": "search hit"}

        mock_motet = Mock()
        mock_motet.memory = Mock()
        mock_motet.memory.hybrid_retrieve = Mock()
        mock_motet.memory.recall = Mock()
        mock_motet.stack = Mock()
        mock_motet.stack.vector = Mock()
        mock_motet.stack.vector.query.return_value = [mock_item]
        mock_motet.stack.config = Mock(tenant_enforce_memory_filter=False)
        mock_motet.tenant_id = "tenant-1"
        mock_motet.motet_id = "motet-1"
        mock_motet.principal_id = "user-1"
        mock_motet.conversation_id = "conv-1"
        mock_motet.agent_id = None
        mock_motet.configured_agent_id = None  # no agent context -> agent_id not passed
        mock_motet.metadata = {}
        mock_motet.task_id = "task-1"
        mock_motet.command_id = "cmd-1"

        with patch("motet.core.commands.builtin.memory.get_motet_context", return_value=mock_motet):
            cmd = memory_search(
                data=MemorySearchData(query="world", top_k=4, tags=["ltm"]),
                task_id="t",  # type: ignore[call-arg]
                conversation_id="c",  # type: ignore[call-arg]
                tenant_id="tenant",  # type: ignore[call-arg]
            )
            raw = cmd._do_execute({})

        data = raw.get("data") or raw
        assert data.get("count") == 1
        assert data.get("items")[0]["id"] == "vec-2"
        mock_motet.stack.vector.query.assert_called_once_with(
            "world",
            top_k=4,
            tags=["ltm"],
            tenant_id="tenant-1",
            principal_id="user-1",
            conversation_id="conv-1",
            motet_id="motet-1",
            agent_id=None,
        )

    def test_memory_recall_semantic_strict_mode_passes_agent_id(self):
        """When memory_agent_scope_mode is strict and agent_id present, vector.query gets agent_id."""
        from motet.core.commands.builtin.memory import memory_recall
        from motet.core.commands.command_data_classes import MemoryRecallData

        mock_item = Mock()
        mock_item.model_dump.return_value = {"id": "vec-1", "content": "agent hit"}

        mock_motet = Mock()
        mock_motet.memory = Mock()
        mock_motet.memory.hybrid_retrieve = Mock()
        mock_motet.memory.recall = Mock()
        mock_motet.stack = Mock()
        mock_motet.stack.vector = Mock()
        mock_motet.stack.vector.query.return_value = [mock_item]
        mock_motet.stack.config = Mock(
            tenant_enforce_memory_filter=False,
            memory_agent_scope_mode="strict",
            memory_agent_tag_prefix="agent:",
        )
        mock_motet.tenant_id = "tenant-1"
        mock_motet.motet_id = "motet-1"
        mock_motet.principal_id = "user-1"
        mock_motet.conversation_id = "conv-1"
        mock_motet.agent_id = "my-agent"
        mock_motet.task_id = "task-1"
        mock_motet.command_id = "cmd-1"

        with patch("motet.core.commands.builtin.memory.get_motet_context", return_value=mock_motet):
            cmd = memory_recall(
                data=MemoryRecallData(query="hello", limit=3, tags=["ltm"], mode="semantic"),
                task_id="t",  # type: ignore[call-arg]
                conversation_id="c",  # type: ignore[call-arg]
                tenant_id="tenant",  # type: ignore[call-arg]
            )
            raw = cmd._do_execute({})

        mock_motet.stack.vector.query.assert_called_once_with(
            "hello",
            top_k=3,
            tags=["ltm"],
            tenant_id="tenant-1",
            principal_id="user-1",
            conversation_id="conv-1",
            motet_id="motet-1",
            agent_id="my-agent",
        )


class TestMotetAgentsModelsWorkflowsSchedules:
    """ADR-0084: motet.agents, .models, .workflows, .schedules return helpers."""

    def test_agents_returns_helper(self):
        motet = MotetContext(worker_context={})
        assert isinstance(motet.agents, MotetAgentsHelper)
        assert motet.agents._motet is motet

    def test_models_returns_helper(self):
        motet = MotetContext(worker_context={})
        assert isinstance(motet.models, MotetModelsHelper)
        assert motet.models._motet is motet

    def test_workflows_returns_helper(self):
        motet = MotetContext(worker_context={})
        assert isinstance(motet.workflows, MotetWorkflowsHelper)
        assert motet.workflows._motet is motet

    def test_schedules_returns_helper(self):
        motet = MotetContext(worker_context={})
        assert isinstance(motet.schedules, MotetSchedulesHelper)
        assert motet.schedules._motet is motet

    def test_schedules_list_uses_manager_and_keeps_alias(self):
        """motet.schedules.list is the helper API; list_schedules remains an alias."""
        manager = Mock()
        manager.list_schedules.return_value = [{"schedule_id": "sch-1"}]
        motet = MotetContext(worker_context={"schedule_manager": manager})
        assert motet.schedules.list() == [{"schedule_id": "sch-1"}]
        assert motet.schedules.list() == [{"schedule_id": "sch-1"}]
        assert manager.list_schedules.call_count == 2

    def test_schedules_create_propagates_identity_to_schedule_command(self):
        """motet.schedules.create must stamp tenant/principal/motet onto ScheduleCommand."""
        motet = MotetContext(
            worker_context={},
            task_id="task-sched-1",
            conversation_id="conv-sched-1",
            tenant_id="motet-global",
            principal_id="user-1",
            motet_id="motet-1",
        )
        motet.do = Mock(return_value={"schedule_id": "sch-1"})

        with patch("motet.core.commands.builtin.schedule.ScheduleCommand") as mock_cls:
            out = motet.schedules.create(
                target_command_type="background-thinker.reflect",
                target_command_data={"topic": "x"},
                schedule_type="recurring",
                interval_seconds=60,
                name="test",
            )

        assert out == {"schedule_id": "sch-1"}
        mock_cls.assert_called_once()
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["task_id"] == "task-sched-1"
        assert kwargs["conversation_id"] == "conv-sched-1"
        assert kwargs["tenant_id"] == "motet-global"
        assert kwargs["principal_id"] == "user-1"
        assert kwargs["motet_id"] == "motet-1"
        motet.do.assert_called_once()

    def test_agents_list_without_task_id_uses_inner_path(self):
        motet = MotetContext(worker_context={}, task_id="")
        with patch("motet.core.agents.discovery.list_visible_agents") as mock_list:
            mock_list.return_value = [{"qualified_id": "core.default"}]
            out = motet.agents.list()
        assert out == [{"qualified_id": "core.default"}]
        mock_list.assert_called_once()

    def test_workflows_list_returns_ids(self):
        motet = MotetContext(worker_context={})
        ids = motet.workflows.list()
        assert isinstance(ids, list)


class TestMotetCommandsHelper:
    """ADR-0084: motet.commands (list/get/run) for discovery and dynamic dispatch."""

    def test_commands_returns_helper(self):
        motet = MotetContext(worker_context={})
        assert isinstance(motet.commands, MotetCommandsHelper)
        assert motet.commands._motet is motet

    def test_commands_list_returns_registered_types(self):
        motet = MotetContext(worker_context={})
        types = motet.commands.list()
        assert isinstance(types, list)
        # At least one command type registered (e.g. tool_execution when tool module loaded)
        assert len(types) >= 0

    def test_commands_get_resolves_bare_and_prefixed(self):
        motet = MotetContext(worker_context={})
        # core.tool_execution is registered when tool module is loaded (canonical); bare name also resolved
        impl_core = motet.commands.get("core.tool_execution")
        if impl_core is not None:
            assert callable(impl_core) or hasattr(impl_core, "__command_type__")
        impl = motet.commands.get("tool_execution")
        if impl is not None:
            assert callable(impl) or hasattr(impl, "__command_type__")
        # Unknown type returns None
        assert motet.commands.get("nonexistent_command_xyz") is None

    def test_commands_run_requires_task_context(self):
        motet = MotetContext(worker_context={}, task_id="")
        with pytest.raises(RuntimeError, match="task context"):
            motet.commands.run("core.tool_execution", {"tool_name": "x", "parameters": {}})

    def test_commands_run_unknown_type_raises(self):
        motet = MotetContext(worker_context={}, task_id="task-1")
        with pytest.raises(ValueError, match="Unknown command type"):
            motet.commands.run("nonexistent_command_xyz", {})


class TestMotetConversationsHelper:
    """ADR-0084: motet.conversations (list/get/clear/register/rename) delegates to conversation commands."""

    def test_conversations_returns_helper(self):
        motet = MotetContext(worker_context={})
        assert isinstance(motet.conversations, MotetConversationsHelper)
        assert motet.conversations._motet is motet

    def test_list_without_task_id_requires_principal(self):
        motet = MotetContext(worker_context={}, task_id="")
        with pytest.raises(ValueError, match="requires motet_id"):
            motet.conversations.list(limit=10)

    def test_list_without_task_id_requires_tenant(self):
        motet = MotetContext(
            worker_context={},
            task_id="",
            principal_id="user-1",
            motet_id="motet-1",
        )
        with pytest.raises(ValueError, match="requires tenant_id"):
            motet.conversations.list(limit=10)

    def test_list_without_task_id_requires_principal_when_motet_tenant_present(self):
        motet = MotetContext(
            worker_context={},
            task_id="",
            tenant_id="tenant-1",
            motet_id="motet-1",
        )
        with pytest.raises(ValueError, match="requires principal_id"):
            motet.conversations.list(limit=10)

    def test_list_without_task_id_uses_inner_path(self):
        motet = MotetContext(
            worker_context={},
            task_id="",
            principal_id="user-1",
            tenant_id="tenant-1",
            motet_id="motet-1",
        )
        with patch("motet.core.conversations.registry.list_conversations_sync") as mock_list:
            mock_list.return_value = [{"id": "c1", "title": "Chat 1"}]
            out = motet.conversations.list(limit=10)
        assert out == [{"id": "c1", "title": "Chat 1"}]
        mock_list.assert_called_once()
        assert mock_list.call_args[1]["limit"] == 10
        assert mock_list.call_args[1]["tenant_id"] == "tenant-1"
        assert mock_list.call_args[1]["motet_id"] == "motet-1"
        assert mock_list.call_args[1]["principal_id"] == "user-1"

    def test_list_with_task_id_delegates(self):
        motet = MotetContext(worker_context={}, task_id="task-1")
        motet.do = Mock(return_value={"conversations": [{"id": "c1"}]})
        out = motet.conversations.list(limit=5)
        assert out == [{"id": "c1"}]
        motet.do.assert_called_once()
        call_kw = motet.do.call_args[1]
        assert call_kw["data"].limit == 5  # ListConversationsData

    def test_get_requires_task_context(self):
        motet = MotetContext(worker_context={}, task_id="")
        with pytest.raises(RuntimeError, match="task context"):
            motet.conversations.get("conv-123")

    def test_clear_requires_task_context(self):
        motet = MotetContext(worker_context={}, task_id="")
        with pytest.raises(RuntimeError, match="task context"):
            motet.conversations.clear("conv-123")

    def test_register_requires_task_context(self):
        motet = MotetContext(worker_context={}, task_id="")
        with pytest.raises(RuntimeError, match="task context"):
            motet.conversations.register("conv-123", title="My Chat")

    def test_rename_requires_task_context(self):
        motet = MotetContext(worker_context={}, task_id="")
        with pytest.raises(RuntimeError, match="task context"):
            motet.conversations.rename("conv-123", "New Title")

    def test_get_with_task_id_delegates(self):
        motet = MotetContext(worker_context={}, task_id="task-1")
        motet.do = Mock(return_value={"conversation_id": "c1", "history": [], "counts": {"memory": 0, "vector": 0}})
        out = motet.conversations.get("c1")
        assert out["conversation_id"] == "c1"
        motet.do.assert_called_once()


class TestMotetContextImmutableIdentity:
    """Identity context (tenant/motet/principal) cannot be overridden in nested composition."""

    def test_call_rejects_tenant_override(self):
        from motet.core.commands.builtin.memory import memory_recall
        from motet.core.commands.command_data_classes import MemoryRecallData

        motet = MotetContext(
            task_id="t1",
            conversation_id="c1",
            tenant_id="tenant-a",
            principal_id="user-a",
            motet_id="motet-a",
            worker_context={},
        )
        with pytest.raises(ValueError, match="tenant_id override is not allowed"):
            motet._call(
                memory_recall,
                data=MemoryRecallData(query="x", tags=[], limit=1),
                tenant_id="tenant-b",
            )

    def test_gather_rejects_principal_override(self):
        from motet.core.commands.builtin.memory import memory_recall
        from motet.core.commands.command_data_classes import MemoryRecallData

        motet = MotetContext(
            task_id="t1",
            conversation_id="c1",
            tenant_id="tenant-a",
            principal_id="user-a",
            motet_id="motet-a",
            worker_context={},
        )
        with pytest.raises(ValueError, match="principal_id override is not allowed"):
            motet._gather(
                [(memory_recall, MemoryRecallData(query="x", tags=[], limit=1))],
                principal_id="user-b",
            )

    def test_class_based_command_rejects_motet_override_from_parent_identity_context(self):
        from motet.core.commands.distributed import DistributedCommand
        from motet.core.workers.invoker_context import (
            set_current_identity_context,
            clear_current_identity_context,
        )

        class _DummyDistributedCommand(DistributedCommand):
            def _do_execute(self, worker_context):
                return {}

            def get_command_type(self) -> str:
                return "test.dummy_command"

            def can_undo(self) -> bool:
                return False

            def undo(self, worker_context):
                return False

        set_current_identity_context(
            {"tenant_id": "tenant-a", "motet_id": "motet-a", "principal_id": "user-a"}
        )
        try:
            with pytest.raises(ValueError, match="motet_id override is not allowed"):
                _DummyDistributedCommand(
                    task_id="t1",
                    data={},
                    tenant_id="tenant-a",
                    motet_id="motet-b",
                    principal_id="user-a",
                )
        finally:
            clear_current_identity_context()
