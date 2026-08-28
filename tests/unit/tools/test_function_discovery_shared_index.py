"""
Motet - Function discovery shared index coordination tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-02

Description:
    Guards cross-worker coordination of the shared function discovery index (#156).

    A rebuild is destructive: it drops the vector index and repopulates it from
    the calling worker's registry. Workers do not have identical registries at
    startup — MCP tools register asynchronously afterwards — and they do not
    share a filesystem, so the on-disk manifest made every worker conclude no
    index existed and rebuild the shared one from its own partial catalog. Each
    parallel restart therefore evicted whole tool families.

    These tests pin:
    - the manifest is published to and read from Redis, with the file as cache
    - incremental publishes merge, so one worker cannot evict another's entries
    - removals still take effect through a merge
    - exactly one worker rebuilds; the rest adopt what it published
    - a worker that cannot take the writer lock waits rather than rebuilding
    - waiting is bounded, and giving up is loud

Dependencies:
    - motet.core.tools.function_discovery_vector_store: store under test
    - pytest: test runner

Usage:
    pytest tests/unit/tools/test_function_discovery_shared_index.py -v

Notes:
    - `_FakeSharedRedis` implements only GET/SET plus the WATCH/MULTI/EXEC
      subset the publisher uses, and raises on a concurrent write to a watched
      key so the optimistic retry path is exercised for real.
    - No Valkey, embedding service, or Celery worker is required.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from motet.core.tools.function_discovery_vector_store import FunctionDiscoveryVectorStore

MANIFEST_KEY = "motet:function_discovery:manifest"
INDEX_NAME = "imf_function_discovery_idx"
MODEL = "sentence-transformers/all-MiniLM-L12-v2"


class _WatchError(Exception):
    """Stands in for redis.exceptions.WatchError."""


class _FakePipeline:
    def __init__(self, redis: "_FakeSharedRedis") -> None:
        self._redis = redis
        self._watched: Dict[str, Any] = {}
        self._queued: List[Any] = []
        self._buffering = False

    def __enter__(self) -> "_FakePipeline":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def watch(self, key: str) -> None:
        self._watched[key] = self._redis.store.get(key)

    def get(self, key: str) -> Any:
        return self._redis.store.get(key)

    def multi(self) -> None:
        self._buffering = True

    def set(self, key: str, value: Any) -> None:
        self._queued.append((key, value))

    def execute(self) -> List[Any]:
        for key, snapshot in self._watched.items():
            if self._redis.store.get(key) != snapshot:
                raise _WatchError("watched key changed")
        for key, value in self._queued:
            self._redis.store[key] = value
            self._redis.set_calls += 1
        return [True] * len(self._queued)


class _FakeSharedRedis:
    """GET/SET plus the WATCH/MULTI/EXEC subset the manifest publisher uses."""

    def __init__(self) -> None:
        self.store: Dict[str, Any] = {}
        self.set_calls = 0
        # Invoked once, immediately after WATCH, to simulate another worker
        # writing the key mid-transaction.
        self.interleave_once: Optional[Any] = None

    def get(self, key: str) -> Any:
        return self.store.get(key)

    def set(self, key: str, value: Any) -> Any:
        self.store[key] = value
        self.set_calls += 1
        return True

    def pipeline(self) -> _FakePipeline:
        pipe = _FakePipeline(self)
        if self.interleave_once is not None:
            interleave, self.interleave_once = self.interleave_once, None
            original_watch = pipe.watch

            def _watch_then_interleave(key: str) -> None:
                original_watch(key)
                interleave()

            pipe.watch = _watch_then_interleave  # type: ignore[method-assign]
        return pipe


class _FakeLock:
    """Single-holder lock whose acquire returns None when already held."""

    def __init__(self) -> None:
        self.held_by: Optional[str] = None
        self.acquisitions = 0

    def factory(self, owner: str):
        def _acquire():
            if self.held_by is not None:
                return None
            self.held_by = owner
            self.acquisitions += 1
            return SimpleNamespace(release_sync=self._release)

        return _acquire

    def _release(self) -> None:
        self.held_by = None


def _tool(description: str) -> SimpleNamespace:
    return SimpleNamespace(
        description=description, category="general", keywords=[], tool_schema=None
    )


def _store(redis: Optional[_FakeSharedRedis], *, tmp_path: Any = None) -> FunctionDiscoveryVectorStore:
    """A store wired to a fake Redis, with no Valkey retriever or embedding service."""
    store = FunctionDiscoveryVectorStore.__new__(FunctionDiscoveryVectorStore)
    store._initialized = False
    store._index_version = 0
    store._id_to_entry = {}
    store._removed_doc_ids = set()
    store._keyword_index_cache = None
    store.embedding_model = MODEL
    store.persist_dir = str(tmp_path) if tmp_path else "/nonexistent"
    store._manifest_path = str(tmp_path / "manifest.json") if tmp_path else ""
    store._valkey_redis = redis
    store._valkey_index_name = INDEX_NAME
    store._shared_manifest_key = MANIFEST_KEY
    store._initialize_hybrid_retriever = lambda: None  # type: ignore[method-assign]
    return store


def _with_entries(store: FunctionDiscoveryVectorStore, names: List[str]) -> None:
    for name in names:
        store._build_tool_item(name, _tool(f"does {name}"))


def _published(redis: _FakeSharedRedis) -> Dict[str, Any]:
    payload = FunctionDiscoveryVectorStore._decode_manifest(redis.store.get(MANIFEST_KEY))
    assert payload is not None
    return payload


def _published_tools(redis: _FakeSharedRedis) -> set:
    return {
        entry["tool_name"]
        for entry in _published(redis)["id_to_entry"].values()
        if entry.get("type") == "tool"
    }


class TestSharedManifestRoundTrip:
    def test_encode_decode_round_trip(self) -> None:
        payload = {"embedding_model": MODEL, "id_to_entry": {"tool:a": {"tool_name": "a"}}}
        assert FunctionDiscoveryVectorStore._decode_manifest(
            FunctionDiscoveryVectorStore._encode_manifest(payload)
        ) == payload

    def test_encoded_manifest_is_ascii_text(self) -> None:
        """The shared Redis client decodes responses to str, so the value must be text."""
        blob = FunctionDiscoveryVectorStore._encode_manifest({"id_to_entry": {}})
        assert isinstance(blob, str)
        blob.encode("ascii")

    def test_encoding_compresses(self) -> None:
        entries = {f"tool:t{i}": {"tool_name": f"t{i}", "description": "x" * 500} for i in range(100)}
        payload = {"embedding_model": MODEL, "id_to_entry": entries}
        raw = len(json.dumps(payload))
        assert len(FunctionDiscoveryVectorStore._encode_manifest(payload)) < raw / 2

    def test_decode_tolerates_garbage(self) -> None:
        assert FunctionDiscoveryVectorStore._decode_manifest("not-base64!!") is None
        assert FunctionDiscoveryVectorStore._decode_manifest(None) is None


class TestPublishSemantics:
    def test_full_reindex_publish_replaces(self) -> None:
        redis = _FakeSharedRedis()
        first = _store(redis)
        _with_entries(first, ["core.a", "core.b"])
        first._save_manifest(authoritative=True)

        second = _store(redis)
        _with_entries(second, ["core.c"])
        second._save_manifest(authoritative=True)

        assert _published_tools(redis) == {"core.c"}

    def test_incremental_publish_merges_instead_of_replacing(self) -> None:
        """
        The reported symptom: a worker publishing its own view wipes the others.

        Worker A rebuilds with the built-ins, then A and B each register their
        own MCP server. All three sets must survive.
        """
        redis = _FakeSharedRedis()
        worker_a = _store(redis)
        _with_entries(worker_a, ["core.one", "core.two"])
        worker_a._save_manifest(authoritative=True)

        worker_b = _store(redis)
        assert worker_b._load_existing_index() is True

        _with_entries(worker_a, ["mcp.alpha.run"])
        worker_a._save_manifest()
        _with_entries(worker_b, ["mcp.beta.run"])
        worker_b._save_manifest()

        assert _published_tools(redis) == {
            "core.one",
            "core.two",
            "mcp.alpha.run",
            "mcp.beta.run",
        }

    def test_merge_is_adopted_locally(self) -> None:
        """A merging worker takes on the union, so its keyword ranking sees other workers' tools."""
        redis = _FakeSharedRedis()
        worker_a = _store(redis)
        _with_entries(worker_a, ["mcp.alpha.run"])
        worker_a._save_manifest(authoritative=True)

        worker_b = _store(redis)
        _with_entries(worker_b, ["mcp.beta.run"])
        worker_b._save_manifest()

        assert "tool:mcp.alpha.run" in worker_b._id_to_entry
        assert worker_b._index_version == 2

    def test_removals_are_not_resurrected_by_the_merge(self) -> None:
        redis = _FakeSharedRedis()
        store = _store(redis)
        _with_entries(store, ["core.a", "mcp.gone.run"])
        store._save_manifest(authoritative=True)

        store._id_to_entry.pop("tool:mcp.gone.run")
        store._removed_doc_ids.add("tool:mcp.gone.run")
        store._save_manifest()

        assert _published_tools(redis) == {"core.a"}

    def test_concurrent_publish_retries_and_preserves_both(self) -> None:
        """A racing write to the watched key must retry, not clobber."""
        redis = _FakeSharedRedis()
        seed = _store(redis)
        _with_entries(seed, ["core.seed"])
        seed._save_manifest(authoritative=True)

        interloper = _store(redis)
        _with_entries(interloper, ["core.seed", "mcp.racer.run"])
        redis.interleave_once = lambda: interloper._save_manifest(authoritative=True)

        worker = _store(redis)
        _with_entries(worker, ["mcp.slow.run"])
        worker._save_manifest()

        assert _published_tools(redis) == {"core.seed", "mcp.racer.run", "mcp.slow.run"}

    def test_publish_survives_redis_outage(self, tmp_path) -> None:
        """Losing Redis must not raise into the indexing path; the file copy still lands."""

        class _BrokenRedis(_FakeSharedRedis):
            def set(self, key: str, value: Any) -> Any:
                raise RuntimeError("redis down")

            def pipeline(self) -> Any:
                raise RuntimeError("redis down")

        store = _store(_BrokenRedis(), tmp_path=tmp_path)
        _with_entries(store, ["core.a"])
        store._save_manifest(authoritative=True)

        with open(store._manifest_path, encoding="utf-8") as fh:
            assert "core.a" in json.load(fh)["id_to_entry"]["tool:core.a"]["tool_name"]


