"""Tests for async LTM vector indexing dispatch (ADR-0092, Valkey-only)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from motet.core.memory.manager import MemoryManager


def test_should_async_vector_index_always_true() -> None:
    """Async indexing is always used (Valkey-only)."""
    assert MemoryManager._should_async_vector_index(None) is True
    assert MemoryManager._should_async_vector_index(MagicMock()) is True


def test_store_memory_dispatches_skips_sync_vector_add() -> None:
    """Async path: vector.add is never called; dispatch only."""
    mock_stack = MagicMock()
    mock_stack.config.memory_long_term_tag = "ltm"
    mock_stack.config.memory_short_term_tag = "stm"
    mock_stack.config.memory_working_tag = "wm"
    mock_stack.config.enable_vector_memory = True
    mock_stack.vector = MagicMock()

    identity = SimpleNamespace(motet_id="m1", tenant_id="t1", principal_id="p1", conversation_id=None)
    mem = MagicMock()
    mem.upsert = MagicMock()

    mgr = MemoryManager(mock_stack)
    with patch.object(mgr, "_scoped_kv_store", return_value=mem), patch.object(
        mgr, "_resolve_identity_context", return_value=identity
    ), patch.object(mgr, "_try_dispatch_vector_index", return_value=True):
        out = mgr.store_memory(
            content="hello",
            type="user_message",
            tags=[],
            metadata={},
            long_term=True,
            working=False,
            motet_context=identity,
        )

    assert "vector_pending" in out["stored_in"]
    mock_stack.vector.add.assert_not_called()
