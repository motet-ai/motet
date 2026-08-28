"""Unit tests for Valkey memory vector store (ADR-0092)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from motet.core.memory.valkey_vector_store import (
    ValkeyVectorStore,
    _build_identity_filter,
    _escape_tag_query_value,
    _tag_field_value_from_list,
    _tags_list_from_field,
)


def test_escape_tag_query_escapes_special_chars() -> None:
    assert "\\{" in _escape_tag_query_value("{tenant}")


def test_escape_tag_query_preserves_hyphen_for_ids() -> None:
    """Hyphenated IDs should not be escaped in TAG filters."""
    value = "motet-global-123"
    escaped = _escape_tag_query_value(value)
    assert escaped == value


def test_escape_tag_query_preserves_colon_and_dot_for_namespaced_tags() -> None:
    """Namespaced tags like agent:core.default should remain unescaped for TAG matching."""
    value = "agent:core.default"
    escaped = _escape_tag_query_value(value)
    assert escaped == value
    assert "\\:" not in escaped
    assert "\\." not in escaped


def test_tag_field_roundtrip_simple_tags() -> None:
    tags = ["ltm", "tenant:x", "stm"]
    field = _tag_field_value_from_list(tags)
    got = _tags_list_from_field(field)
    assert set(got) == set(tags)


def test_build_identity_filter_empty_when_none() -> None:
    assert _build_identity_filter() == ""
    assert _build_identity_filter(tenant_id="", principal_id=None) == ""


def test_build_identity_filter_builds_tag_predicates() -> None:
    s = _build_identity_filter(tenant_id="t1", principal_id="p1", motet_id="m1")
    assert "@tenant_id:{" in s
    assert "t1" in s
    assert "@principal_id:{" in s
    assert "p1" in s
    assert "@motet_id:{" in s
    assert "m1" in s


def test_build_identity_filter_keeps_hyphenated_values_raw() -> None:
    s = _build_identity_filter(
        tenant_id="motet-global",
        principal_id="4944d1c6-9a19-490b-9256-1b439709135b",
    )
    assert "\\-" not in s
    assert "motet-global" in s
    assert "4944d1c6-9a19-490b-9256-1b439709135b" in s


def test_build_identity_filter_includes_agent_id() -> None:
    """When agent_id provided, filter uses dedicated @agent_id TAG field."""
    s = _build_identity_filter(tenant_id="t1", agent_id="core.default")
    assert "@tenant_id:{" in s
    assert "@agent_id:{" in s
    assert "core.default" in s
    assert "\\." not in s


@pytest.fixture
def valkey_store_minimal() -> ValkeyVectorStore:
    """ValkeyVectorStore with Redis mocked and a stub embedding_fn (no sentence_transformers)."""

    def execute_command(*args: object, **kwargs: object) -> object:
        if args and args[0] in ("FT.INFO", b"FT.INFO"):
            raise RuntimeError("no such index")
        return b"OK"

    mock_redis = MagicMock()
    mock_redis.execute_command.side_effect = execute_command

    mock_rm = MagicMock()
    mock_rm.get_sync_binary_client.return_value = mock_redis

    def stub_embed(_text: str) -> np.ndarray:
        return np.zeros(384, dtype=np.float32)

    with patch(
        "motet.core.distributed.redis_manager.get_redis_manager",
        return_value=mock_rm,
    ):
        store = ValkeyVectorStore(
            index_name="unit_test_mem_idx",
            key_prefix="unit:memvec:",
            redis_client_id="unit_test_mem",
            embedding_fn=stub_embed,
            embedding_dim=384,
            enable_embedding_cache=False,
            enable_result_cache=False,
        )
        store._redis = mock_redis
        return store


def test_build_knn_query_includes_tag_filter(valkey_store_minimal: ValkeyVectorStore) -> None:
    store = valkey_store_minimal
    vec = b"\x00" * (4 * 384)
    cmd = store._build_knn_query(top_k=3, vec_bytes=vec, tags=["ltm", "tenant:a"])
    assert cmd[0] == "FT.SEARCH"
    assert cmd[1] == store._index_name
    q = cmd[2]
    assert isinstance(q, str)
    assert "KNN" in q
    assert "@user_tags:" in q


def test_build_knn_query_no_tags(valkey_store_minimal: ValkeyVectorStore) -> None:
    store = valkey_store_minimal
    vec = b"\x00" * (4 * 384)
    cmd = store._build_knn_query(top_k=5, vec_bytes=vec, tags=None)
    q = cmd[2]
    assert q.startswith("*=>")


def test_build_knn_query_includes_identity_filters(valkey_store_minimal: ValkeyVectorStore) -> None:
    """ADR-0092: KNN query applies tenant/principal/conversation/agent isolation."""
    store = valkey_store_minimal
    vec = b"\x00" * (4 * 384)
    cmd = store._build_knn_query(
        top_k=3,
        vec_bytes=vec,
        tags=["ltm"],
        tenant_id="tenant-1",
        principal_id="user-1",
        conversation_id="conv-1",
        motet_id="motet-1",
        agent_id="my-agent",
    )
    q = cmd[2]
    assert "KNN" in q
    assert "@user_tags:" in q
    assert "@tenant_id:{" in q
    assert "@principal_id:{" in q
    assert "@conversation_id:{" in q
    assert "@motet_id:{" in q
    assert "@agent_id:{" in q
    assert "tenant" in q and "user" in q and "conv" in q and "motet" in q


# --- Clear / destructive operations ---


def test_clear_all_with_filters_uses_clear_by_filter(valkey_store_minimal: ValkeyVectorStore) -> None:
    """When tenant/principal/conversation/motet/agent provided, clear_all uses _clear_by_filter."""
    store = valkey_store_minimal

    def _execute_command(*args: object, **kwargs: object) -> object:
        if args and args[0] in ("FT.INFO", b"FT.INFO"):
            raise RuntimeError("no such index")
        if args and args[0] in ("FT.SEARCH", b"FT.SEARCH"):
            return [2, b"unit:memvec:m1", b"unit:memvec:m2"]
        return b"OK"

    store._redis.execute_command.side_effect = _execute_command
    n = store.clear_all(tenant_id="t1", principal_id="p1")
    assert n == 2
    calls = [c[0] for c in store._redis.execute_command.call_args_list]
    assert any("FT.SEARCH" in str(c) for c in calls)
    assert store._redis.delete.call_count == 2
    # Filter should include tenant and principal
    search_call = next(
        c for c in store._redis.execute_command.call_args_list
        if c[0] and c[0][0] in ("FT.SEARCH", b"FT.SEARCH")
    )
    filter_arg = search_call[0][2]
    assert "t1" in str(filter_arg)
    assert "p1" in str(filter_arg)


def test_clear_all_without_filters_scans_and_deletes(valkey_store_minimal: ValkeyVectorStore) -> None:
    """When no filters, clear_all scans key prefix and deletes matching keys."""
    store = valkey_store_minimal
    store._redis.scan.return_value = (0, [b"unit:memvec:x1", b"unit:memvec:x2"])
    n = store.clear_all()
    assert n == 2
    store._redis.scan.assert_called_with(cursor=0, match="unit:memvec:*", count=500)
    # delete(*keys) is one call with multiple keys
    assert store._redis.delete.call_count == 1
    assert len(store._redis.delete.call_args[0]) == 2


def test_clear_by_type_with_filters_combines_type_and_identity(
    valkey_store_minimal: ValkeyVectorStore,
) -> None:
    """clear_by_type with filters uses @memory_type + identity in FT.SEARCH."""

    def _execute_command(*args: object, **kwargs: object) -> object:
        if args and args[0] in ("FT.INFO", b"FT.INFO"):
            raise RuntimeError("no such index")
        if args and args[0] in ("FT.SEARCH", b"FT.SEARCH"):
            return [1, b"unit:memvec:doc1"]
        return b"OK"

    store = valkey_store_minimal
    store._redis.execute_command.side_effect = _execute_command
    n = store.clear_by_type("assistant_response", tenant_id="t1", agent_id="foo")
    assert n == 1
    search_call = next(
        c for c in store._redis.execute_command.call_args_list
        if c[0] and c[0][0] in ("FT.SEARCH", b"FT.SEARCH")
    )
    filter_expr = str(search_call[0][2])
    assert "memory_type" in filter_expr
    assert "assistant_response" in filter_expr or "assistant\\_response" in filter_expr
    assert "t1" in filter_expr
    assert "agent" in filter_expr


def test_clear_by_filter_pagination_stops_when_batch_smaller_than_page(
    valkey_store_minimal: ValkeyVectorStore,
) -> None:
    """_clear_by_filter stops when returned batch is smaller than page_size."""

    def _execute_command(*args: object, **kwargs: object) -> object:
        if args and args[0] in ("FT.INFO", b"FT.INFO"):
            raise RuntimeError("no such index")
        if args and args[0] in ("FT.SEARCH", b"FT.SEARCH"):
            return [1, b"unit:memvec:only_one"]
        return b"OK"

    store = valkey_store_minimal
    store._redis.execute_command.side_effect = _execute_command
    n = store._clear_by_filter("@tenant_id:{t1}", limit=10000)
    assert n == 1
    assert store._redis.delete.call_count == 1


# --- Index schema validation ---


def test_extract_vector_dim_from_ft_info(valkey_store_minimal: ValkeyVectorStore) -> None:
    """_extract_vector_dim_from_ft_info finds dim in nested FT.INFO structure."""
    store = valkey_store_minimal
    assert store._extract_vector_dim_from_ft_info(None) is None
    assert store._extract_vector_dim_from_ft_info([]) is None
    # Flat list: key, value
    assert store._extract_vector_dim_from_ft_info(["dim", "384"]) == 384
    assert store._extract_vector_dim_from_ft_info(["dim", 384]) == 384
    # Nested (attributes section)
    info = ["attributes", ["identifier", "embedding", "dim", "768"]]
    assert store._extract_vector_dim_from_ft_info(info) == 768
    # Bytes values
    assert store._extract_vector_dim_from_ft_info([b"dim", b"256"]) == 256


def test_validate_index_schema_dim_match_no_warning(
    valkey_store_minimal: ValkeyVectorStore, caplog: pytest.LogCaptureFixture
) -> None:
    """When index dim matches embedder (384), no warning logged."""
    store = valkey_store_minimal
    store._dim = 384
    info = ["attributes", ["dim", "384"]]
    store._redis.execute_command.side_effect = lambda *a, **k: (
        info if a and a[0] in ("FT.INFO", b"FT.INFO") else b"OK"
    )
    store._validate_index_schema()
    assert "dimension_mismatch" not in caplog.text
    assert "valkey_index_dimension_mismatch" not in caplog.text


def test_validate_index_schema_dim_mismatch_logs_warning(
    valkey_store_minimal: ValkeyVectorStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """When index dim differs from embedder, warning logged."""
    store = valkey_store_minimal
    store._dim = 384
    info = ["attributes", ["dim", "768"]]
    store._redis.execute_command.side_effect = lambda *a, **k: (
        info if a and a[0] in ("FT.INFO", b"FT.INFO") else b"OK"
    )
    store._validate_index_schema()
    captured = capsys.readouterr()
    combined = f"{captured.out}\n{captured.err}"
    assert "valkey_index_dimension_mismatch" in combined or "dimension_mismatch" in combined


def test_validate_index_schema_ft_info_failure_skipped(
    valkey_store_minimal: ValkeyVectorStore,
) -> None:
    """When FT.INFO fails, validation skips without raising."""
    store = valkey_store_minimal

    def _execute(*args: object, **kwargs: object) -> object:
        if args and args[0] in ("FT.INFO", b"FT.INFO"):
            raise RuntimeError("connection refused")
        return b"OK"

    store._redis.execute_command.side_effect = _execute
    store._validate_index_schema()


# --- query() response parsing ---


def test_query_parses_ft_search_response(valkey_store_minimal: ValkeyVectorStore) -> None:
    """query() correctly parses a realistic FT.SEARCH response into MemoryItem list."""
    import json

    store = valkey_store_minimal
    store._embed_text = lambda t: [0.0] * 384  # type: ignore[assignment]

    ft_response = [
        1,  # total hits
        b"unit:memvec:mem-1",  # document key
        [
            b"memory_id", b"mem-1",
            b"tenant_id", b"tenant-a",
            b"principal_id", b"user-1",
            b"motet_id", b"motet-x",
            b"conversation_id", b"conv-1",
            b"scope_type", b"conversation",
            b"memory_type", b"user_message",
            b"user_tags", b"ltm,agent:core.default",
            b"metadata_json", json.dumps({"scope_id": "conv-1"}).encode(),
            b"created_at", b"2026-03-21T12:00:00+00:00",
            b"vector_distance", b"0.25",
        ],
    ]

    def _execute_command(*args: object, **kwargs: object) -> object:
        if args and args[0] in ("FT.SEARCH", b"FT.SEARCH"):
            return ft_response
        if args and args[0] in ("FT.INFO", b"FT.INFO"):
            raise RuntimeError("unused")
        return b"OK"

    store._redis.execute_command.side_effect = _execute_command

    results = store.query("hello", top_k=3, tags=["ltm"])
    assert len(results) == 1
    item = results[0]
    assert item.id == "mem-1"
    assert item.tenant_id == "tenant-a"
    assert item.principal_id == "user-1"
    assert item.motet_id == "motet-x"
    assert item.conversation_id == "conv-1"
    assert item.scope_type == "conversation"
    assert item.type == "user_message"
    assert "ltm" in item.tags
    assert "agent:core.default" in item.tags
    assert item.metadata.get("search_score") is not None
    assert 0.0 < item.metadata["search_score"] <= 1.0


def test_query_filters_out_non_matching_tags(valkey_store_minimal: ValkeyVectorStore) -> None:
    """Client-side tag post-filter removes items whose tags don't overlap the requested tags."""
    store = valkey_store_minimal
    store._embed_text = lambda t: [0.0] * 384  # type: ignore[assignment]

    ft_response = [
        1,
        b"unit:memvec:mem-2",
        [
            b"memory_id", b"mem-2",
            b"user_tags", b"stm",
            b"metadata_json", b"{}",
            b"created_at", b"2026-03-21T12:00:00+00:00",
            b"vector_distance", b"0.1",
        ],
    ]

    store._redis.execute_command.side_effect = (
        lambda *a, **k: ft_response if a and a[0] in ("FT.SEARCH", b"FT.SEARCH") else b"OK"
    )

    results = store.query("hello", top_k=3, tags=["ltm"])
    assert len(results) == 0