class TestLoadPrefersSharedManifest:
    def test_redis_manifest_wins_over_local_file(self, tmp_path) -> None:
        """
        The file is a per-container cache and can describe an index nobody else has.

        Trusting it first is what let a restarting worker rebuild over a healthy
        shared index.
        """
        redis = _FakeSharedRedis()
        publisher = _store(redis)
        _with_entries(publisher, ["core.shared"])
        publisher._save_manifest(authoritative=True)

        reader = _store(redis, tmp_path=tmp_path)
        _with_entries(reader, ["core.stale_local"])
        reader._save_manifest_file()
        reader._id_to_entry = {}

        assert reader._load_existing_index() is True
        assert {e["tool_name"] for e in reader._id_to_entry.values()} == {"core.shared"}

    def test_falls_back_to_file_when_redis_has_nothing(self, tmp_path) -> None:
        redis = _FakeSharedRedis()
        store = _store(redis, tmp_path=tmp_path)
        _with_entries(store, ["core.cached"])
        store._save_manifest_file()
        store._id_to_entry = {}

        assert store._load_existing_index() is True
        assert {e["tool_name"] for e in store._id_to_entry.values()} == {"core.cached"}

    def test_no_manifest_anywhere_means_rebuild(self) -> None:
        assert _store(_FakeSharedRedis())._load_existing_index() is False

    @pytest.mark.parametrize(
        "mutation",
        [
            {"entry_schema_version": 1},
            {"embedding_model": "some-other-model"},
            {"index_name": "a_different_index"},
        ],
        ids=["stale_entry_schema", "different_embedding_model", "different_index"],
    )
    def test_incompatible_manifest_is_rejected(self, mutation: Dict[str, Any]) -> None:
        redis = _FakeSharedRedis()
        publisher = _store(redis)
        _with_entries(publisher, ["core.a"])
        payload = publisher._manifest_payload(publisher._id_to_entry)
        payload.update(mutation)
        redis.set(MANIFEST_KEY, FunctionDiscoveryVectorStore._encode_manifest(payload))

        reader = _store(redis)
        assert reader.shared_index_is_current() is False
        assert reader._load_existing_index() is False


