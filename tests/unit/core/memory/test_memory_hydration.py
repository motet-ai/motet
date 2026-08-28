"""Tests for _hydrate_semantic_results_from_kv (ADR-0092 KV hydration of vector hits)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from motet.core.commands.builtin.memory import _hydrate_semantic_results_from_kv
from motet.core.types import MemoryItem


def _make_motet_with_kv(kv_items: dict) -> MagicMock:
    """Build a mock MotetContext whose memory._scoped_kv_store returns a KV with get()."""

    kv = MagicMock()
    kv.get.side_effect = lambda mid: kv_items.get(mid)

    memory_manager = MagicMock()
    memory_manager._scoped_kv_store.return_value = kv

    motet = MagicMock()
    motet.memory = memory_manager
    return motet


def test_hydration_merges_kv_content_into_vector_hit() -> None:
    """Vector hit with minimal fields is enriched with full KV content and metadata."""
    vector_item = MemoryItem(
        id="mem-1",
        type="user_message",
        content="",
        tags=["ltm"],
        metadata={"search_score": 0.85},
    )

    kv_item = MemoryItem(
        id="mem-1",
        type="user_message",
        content="The full decrypted content from KV store",
        tags=["ltm", "agent:core.default"],
        metadata={"source": "user", "importance": 7},
        tenant_id="t1",
        principal_id="p1",
    )

    motet = _make_motet_with_kv({"mem-1": kv_item})
    result = _hydrate_semantic_results_from_kv(motet, [vector_item])

    assert len(result) == 1
    merged = result[0]
    assert merged["content"] == "The full decrypted content from KV store"
    assert merged["metadata"]["search_score"] == 0.85
    assert merged["metadata"]["source"] == "user"


def test_hydration_preserves_search_score_over_kv_metadata() -> None:
    """search_score from vector result survives the KV merge."""
    vector_item = MemoryItem(
        id="mem-2",
        type="note",
        content="",
        tags=[],
        metadata={"search_score": 0.92, "distance": 0.08},
    )

    kv_item = MemoryItem(
        id="mem-2",
        type="note",
        content="Full note content",
        tags=["important"],
        metadata={"search_score": 0.0, "other_field": "value"},
    )

    motet = _make_motet_with_kv({"mem-2": kv_item})
    result = _hydrate_semantic_results_from_kv(motet, [vector_item])

    merged = result[0]
    assert merged["metadata"]["search_score"] == 0.92
    assert merged["metadata"]["distance"] == 0.08
    assert merged["metadata"]["other_field"] == "value"


def test_hydration_returns_base_item_when_kv_miss() -> None:
    """When KV lookup returns None, the original vector item is returned as-is."""
    vector_item = MemoryItem(
        id="mem-missing",
        type="user_message",
        content="",
        tags=["ltm"],
        metadata={"search_score": 0.7},
    )

    motet = _make_motet_with_kv({})
    result = _hydrate_semantic_results_from_kv(motet, [vector_item])

    assert len(result) == 1
    assert result[0]["id"] == "mem-missing"
    assert result[0]["content"] == ""


def test_hydration_returns_items_unchanged_when_no_memory_manager() -> None:
    """When motet.memory is None, items pass through unmodified."""
    vector_item = MemoryItem(
        id="mem-1", type="note", content="", tags=[], metadata={}
    )

    motet = MagicMock()
    motet.memory = None

    result = _hydrate_semantic_results_from_kv(motet, [vector_item])
    assert len(result) == 1


def test_hydration_returns_empty_for_empty_input() -> None:
    """Empty item list returns empty list without calling KV."""
    motet = _make_motet_with_kv({})
    result = _hydrate_semantic_results_from_kv(motet, [])
    assert result == []
    motet.memory._scoped_kv_store.assert_not_called()


def test_hydration_handles_kv_get_exception_gracefully() -> None:
    """If KV.get raises, the original vector item is returned and processing continues."""
    vector_item_ok = MemoryItem(
        id="mem-ok", type="note", content="", tags=[], metadata={"search_score": 0.9}
    )
    vector_item_err = MemoryItem(
        id="mem-err", type="note", content="", tags=[], metadata={"search_score": 0.8}
    )

    kv_item_ok = MemoryItem(
        id="mem-ok", type="note", content="Full content", tags=[], metadata={}
    )

    def kv_get(mid: str):
        if mid == "mem-err":
            raise RuntimeError("connection lost")
        return kv_item_ok if mid == "mem-ok" else None

    kv = MagicMock()
    kv.get.side_effect = kv_get

    memory_manager = MagicMock()
    memory_manager._scoped_kv_store.return_value = kv

    motet = MagicMock()
    motet.memory = memory_manager

    result = _hydrate_semantic_results_from_kv(motet, [vector_item_ok, vector_item_err])

    assert len(result) == 2
    assert result[0]["content"] == "Full content"
    assert result[1]["id"] == "mem-err"
    assert result[1]["content"] == ""
