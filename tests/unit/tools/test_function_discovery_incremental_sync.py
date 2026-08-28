"""
Unit tests for bundle-scoped function discovery incremental sync.

Validates per-item hash diff behavior in sync_bundle_entries:
- unchanged docs are skipped
- changed docs are updated (remove+add only for changed IDs)
- add/remove deltas only touch affected IDs
"""

from types import SimpleNamespace

from motet.core.registry import RegistryScope, ScopeGrant
from motet.core.tools.function_discovery_vector_store import FunctionDiscoveryVectorStore, _ValkeyVectorRetriever


class _DummyHybridRetriever:
    def __init__(self, query_result=None):
        self.removed_batches = []
        self.added_batches = []
        self.query_result = query_result or {"ids": [[]], "distances": [[]]}

    def remove_documents_batch(self, doc_ids):
        self.removed_batches.append(list(doc_ids))

    def add_documents_batch(self, documents, *, doc_ids, mode, show_progress):
        self.added_batches.append(
            {
                "documents": list(documents),
                "doc_ids": list(doc_ids),
                "mode": mode,
                "show_progress": show_progress,
            }
        )

    def query(self, **_kwargs):
        return self.query_result


class _DummyRedis:
    def __init__(self):
        self.commands = []
        self.hashes = {}

    def execute_command(self, *args):
        self.commands.append(args)
        if args[0] == "FT.INFO":
            return ["index_name", args[1]]
        if args[0] == "FT.SEARCH":
            return [0]
        return None

    def hset(self, key, mapping):
        self.hashes[key] = mapping

    def scan(self, cursor=0, match=None, count=None):
        return 0, []


class _DummyToolRegistry:
    def __init__(self, tools, scopes=None):
        self._tools = dict(tools)
        self._scopes = dict(scopes or {})

    def list(self):
        return self._tools

    def list_items(self):
        return self._tools

    def get_scope(self, name):
        return self._scopes.get(name)


class _DummyWorkflowRegistry:
    def __init__(self, workflows=None):
        self._workflows = dict(workflows or {})

    def get(self, workflow_id):
        return self._workflows.get(workflow_id)


def _build_store():
    store = FunctionDiscoveryVectorStore.__new__(FunctionDiscoveryVectorStore)
    store._initialized = True
    store._index_version = 0
    store._id_to_entry = {}
    # Removals are tracked so the shared-manifest merge does not resurrect them
    # (#156); __init__ is bypassed here, so seed it explicitly.
    store._removed_doc_ids = set()
    store._hybrid_retriever = _DummyHybridRetriever()
    return store


def _tool(name: str, description: str):
    return SimpleNamespace(
        description=description,
        category="general",
        keywords=[],
        tool_schema=None,
        name=name,
    )


def test_sync_bundle_entries_skips_unchanged_docs():
    store = _build_store()
    tool_name = "calculator.math_tool"

    initial_tool_registry = _DummyToolRegistry({tool_name: _tool(tool_name, "Original description")})
    initial_item = store._build_tool_item(
        tool_name,
        initial_tool_registry.list_items()[tool_name],
        record_entry=False,
    )
    initial_hash = store._compute_item_hash(initial_item)
    store._id_to_entry = {
        f"tool:{tool_name}": {
            "type": "tool",
            "tool_name": tool_name,
            "category": "general",
            "is_mcp": False,
            "content_hash": initial_hash,
        }
    }

    result = store.sync_bundle_entries(
        "calculator",
        tool_names=[tool_name],
        workflow_ids=[],
        command_types=[],
        tool_registry=initial_tool_registry,
        workflow_registry=_DummyWorkflowRegistry(),
    )

    assert result["added"] == 0
    assert result["removed"] == 0
    assert result["updated"] == 0
    assert result["unchanged"] == 1
    assert store._hybrid_retriever.removed_batches == []
    assert store._hybrid_retriever.added_batches == []


def test_sync_bundle_entries_updates_only_changed_docs():
    store = _build_store()
    tool_name = "calculator.math_tool"

    old_registry = _DummyToolRegistry({tool_name: _tool(tool_name, "Old description")})
    old_item = store._build_tool_item(
        tool_name,
        old_registry.list_items()[tool_name],
        record_entry=False,
    )
    store._id_to_entry = {
        f"tool:{tool_name}": {
            "type": "tool",
            "tool_name": tool_name,
            "category": "general",
            "is_mcp": False,
            "content_hash": store._compute_item_hash(old_item),
        }
    }

    new_registry = _DummyToolRegistry({tool_name: _tool(tool_name, "New description")})
    result = store.sync_bundle_entries(
        "calculator",
        tool_names=[tool_name],
        workflow_ids=[],
        command_types=[],
        tool_registry=new_registry,
        workflow_registry=_DummyWorkflowRegistry(),
    )

    assert result["added"] == 0
    assert result["removed"] == 0
    assert result["updated"] == 1
    assert result["unchanged"] == 0
    assert store._hybrid_retriever.removed_batches == [[f"tool:{tool_name}"]]
    assert store._hybrid_retriever.added_batches[0]["doc_ids"] == [f"tool:{tool_name}"]