class _RebuildRecorder:
    """Stands in for a real rebuild: records the call and publishes authoritatively."""

    def __init__(self, store: FunctionDiscoveryVectorStore, tools: List[str]) -> None:
        self.count = 0
        self._store = store
        self._tools = tools
        store.index_tools_and_workflows = self._rebuild  # type: ignore[method-assign]

    def _rebuild(self, **kwargs: Any) -> int:
        self.count += 1
        _with_entries(self._store, self._tools)
        self._store._initialized = True
        self._store._save_manifest(authoritative=True)
        return len(self._tools)


def _ensure(store: FunctionDiscoveryVectorStore, lock_factory, **kwargs: Any) -> str:
    return store.ensure_shared_index(
        SimpleNamespace(list_items=dict),
        SimpleNamespace(list_all=list),
        lock_factory=lock_factory,
        sleep_fn=lambda _seconds: None,
        **kwargs,
    )


class TestEnsureSharedIndex:
    def test_rebuilds_when_nothing_is_published(self) -> None:
        redis = _FakeSharedRedis()
        lock = _FakeLock()
        store = _store(redis)
        rebuild = _RebuildRecorder(store, ["core.a"])

        assert _ensure(store, lock.factory("a")) == "rebuilt"
        assert rebuild.count == 1
        assert _published_tools(redis) == {"core.a"}

    def test_second_worker_adopts_instead_of_rebuilding(self) -> None:
        """The core fix: only one worker may rebuild a shared, destructive index."""
        redis = _FakeSharedRedis()
        lock = _FakeLock()
        first, second = _store(redis), _store(redis)
        first_rebuild = _RebuildRecorder(first, ["core.a", "mcp.alpha.run"])
        second_rebuild = _RebuildRecorder(second, ["core.a"])

        assert _ensure(first, lock.factory("first")) == "rebuilt"
        assert _ensure(second, lock.factory("second")) == "loaded"

        assert (first_rebuild.count, second_rebuild.count) == (1, 0)
        assert _published_tools(redis) == {"core.a", "mcp.alpha.run"}

    def test_waits_for_the_lock_holder_then_loads(self) -> None:
        """
        A worker that loses the lock race must not rebuild alongside the winner.

        The holder publishes on the second poll; the waiter should pick that up
        and skip its own rebuild.
        """
        redis = _FakeSharedRedis()
        winner = _store(redis)
        _with_entries(winner, ["core.a", "mcp.alpha.run"])

        polls = {"n": 0}

        def _publish_on_second_poll(_seconds: float) -> None:
            polls["n"] += 1
            if polls["n"] == 2:
                winner._save_manifest(authoritative=True)

        waiter = _store(redis)
        rebuild = _RebuildRecorder(waiter, ["core.a"])
        outcome = waiter.ensure_shared_index(
            SimpleNamespace(list_items=dict),
            SimpleNamespace(list_all=list),
            lock_factory=lambda: None,
            sleep_fn=_publish_on_second_poll,
            wait_timeout_seconds=60.0,
        )

        assert outcome == "loaded_after_wait"
        assert rebuild.count == 0
        assert _published_tools(redis) == {"core.a", "mcp.alpha.run"}

    def test_rechecks_after_acquiring_a_contended_lock(self) -> None:
        """
        Whoever held the lock was probably rebuilding, so re-check before clearing.

        Here the lock frees up only after the holder has published.
        """
        redis = _FakeSharedRedis()
        holder = _store(redis)
        _with_entries(holder, ["core.a"])

        state = {"free": False}

        def _lock_factory():
            if not state["free"]:
                return None
            return SimpleNamespace(release_sync=lambda: None)

        def _finish_holder(_seconds: float) -> None:
            holder._save_manifest(authoritative=True)
            state["free"] = True

        waiter = _store(redis)
        # Loading is disabled on the poll path so the re-check inside the lock is
        # what has to catch the published manifest.
        rebuild = _RebuildRecorder(waiter, ["core.b"])
        loads = {"n": 0}
        real_load = waiter._load_existing_index

        def _load_only_under_lock() -> bool:
            loads["n"] += 1
            return False if loads["n"] == 2 else real_load()

        waiter._load_existing_index = _load_only_under_lock  # type: ignore[method-assign]

        outcome = waiter.ensure_shared_index(
            SimpleNamespace(list_items=dict),
            SimpleNamespace(list_all=list),
            lock_factory=_lock_factory,
            sleep_fn=_finish_holder,
            wait_timeout_seconds=60.0,
        )

        assert outcome == "loaded_after_wait"
        assert rebuild.count == 0

    def test_gives_up_waiting_and_rebuilds(self) -> None:
        """Running with no index at all is worse than a redundant rebuild."""
        redis = _FakeSharedRedis()
        store = _store(redis)
        rebuild = _RebuildRecorder(store, ["core.a"])

        outcome = _ensure(store, lambda: None, wait_timeout_seconds=0.0)

        assert outcome == "rebuilt_after_timeout"
        assert rebuild.count == 1

    def test_force_reindex_rebuilds_even_with_a_current_manifest(self) -> None:
        redis = _FakeSharedRedis()
        lock = _FakeLock()
        publisher = _store(redis)
        _with_entries(publisher, ["core.old"])
        publisher._save_manifest(authoritative=True)

        store = _store(redis)
        rebuild = _RebuildRecorder(store, ["core.new"])
        assert _ensure(store, lock.factory("x"), force_reindex=True) == "rebuilt"
        assert rebuild.count == 1

    def test_lock_is_released_when_the_rebuild_raises(self) -> None:
        redis = _FakeSharedRedis()
        lock = _FakeLock()
        store = _store(redis)

        def _boom(**_kwargs: Any) -> int:
            raise RuntimeError("indexing blew up")

        store.index_tools_and_workflows = _boom  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="indexing blew up"):
            _ensure(store, lock.factory("a"))
        assert lock.held_by is None

    def test_lock_factory_failure_does_not_abort_startup(self) -> None:
        """A broken lock backend should degrade to the timeout path, not raise."""
        redis = _FakeSharedRedis()
        store = _store(redis)
        rebuild = _RebuildRecorder(store, ["core.a"])

        def _explode():
            raise RuntimeError("redis unreachable")

        assert _ensure(store, _explode, wait_timeout_seconds=0.0) == "rebuilt_after_timeout"
        assert rebuild.count == 1