def test_query_returns_empty_on_no_hits(valkey_store_minimal: ValkeyVectorStore) -> None:
    """query() returns empty list when FT.SEARCH returns zero hits."""
    store = valkey_store_minimal
    store._embed_text = lambda t: [0.0] * 384  # type: ignore[assignment]

    store._redis.execute_command.side_effect = (
        lambda *a, **k: [0] if a and a[0] in ("FT.SEARCH", b"FT.SEARCH") else b"OK"
    )

    results = store.query("nothing", top_k=5)
    assert results == []


def test_query_handles_none_values_in_identity_fields(valkey_store_minimal: ValkeyVectorStore) -> None:
    """Items with 'none' sentinel values for identity fields are mapped to None."""
    import json

    store = valkey_store_minimal
    store._embed_text = lambda t: [0.0] * 384  # type: ignore[assignment]

    ft_response = [
        1,
        b"unit:memvec:mem-3",
        [
            b"memory_id", b"mem-3",
            b"tenant_id", b"none",
            b"principal_id", b"none",
            b"motet_id", b"none",
            b"conversation_id", b"none",
            b"scope_type", b"none",
            b"memory_type", b"note",
            b"user_tags", b"",
            b"metadata_json", b"{}",
            b"created_at", b"2026-03-21T00:00:00+00:00",
            b"vector_distance", b"0.5",
        ],
    ]

    store._redis.execute_command.side_effect = (
        lambda *a, **k: ft_response if a and a[0] in ("FT.SEARCH", b"FT.SEARCH") else b"OK"
    )

    results = store.query("test", top_k=1)
    assert len(results) == 1
    item = results[0]
    assert item.tenant_id is None
    assert item.principal_id is None
    assert item.motet_id is None
    assert item.conversation_id is None