def test_index_tools_incremental_updates_changed_existing_tool():
    store = _build_store()
    tool_name = "mcp.weather.get_forecast"

    old_tool = _tool(tool_name, "old description")
    old_item = store._build_tool_item(tool_name, old_tool, record_entry=False)
    store._id_to_entry = {
        f"tool:{tool_name}": {
            "type": "tool",
            "tool_name": tool_name,
            "category": "general",
            "is_mcp": True,
            "mcp_service_id": "weather",
            "mcp_tool_name": "get_forecast",
            "content_hash": store._compute_item_hash(old_item),
        }
    }

    registry = _DummyToolRegistry({tool_name: _tool(tool_name, "new description")})
    count = store.index_tools_incremental([tool_name], registry)

    assert count == 1
    assert store._hybrid_retriever.removed_batches == [[f"tool:{tool_name}"]]
    assert store._hybrid_retriever.added_batches[0]["doc_ids"] == [f"tool:{tool_name}"]


def _workflow(workflow_id: str, name: str, description: str):
    return SimpleNamespace(
        workflow_id=workflow_id,
        name=name,
        description=description,
        use_for=["tool"],
        is_used_for_tool=lambda: True,
    )


def test_index_workflows_incremental_updates_changed_existing_workflow():
    store = _build_store()
    workflow_id = "calculator.multi_step_calc"

    old_workflow = _workflow(workflow_id, "Old name", "Old description")
    doc_id, old_item, old_entry = store._build_workflow_item(old_workflow)
    store._id_to_entry = {doc_id: old_entry}

    new_workflow = _workflow(workflow_id, "New name", "New description")
    workflow_registry = _DummyWorkflowRegistry({workflow_id: new_workflow})
    count = store.index_workflows_incremental([workflow_id], workflow_registry)

    assert count == 1
    assert store._hybrid_retriever.removed_batches == [[f"workflow:{workflow_id}"]]
    assert store._hybrid_retriever.added_batches[0]["doc_ids"] == [f"workflow:{workflow_id}"]


def test_index_commands_incremental_updates_changed_existing_command(monkeypatch):
    store = _build_store()
    command_type = "core.example_command"
    doc_id = f"command:{command_type}"

    store._id_to_entry = {
        doc_id: {
            "type": "command",
            "command_type": command_type,
            "description": "Old description",
            "content_hash": "old-hash",
        }
    }

    new_entry = {
        "type": "command",
        "command_type": command_type,
        "description": "New description",
        "content_hash": "new-hash",
    }
    new_item = SimpleNamespace(id=doc_id, content="new command content")

    monkeypatch.setattr(
        store,
        "_build_command_item",
        lambda _ct, **_kwargs: (doc_id, new_item, new_entry),
    )

    count = store.index_commands_incremental([command_type])

    assert count == 1
    assert store._hybrid_retriever.removed_batches == [[doc_id]]
    assert store._hybrid_retriever.added_batches[0]["doc_ids"] == [doc_id]


