"""
Motet - Memory long_term Flag Threading Tests

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-21

Description:
Tests that the ``long_term`` parameter flows end-to-end from
``motet.memory.store(long_term=True)`` through the delegation chain
(MotetMemoryHelper -> MemoryStoreData -> memory_store command -> MemoryManager)
so that custom memory types can opt into LTM vector indexing (ADR-0092).

Dependencies:
- motet.core.commands.command_data_classes (MemoryStoreData)
- motet.core.commands.builtin.memory (memory_store command)
- motet.core.commands.decorator (MotetMemoryHelper, MotetContext)
- motet.core.memory.manager (MemoryManager)

Usage:
    pytest tests/unit/core/memory/test_memory_long_term_flag.py -v

Notes:
- Covers the data-class, command, helper-delegation, helper-direct, and manager layers.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from motet.core.commands.command_data_classes import MemoryStoreData


# ---------------------------------------------------------------------------
# 1. MemoryStoreData serialisation
# ---------------------------------------------------------------------------

class TestMemoryStoreDataLongTerm:
    """MemoryStoreData must carry the long_term flag through serialisation."""

    def test_long_term_defaults_to_none(self):
        data = MemoryStoreData(content="x")
        assert data.long_term is None

    def test_long_term_true_round_trips(self):
        data = MemoryStoreData(content="x", long_term=True)
        assert data.long_term is True
        dumped = data.model_dump()
        assert dumped["long_term"] is True
        restored = MemoryStoreData.model_validate(dumped)
        assert restored.long_term is True

    def test_long_term_false_round_trips(self):
        data = MemoryStoreData(content="x", long_term=False)
        assert data.long_term is False


# ---------------------------------------------------------------------------
# 2. memory_store command passes long_term to manager
# ---------------------------------------------------------------------------

class TestMemoryStoreCommandLongTerm:
    """The memory_store command must forward long_term to the memory helper."""

    def test_long_term_forwarded_to_store_memory(self):
        from motet.core.commands.builtin.memory import memory_store

        mock_motet = Mock()
        mock_motet.memory.store.return_value = {"id": "m1", "stored_in": ["memory", "vector_pending"]}

        with patch("motet.core.commands.builtin.memory.get_motet_context", return_value=mock_motet):
            cmd = memory_store(
                data=MemoryStoreData(content="panel insight", type="panel_analysis", long_term=True),
                task_id="t",  # type: ignore[call-arg]
                conversation_id="c",  # type: ignore[call-arg]
                tenant_id="tenant",  # type: ignore[call-arg]
            )
            raw = cmd._do_execute({})

        call_kwargs = mock_motet.memory.store.call_args
        assert call_kwargs.kwargs.get("long_term") is True

    def test_long_term_none_when_omitted(self):
        from motet.core.commands.builtin.memory import memory_store

        mock_motet = Mock()
        mock_motet.memory.store.return_value = {"id": "m2", "stored_in": ["memory"]}

        with patch("motet.core.commands.builtin.memory.get_motet_context", return_value=mock_motet):
            cmd = memory_store(
                data=MemoryStoreData(content="note"),
                task_id="t",  # type: ignore[call-arg]
                conversation_id="c",  # type: ignore[call-arg]
                tenant_id="tenant",  # type: ignore[call-arg]
            )
            raw = cmd._do_execute({})

        call_kwargs = mock_motet.memory.store.call_args
        assert call_kwargs.kwargs.get("long_term") is None


# ---------------------------------------------------------------------------
# 3. MotetMemoryHelper delegation path
# ---------------------------------------------------------------------------

class TestMotetMemoryHelperLongTermDelegation:
    """When delegating via motet.do(memory_store, ...), long_term must appear in MemoryStoreData."""

    def test_long_term_included_in_delegated_data(self):
        from motet.core.commands.decorator import MotetContext

        mock_mgr = Mock()
        motet = MotetContext(task_id="task-1", worker_context={"memory_manager": mock_mgr})
        motet.do = Mock(return_value={"memory_id": "m3", "stored": True})

        motet.memory.store(content="panel", type="panel_analysis", long_term=True)

        motet.do.assert_called_once()
        data_arg = motet.do.call_args[1].get("data") or motet.do.call_args[0][1]
        assert isinstance(data_arg, MemoryStoreData)
        assert data_arg.long_term is True

    def test_long_term_none_when_not_passed(self):
        from motet.core.commands.decorator import MotetContext

        mock_mgr = Mock()
        motet = MotetContext(task_id="task-1", worker_context={"memory_manager": mock_mgr})
        motet.do = Mock(return_value={"memory_id": "m4", "stored": True})

        motet.memory.store(content="note", type="note")

        data_arg = motet.do.call_args[1].get("data") or motet.do.call_args[0][1]
        assert data_arg.long_term is None


# ---------------------------------------------------------------------------
# 4. MotetMemoryHelper direct (non-delegating) path
# ---------------------------------------------------------------------------

class TestMotetMemoryHelperLongTermDirect:
    """When inside a memory command (no delegation), long_term must be forwarded via **kwargs."""

    def test_long_term_forwarded_to_manager_directly(self):
        from motet.core.commands.decorator import MotetContext

        mock_mgr = Mock()
        mock_mgr.store_memory.return_value = {"id": "m5"}
        motet = MotetContext(worker_context={"memory_manager": mock_mgr})
        motet._command = Mock(get_command_type=Mock(return_value="core.memory_store"))

        motet.memory.store(content="direct", type="note", long_term=True)

        mock_mgr.store_memory.assert_called_once()
        call_kwargs = mock_mgr.store_memory.call_args
        assert call_kwargs.kwargs.get("long_term") is True


# ---------------------------------------------------------------------------
# 5. MemoryManager dispatches vector index for custom types with long_term=True
# ---------------------------------------------------------------------------

class TestMemoryManagerLongTermDispatch:
    """MemoryManager.store_memory must dispatch vector indexing for arbitrary types when long_term=True."""

    def test_custom_type_with_long_term_dispatches_vector_index(self):
        from motet.core.memory.manager import MemoryManager

        mock_stack = MagicMock()
        mock_stack.config.memory_long_term_tag = "ltm"
        mock_stack.config.memory_short_term_tag = "stm"
        mock_stack.config.memory_working_tag = "wm"
        mock_stack.vector = MagicMock()

        identity = SimpleNamespace(
            motet_id="m1", tenant_id="t1", principal_id="p1",
            conversation_id=None, agent_id=None,
        )
        kv = MagicMock()

        mgr = MemoryManager(mock_stack)
        with patch.object(mgr, "_scoped_kv_store", return_value=kv), \
             patch.object(mgr, "_resolve_identity_context", return_value=identity), \
             patch.object(mgr, "_try_dispatch_vector_index", return_value=True) as mock_dispatch:
            out = mgr.store_memory(
                content="panel insight",
                type="panel_analysis",
                tags=["panel"],
                metadata={},
                long_term=True,
                working=False,
                motet_context=identity,
            )

        mock_dispatch.assert_called_once()
        assert "vector_pending" in out["stored_in"]

    def test_custom_type_without_long_term_does_not_dispatch(self):
        from motet.core.memory.manager import MemoryManager

        mock_stack = MagicMock()
        mock_stack.config.memory_long_term_tag = "ltm"
        mock_stack.config.memory_short_term_tag = "stm"
        mock_stack.config.memory_working_tag = "wm"
        mock_stack.vector = MagicMock()

        identity = SimpleNamespace(
            motet_id="m1", tenant_id="t1", principal_id="p1",
            conversation_id=None, agent_id=None,
        )
        kv = MagicMock()

        mgr = MemoryManager(mock_stack)
        with patch.object(mgr, "_scoped_kv_store", return_value=kv), \
             patch.object(mgr, "_resolve_identity_context", return_value=identity), \
             patch.object(mgr, "_try_dispatch_vector_index", return_value=True) as mock_dispatch:
            out = mgr.store_memory(
                content="panel insight",
                type="panel_analysis",
                tags=["panel"],
                metadata={},
                motet_context=identity,
            )

        mock_dispatch.assert_not_called()
        assert "vector_pending" not in out["stored_in"]