def test_reconcile_registry_state_removes_stale_docs(monkeypatch):
    store = _build_store()
    stale_tool_doc = "tool:core.stale_tool"
    stale_wf_doc = "workflow:core.stale_workflow"
    stale_cmd_doc = "command:core.stale_command"
    store._id_to_entry = {
        stale_tool_doc: {"type": "tool", "content_hash": "x"},
        stale_wf_doc: {"type": "workflow", "content_hash": "y"},
        stale_cmd_doc: {"type": "command", "content_hash": "z"},
    }

    # Keep incremental reconcilers as no-ops so we only validate stale removals.
    monkeypatch.setattr(store, "index_tools_incremental", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(store, "index_workflows_incremental", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(store, "index_user_workflows_from_catalog", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(store, "index_commands_incremental", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        "motet.core.workflow.user_catalog.catalog_user_workflow_discovery_doc_ids",
        lambda *args, **kwargs: [],
    )

    stats = store.reconcile_registry_state(
        tool_names=[],
        workflow_ids=[],
        command_types=[],
        tool_registry=_DummyToolRegistry({}),
        workflow_registry=_DummyWorkflowRegistry({}),
    )

    assert stats["stale_tools_removed"] == 1
    assert stats["stale_workflows_removed"] == 1
    assert stats["stale_commands_removed"] == 1
    assert stale_tool_doc not in store._id_to_entry
    assert stale_wf_doc not in store._id_to_entry
    assert stale_cmd_doc not in store._id_to_entry


def test_search_functions_filters_by_scope_keys():
    store = _build_store()
    store._hybrid_retriever = _DummyHybridRetriever(
        query_result={
            "ids": [["tool:core.allowed", "tool:core.denied"]],
            "distances": [[0.1, 0.2]],
        }
    )
    store._id_to_entry = {
        "tool:core.allowed": {
            "type": "tool",
            "tool_name": "core.allowed",
            "scope_keys": ["tenant-a:*:*:*"],
            "content_hash": "x",
        },
        "tool:core.denied": {
            "type": "tool",
            "tool_name": "core.denied",
            "scope_keys": ["tenant-b:*:*:*"],
            "content_hash": "y",
        },
    }

    results = store.search_functions(
        query="allowed tools",
        top_k=5,
        search_types=["tool"],
        tenant_id="tenant-a",
        motet_id="prod",
        role="admin",
        principal_id="user-1",
    )

    assert len(results) == 1
    assert results[0]["name"] == "core.allowed"


def test_search_functions_synonym_overlap_boosts_browser_tool():
    store = _build_store()
    # Seed vector ordering with web_search first; synonym/overlap boost should
    # promote http_get_browser for a "browse ... read ... news" query.
    store._hybrid_retriever = _DummyHybridRetriever(
        query_result={
            "ids": [["tool:core.web_search", "tool:core.http_get_browser"]],
            "distances": [[0.05, 0.06]],
        }
    )
    store._id_to_entry = {
        "tool:core.web_search": {
            "type": "tool",
            "tool_name": "core.web_search",
            "description": "Search the web",
            "content_hash": "a",
        },
        "tool:core.http_get_browser": {
            "type": "tool",
            "tool_name": "core.http_get_browser",
            "description": "Fetch content from websites with a browser",
            "content_hash": "b",
        },
    }

    results = store.search_functions(
        query="browse cnn.com and read me the news",
        top_k=5,
        search_types=["tool"],
    )

    assert len(results) == 2
    assert results[0]["name"] == "core.http_get_browser"
    assert results[0]["similarity_score"] > results[1]["similarity_score"]


def test_build_tool_item_indexes_core_handoff():
    """core.handoff is in the tool catalog; the handler is the grant, not a denylist."""
    store = _build_store()
    item = store._build_tool_item(
        "core.handoff",
        _tool("core.handoff", "Delegate this turn to one configured teammate agent."),
        record_entry=False,
    )
    assert item is not None
    assert item.metadata["tool_name"] == "core.handoff"


def test_build_tool_item_includes_scope_keys_from_registry():
    store = _build_store()
    tool_name = "core.scoped_tool"
    scoped_registry = _DummyToolRegistry(
        {tool_name: _tool(tool_name, "Scoped tool")},
        scopes={
            tool_name: RegistryScope(
                namespace="core",
                grants=[ScopeGrant(tenant_id="tenant-a", role="admin")],
            )
        },
    )

    item = store._build_tool_item(
        tool_name,
        scoped_registry.list_items()[tool_name],
        record_entry=False,
        scope_keys=store._resolve_tool_scope_keys(scoped_registry, tool_name),
    )
    assert item is not None
    assert item.metadata.get("scope_keys") == ["tenant-a:*:admin:*"]


def test_valkey_knn_command_shape_excludes_sortby():
    retriever = _ValkeyVectorRetriever.__new__(_ValkeyVectorRetriever)
    retriever._index_name = "imf_function_discovery_idx"
    args = retriever._build_knn_search_command(n_results=10, vec_bytes=b"abc")

    assert args[0] == "FT.SEARCH"
    assert args[1] == "imf_function_discovery_idx"
    assert "KNN 10" in args[2]
    assert "SORTBY" not in args
    assert args[-2:] == ["DIALECT", "2"]


def test_valkey_retriever_uses_injected_embedding_fn_for_indexing_and_query():
    calls = []

    def embed(text):
        calls.append(text)
        return [1.0, 2.0, 3.0]

    redis = _DummyRedis()
    retriever = _ValkeyVectorRetriever(
        redis_client=redis,
        index_name="idx",
        key_prefix="fd:",
        embedding_model="unused-local-model",
        embedding_fn=embed,
        dim=3,
    )

    retriever.add_documents_batch(
        ["first doc", "second doc"],
        doc_ids=["doc-1", "doc-2"],
    )
    result = retriever.query(query_texts=["search text"], n_results=5)

    assert calls == ["first doc", "second doc", "search text"]
    assert sorted(redis.hashes) == ["fd:doc-1", "fd:doc-2"]
    assert len(redis.hashes["fd:doc-1"]["embedding"]) == 12
    assert result == {"ids": [[]], "distances": [[]]}


def test_user_workflow_discovery_doc_id_is_tenant_qualified():
    store = _build_store()
    wf_a = _workflow("user.alice.weekly_brief", "Weekly", "ops brief")
    wf_a.metadata = {"tenant_id": "acme"}
    wf_b = _workflow("user.alice.weekly_brief", "Weekly", "finance brief")
    wf_b.metadata = {"tenant_id": "beta"}

    doc_a, _item_a, entry_a = store._build_workflow_item(
        wf_a, tenant_id="acme", scope_keys=["acme:*:*:*"]
    )
    doc_b, _item_b, entry_b = store._build_workflow_item(
        wf_b, tenant_id="beta", scope_keys=["beta:*:*:*"]
    )

    assert doc_a == "workflow:acme:user.alice.weekly_brief"
    assert doc_b == "workflow:beta:user.alice.weekly_brief"
    assert doc_a != doc_b
    assert entry_a["workflow_function_name"] == "workflow_user.alice.weekly_brief"
    assert entry_b["workflow_function_name"] == "workflow_user.alice.weekly_brief"
    assert entry_a["tenant_id"] == "acme"
    assert entry_b["tenant_id"] == "beta"


def test_index_workflows_incremental_skips_user_namespace():
    store = _build_store()
    wf = _workflow("user.acme.brief", "Brief", "desc")
    wf.metadata = {"tenant_id": "acme"}
    registry = _DummyWorkflowRegistry({"user.acme.brief": wf})

    assert store.index_workflows_incremental(["user.acme.brief"], registry) == 0
    assert store._hybrid_retriever.added_batches == []


def test_remove_user_workflow_drops_qualified_and_leftover():
    store = _build_store()
    store._save_manifest = lambda *args, **kwargs: None
    qualified = "workflow:acme:user.acme.brief"
    leftover = "workflow:user.acme.brief"
    store._id_to_entry = {
        qualified: {"type": "workflow"},
        leftover: {"type": "workflow"},
        "workflow:navigate_screenshot": {"type": "workflow"},
    }

    removed = store.remove_user_workflow("user.acme.brief", "acme")

    assert removed == 2
    assert qualified not in store._id_to_entry
    assert leftover not in store._id_to_entry
    assert "workflow:navigate_screenshot" in store._id_to_entry


def test_reconcile_keeps_tenant_user_docs_and_drops_leftover(monkeypatch):
    store = _build_store()
    store._save_manifest = lambda *args, **kwargs: None
    qualified = "workflow:acme:user.acme.brief"
    leftover = "workflow:user.acme.brief"
    store._id_to_entry = {
        qualified: {"type": "workflow", "content_hash": "x"},
        leftover: {"type": "workflow", "content_hash": "y"},
        "workflow:core.ok": {"type": "workflow", "content_hash": "z"},
    }
    monkeypatch.setattr(store, "index_tools_incremental", lambda *_a, **_k: 0)
    monkeypatch.setattr(store, "index_workflows_incremental", lambda *_a, **_k: 0)
    monkeypatch.setattr(store, "index_user_workflows_from_catalog", lambda *_a, **_k: 0)
    monkeypatch.setattr(store, "index_commands_incremental", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        "motet.core.workflow.user_catalog.catalog_user_workflow_discovery_doc_ids",
        lambda *a, **k: [qualified],
    )

    stats = store.reconcile_registry_state(
        tool_names=[],
        workflow_ids=["core.ok", "user.acme.brief"],
        command_types=[],
        tool_registry=_DummyToolRegistry({}),
        workflow_registry=_DummyWorkflowRegistry({}),
    )

    assert leftover not in store._id_to_entry
    assert qualified in store._id_to_entry
    assert "workflow:core.ok" in store._id_to_entry
    assert stats["stale_workflows_removed"] == 1

