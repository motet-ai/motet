"""
Motet - Function discovery keyword ranking tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Guards keyword ranking and vector-index integrity in
    `FunctionDiscoveryVectorStore`.

    Tool `_id_to_entry` records once omitted `description`, so keyword ranking and
    the post-fusion boost matched tokenized tool *names* only. Live effect: the
    query "fetch a web page and return its text content" ranked
    `core.http_get_browser` 54th of 55 while Google Workspace `get_*_content`
    tools swept the top, because those are literal name matches for get + content.

    Command entries historically took `description` only from the data-class
    docstring first line (#194); these tests also pin that indexing reads
    first-class `CommandRegistration.description` (tool-parity).

    These tests pin:
    - entry shape carries description + keywords (all indexing paths)
    - workflow entries carry description + derived keywords (schema v4)
    - stale-shape manifests are rejected so shared indices get rebuilt
    - keyword scoring is IDF-weighted with document-length normalization
    - a full reindex leaves every written document reachable by vector KNN
    - command descriptions come from `CommandRegistration.description`

Dependencies:
    - motet.core.tools.function_discovery_vector_store: store under test
    - pytest: test runner

Usage:
    pytest tests/unit/tools/test_function_discovery_keyword_ranking.py -v

Notes:
    - No Valkey or embedding service is required: the keyword half runs against
      the in-memory entry manifest, and index-lifecycle tests assert the command
      sequence issued to a recording Redis stand-in.
    - Ranking tests call `_rank_by_keywords` rather than reimplementing the
      scoring loop, so a regression in that loop fails them. Verified by mutation:
      removing IDF, length normalization, entry descriptions, the KNN LIMIT, or
      the index drop each fails at least one test.
"""

from __future__ import annotations

import json
from array import array
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from motet.core.commands.command_type_registry import (
    CommandImplementationType,
    CommandRegistration,
    command_type_registry,
)
from motet.core.tools.function_discovery_vector_store import (
    FunctionDiscoveryVectorStore,
    _KeywordIndex,
    _ValkeyVectorRetriever,
)


class _RecordingRedis:
    """Minimal Redis stand-in that records commands issued by the retriever."""

    def __init__(
        self,
        *,
        keys: Optional[List[str]] = None,
        dropindex_raises: bool = False,
        search_rows: int = 0,
    ) -> None:
        self.commands: List[Tuple[Any, ...]] = []
        self.deleted: List[str] = []
        self._keys = list(keys or [])
        self._dropindex_raises = dropindex_raises
        self._search_rows = search_rows

    def execute_command(self, *args: Any) -> Any:
        self.commands.append(args)
        name = args[0]
        if name == "FT.DROPINDEX" and self._dropindex_raises:
            raise RuntimeError("Index with name not found")
        if name == "FT.INFO":
            raise RuntimeError("Index with name not found")  # force FT.CREATE
        if name == "FT.SEARCH":
            rows: List[Any] = [self._search_rows]
            for i in range(self._search_rows):
                rows.extend([f"motet:fd:doc{i}", ["doc_id", f"doc{i}"]])
            return rows
        return "OK"

    def scan(self, cursor: int = 0, match: Any = None, count: Any = None):
        return 0, list(self._keys)

    def delete(self, *keys: Any) -> int:
        self.deleted.extend(keys)
        return len(keys)


def _tool(
    description: str,
    *,
    keywords: Optional[List[str]] = None,
    category: str = "general",
) -> SimpleNamespace:
    return SimpleNamespace(
        description=description,
        category=category,
        keywords=list(keywords or []),
        tool_schema=None,
    )


def _store() -> FunctionDiscoveryVectorStore:
    store = FunctionDiscoveryVectorStore.__new__(FunctionDiscoveryVectorStore)
    store._initialized = True
    store._index_version = 0
    store._id_to_entry = {}
    store._removed_doc_ids = set()
    store._keyword_index_cache = None
    return store


def _index(store: FunctionDiscoveryVectorStore, tools: Dict[str, Any]) -> None:
    for name, tool in tools.items():
        store._build_tool_item(name, tool)
    store._index_version = len(store._id_to_entry)


def _keyword_scores(
    store: FunctionDiscoveryVectorStore, query: str
) -> List[Tuple[float, str]]:
    """
    Rank the indexed corpus for a query using the production keyword ranker.

    Calls `_rank_by_keywords` — the same method `search_functions` uses for the
    keyword half — and maps doc ids to tool names. Deliberately not a
    reimplementation of the scoring loop, so a regression in that loop fails
    these tests.
    """
    expanded = FunctionDiscoveryVectorStore._expand_with_synonyms(
        list(FunctionDiscoveryVectorStore._tokenize_meaningful(query))
    )
    return [
        (score, store._id_to_entry[doc_id]["tool_name"])
        for doc_id, score in store._rank_by_keywords(expanded)
    ]


def _rank_of(scored: List[Tuple[float, str]], name: str) -> Optional[int]:
    for idx, (_score, tool_name) in enumerate(scored, start=1):
        if tool_name == name:
            return idx
    return None


# Mirrors the live catalog shape that produced the failure: one first-party
# content-fetch tool against a family of same-prefix MCP siblings whose *names*
# collide with the query tokens.
_CATALOG = {
    "core.http_get_browser": _tool(
        "RECOMMENDED for reading web pages. Fetch a URL with a real headless "
        "browser and return the readable text content of the page, including "
        "JavaScript-rendered content.",
        keywords=["web", "page", "article", "readable text"],
        category="http",
    ),
    "core.http_get": _tool(
        "Perform a simple HTTP GET request and return the raw response body.",
        keywords=["http", "request"],
        category="http",
    ),
    "mcp.google_workspace.get_doc_content": _tool(
        "Retrieve a Google Docs document by document id.", category="mcp"
    ),
    "mcp.google_workspace.get_page": _tool(
        "Get a Google Chat space page resource by id.", category="mcp"
    ),
    "mcp.google_workspace.get_drive_file_content": _tool(
        "Retrieve a file stored in Google Drive by file id.", category="mcp"
    ),
    "mcp.google_workspace.get_gmail_message_content": _tool(
        "Retrieve the body of a Gmail message by message id.", category="mcp"
    ),
    "mcp.playwright.browser_navigate": _tool(
        "Navigate the browser to a URL for interaction flows.", category="mcp"
    ),
    "mcp.playwright.browser_snapshot": _tool(
        "Capture an accessibility tree snapshot for element targeting.",
        category="mcp",
    ),
    "mcp.playwright.browser_click": _tool(
        "Click an element on the page by accessibility ref.", category="mcp"
    ),
}

_WEB_READ_QUERY = "fetch a web page and return its text content"


class TestEntryShape:
    """A0.1 / A0.3 — description must reach the entry on every indexing path."""

    def test_full_index_records_description_and_keywords(self) -> None:
        store = _store()
        _index(store, {"core.http_get_browser": _CATALOG["core.http_get_browser"]})

        entry = store._id_to_entry["tool:core.http_get_browser"]
        assert entry["description"], "tool entry must carry description"
        assert "readable text" in entry["keywords"]

    def test_every_indexed_tool_entry_has_description(self) -> None:
        """Regression guard: this bug was invisible precisely because nothing asserted it."""
        store = _store()
        _index(store, _CATALOG)

        missing = [
            entry["tool_name"]
            for entry in store._id_to_entry.values()
            if entry.get("type") == "tool" and not entry.get("description")
        ]
        assert missing == []

    def test_incremental_and_bundle_paths_match_full_index(self) -> None:
        """All entry-construction paths route through one helper, so shapes agree."""
        name = "core.http_get_browser"
        tool = _CATALOG[name]

        full = _store()
        _index(full, {name: tool})
        full_entry = full._id_to_entry[f"tool:{name}"]

        helper = _store()
        item = helper._build_tool_item(name, tool, record_entry=False)
        assert item is not None
        assert helper._tool_entry_from_item(item) == full_entry

    def test_description_is_clipped(self) -> None:
        cap = FunctionDiscoveryVectorStore._ENTRY_DESCRIPTION_MAX_CHARS
        store = _store()
        _index(store, {"mcp.verbose.tool": _tool("x" * (cap + 500))})

        entry = store._id_to_entry["tool:mcp.verbose.tool"]
        assert len(entry["description"]) == cap


class TestWorkflowEntryShape:
    """v4 — workflow entries must carry description + keywords like tools."""

    def _navigate_workflow(self) -> SimpleNamespace:
        return SimpleNamespace(
            workflow_id="navigate_screenshot",
            name="Navigate and Screenshot",
            description="Navigate to a URL and take a screenshot of the page.",
            keywords=["website"],
            steps={
                "navigate": SimpleNamespace(
                    command_data={"tool_name": "mcp.playwright.browser_navigate"}
                ),
                "screenshot": SimpleNamespace(
                    command_data={"tool_name": "mcp.playwright.browser_take_screenshot"}
                ),
            },
            metadata={},
        )

    def test_workflow_entry_carries_description_and_derived_keywords(self) -> None:
        store = _store()
        doc_id, _item, entry = store._build_workflow_item(self._navigate_workflow())
        store._id_to_entry[doc_id] = entry

        assert doc_id == "workflow:navigate_screenshot"
        assert "screenshot" in entry["description"]
        assert "playwright" in entry["keywords"]
        assert "browser" in entry["keywords"]
        assert "website" in entry["keywords"]
        text = FunctionDiscoveryVectorStore._entry_searchable_text(entry)
        assert "screenshot" in text
        assert "playwright" in text

    def test_workflow_description_outranks_name_only_sibling(self) -> None:
        store = _store()
        rich = self._navigate_workflow()
        poor = SimpleNamespace(
            workflow_id="other",
            name="Other",
            description="",
            keywords=None,
            steps={},
            metadata={},
        )
        for wf in (rich, poor):
            doc_id, _item, entry = store._build_workflow_item(wf)
            store._id_to_entry[doc_id] = entry
        store._index_version = len(store._id_to_entry)

        expanded = FunctionDiscoveryVectorStore._expand_with_synonyms(
            list(
                FunctionDiscoveryVectorStore._tokenize_meaningful(
                    "navigate to a website and take a screenshot with browser"
                )
            )
        )
        ranked = store._rank_by_keywords(expanded)
        names = [
            store._id_to_entry[doc_id]["workflow_function_name"]
            for doc_id, _score in ranked
        ]
        assert names[0] == "workflow_navigate_screenshot"


class TestManifestSchemaVersion:
    """A0 — stale-shape manifests must be rebuilt, not loaded."""

    def _write(self, tmp_path, payload: Dict[str, Any]):
        store = _store()
        store.persist_dir = str(tmp_path)
        store.embedding_model = "test-model"
        store._manifest_path = str(tmp_path / "function_discovery_manifest.json")
        # Stubbed so a load failure can only come from the version check itself —
        # otherwise the real retriever raises (no Valkey) and the load returns
        # False regardless, letting these tests pass for the wrong reason.
        store._initialize_hybrid_retriever = lambda: None  # type: ignore[method-assign]
        with open(store._manifest_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return store

    def test_rejects_manifest_without_entry_schema_version(self, tmp_path) -> None:
        """Pre-fix manifests have no version key and description-less entries."""
        store = self._write(
            tmp_path,
            {
                "embedding_model": "test-model",
                "index_version": 1,
                "id_to_entry": {
                    "tool:core.http_get_browser": {
                        "type": "tool",
                        "tool_name": "core.http_get_browser",
                        "content_hash": "abc",
                    }
                },
            },
        )
        assert store._load_existing_index() is False

    def test_accepts_current_entry_schema_version(self, tmp_path) -> None:
        store = self._write(
            tmp_path,
            {
                "embedding_model": "test-model",
                "entry_schema_version": FunctionDiscoveryVectorStore._ENTRY_SCHEMA_VERSION,
                "index_version": 1,
                "id_to_entry": {
                    "tool:core.http_get_browser": {
                        "type": "tool",
                        "tool_name": "core.http_get_browser",
                        "description": "Fetch a URL with a browser.",
                        "content_hash": "abc",
                    }
                },
            },
        )
        assert store._load_existing_index() is True
        assert store._id_to_entry["tool:core.http_get_browser"]["description"]

    def test_saved_manifest_stamps_version(self, tmp_path) -> None:
        store = _store()
        store.embedding_model = "test-model"
        store._manifest_path = str(tmp_path / "m.json")
        _index(store, {"core.http_get": _CATALOG["core.http_get"]})
        store._save_manifest()

        payload = json.load(open(store._manifest_path, encoding="utf-8"))
        assert (
            payload["entry_schema_version"]
            == FunctionDiscoveryVectorStore._ENTRY_SCHEMA_VERSION
        )


class TestKeywordRanking:
    """A0 / A6 — the live failure must not reproduce."""

    def test_web_read_intent_ranks_content_fetch_tool_first(self) -> None:
        store = _store()
        _index(store, _CATALOG)

        scored = _keyword_scores(store, _WEB_READ_QUERY)
        assert scored, "query should match something"
        assert scored[0][1] == "core.http_get_browser", (
            "content-fetch tool must outrank name-colliding siblings; "
            f"got {[n for _s, n in scored[:5]]}"
        )

    def test_content_fetch_outranks_every_workspace_name_collision(self) -> None:
        """
        `get_page` / `get_*_content` are literal name matches for get + page +
        content — the exact collision that beat `core.http_get_browser` live.

        This asserts relative order, not a per-family quota: capping hits per MCP
        server is diversification (deferred), and this fix is not it.
        """
        store = _store()
        _index(store, _CATALOG)
        scored = _keyword_scores(store, _WEB_READ_QUERY)

        target = _rank_of(scored, "core.http_get_browser")
        assert target is not None
        workspace_ranks = [
            rank
            for rank, (_score, name) in enumerate(scored, start=1)
            if name.startswith("mcp.google_workspace.")
        ]
        assert workspace_ranks, "workspace siblings should still be recalled"
        assert target < min(workspace_ranks), (
            f"http_get_browser at {target} must outrank all workspace tools "
            f"{workspace_ranks}"
        )

    def test_description_is_what_makes_the_difference(self) -> None:
        """Strip descriptions (pre-fix shape) and the correct tool falls away."""
        store = _store()
        _index(store, _CATALOG)
        for entry in store._id_to_entry.values():
            entry["description"] = ""
            entry["keywords"] = []
        store._keyword_index_cache = None

        scored = _keyword_scores(store, _WEB_READ_QUERY)
        assert _rank_of(scored, "core.http_get_browser") != 1, (
            "name-only matching should NOT rank it first — if it does, this test "
            "no longer demonstrates the regression it guards"
        )

    def test_ranker_applies_idf_not_raw_hit_counts(self) -> None:
        """
        One rare term must outweigh several ubiquitous ones.

        Built so raw hit counting gives the wrong answer: the decoy matches two
        query tokens that appear in nearly every document, while the target
        matches only the single token unique to it. Counting hits ranks the decoy
        first; weighting by IDF ranks the target first.
        """
        catalog = {
            f"bulk.entry_{i:02d}": _tool("Handles shared common workload records")
            for i in range(18)
        }
        catalog["decoy.two_generic_hits"] = _tool("Handles shared common workload")
        catalog["target.one_rare_hit"] = _tool("Handles zebrafish workload records")

        store = _store()
        _index(store, catalog)
        scored = _keyword_scores(store, "shared common zebrafish")

        target = _rank_of(scored, "target.one_rare_hit")
        decoy = _rank_of(scored, "decoy.two_generic_hits")
        assert target is not None and decoy is not None
        assert target < decoy, (
            "rare-term match must outrank two generic matches; "
            f"target={target} decoy={decoy}"
        )

    def test_idf_discounts_terms_shared_across_a_family(self) -> None:
        store = _store()
        _index(store, _CATALOG)
        ki = store._get_keyword_index()

        # "browser" spans the Playwright family plus http_get_browser; "readable"
        # appears once. The rare term must carry more weight.
        assert ki.idf("readable") > ki.idf("browser")

    def test_length_normalization_penalizes_verbose_docs(self) -> None:
        store = _store()
        _index(store, _CATALOG)
        ki = store._get_keyword_index()

        assert ki.length_norm(3) > ki.length_norm(300)

    def test_keywords_are_searchable(self) -> None:
        store = _store()
        _index(store, _CATALOG)

        scored = _keyword_scores(store, "readable article")
        assert scored[0][1] == "core.http_get_browser"


class TestKeywordIndexCache:
    def test_cache_reused_when_entries_unchanged(self) -> None:
        store = _store()
        _index(store, _CATALOG)
        assert store._get_keyword_index() is store._get_keyword_index()

    def test_cache_invalidated_when_entries_change(self) -> None:
        store = _store()
        _index(store, _CATALOG)
        first = store._get_keyword_index()

        _index(store, {"core.new_tool": _tool("A brand new capability.")})
        second = store._get_keyword_index()

        assert second is not first
        assert second.n_docs == first.n_docs + 1

    def test_empty_corpus_is_safe(self) -> None:
        store = _store()
        ki = store._get_keyword_index()
        assert ki.n_docs == 0
        assert ki.length_norm(0) == 1.0
        assert ki.idf("anything") == 0.0


class TestVectorIndexLifecycle:
    """
    Documents written by a full reindex must be reachable by KNN.

    Live symptom: every built-in `core.*` tool was written and counted in
    FT.INFO num_docs with zero indexing failures, yet KNN over 313 documents
    returned only 143 — the `core.*` population was silently unreachable by
    semantic search because the mass delete in clear_all() was still being
    applied when the same keys were rewritten.
    """

    def _retriever(self, redis: Any) -> _ValkeyVectorRetriever:
        r = _ValkeyVectorRetriever.__new__(_ValkeyVectorRetriever)
        r._redis = redis
        r._index_name = "imf_function_discovery_idx"
        r._key_prefix = "motet:fd:"
        r._dim = 4
        return r

    def test_clear_all_drops_index_before_deleting_and_recreates_after(self) -> None:
        redis = _RecordingRedis(keys=["motet:fd:tool:a", "motet:fd:tool:b"])
        self._retriever(redis).clear_all()

        commands = [c[0] for c in redis.commands]
        assert "FT.DROPINDEX" in commands, "index must be dropped to discard pending deletes"
        assert "FT.CREATE" in commands, "empty index must be recreated"
        assert commands.index("FT.DROPINDEX") < commands.index("FT.CREATE")
        assert redis.deleted, "keys must still be removed"

    def test_clear_all_tolerates_missing_index(self) -> None:
        redis = _RecordingRedis(keys=[], dropindex_raises=True)
        self._retriever(redis).clear_all()  # first run has no index yet

    def test_count_searchable_documents_uses_unit_probe_not_zeros(self) -> None:
        """Cosine distance against a zero-norm vector is undefined."""
        redis = _RecordingRedis(keys=[], search_rows=3)
        r = self._retriever(redis)
        assert r.count_searchable_documents(limit=50) == 3

        search = next(c for c in redis.commands if c[0] == "FT.SEARCH")
        vec_idx = list(search).index("vec") + 1
        values = list(array("f", search[vec_idx]))
        assert any(v != 0.0 for v in values)

    def test_knn_command_sets_explicit_limit(self) -> None:
        """FT.SEARCH returns 10 rows by default, truncating the fusion pool."""
        r = self._retriever(_RecordingRedis(keys=[]))
        args = r._build_knn_search_command(n_results=60, vec_bytes=b"abc")

        assert "KNN 60" in args[2]
        limit_idx = args.index("LIMIT")
        assert args[limit_idx + 1 : limit_idx + 3] == ["0", "60"]


class TestCommandDescriptionSource:
    """#194 — indexer reads first-class CommandRegistration.description (tool-parity)."""

    def test_uses_registration_description_field(self, monkeypatch) -> None:
        class ThinData(BaseModel):
            """Data payload for example operations."""

            query: str = Field(description="Search query")

        class DecoratedCommand:
            """Dynamically generated command from decorated function."""

        registration = CommandRegistration(
            command_type="test.rich_desc_command",
            implementation=DecoratedCommand,
            implementation_type=CommandImplementationType.DECORATOR_BASED,
            data_class=ThinData,
            description="Store content in distributed memory with tenant isolation.",
        )
        monkeypatch.setattr(
            command_type_registry,
            "get",
            lambda ct, **_kw: registration if ct == "test.rich_desc_command" else None,
        )

        store = _store()
        _doc_id, item, entry = store._build_command_item("test.rich_desc_command")

        expected = "Store content in distributed memory with tenant isolation."
        assert entry is not None
        assert entry["description"] == expected
        assert item is not None
        assert expected in item.content
        assert "Data payload for example operations." not in item.content

    def test_falls_back_to_derive_when_registration_description_empty(
        self, monkeypatch
    ) -> None:
        class ThinData(BaseModel):
            """Data payload for example operations."""

            query: str = Field(description="Search query")

        def rich_impl(data: ThinData) -> dict:
            """Store content in distributed memory with tenant isolation."""
            return {}

        class DecoratedCommand:
            """Dynamically generated command from decorated function."""

            _original_function = staticmethod(rich_impl)

        registration = CommandRegistration(
            command_type="test.derive_desc_command",
            implementation=DecoratedCommand,
            implementation_type=CommandImplementationType.DECORATOR_BASED,
            data_class=ThinData,
            description="",
        )
        monkeypatch.setattr(
            command_type_registry,
            "get",
            lambda ct, **_kw: registration if ct == "test.derive_desc_command" else None,
        )

        store = _store()
        _doc_id, _item, entry = store._build_command_item("test.derive_desc_command")

        assert entry is not None
        assert entry["description"].startswith("Store content in distributed memory")

    def test_clips_long_registration_description(self, monkeypatch) -> None:
        class ThinData(BaseModel):
            """Data payload for example operations."""

            query: str = Field(default="")

        long_line = "x" * (FunctionDiscoveryVectorStore._ENTRY_DESCRIPTION_MAX_CHARS + 50)

        class DecoratedCommand:
            """Dynamically generated command from decorated function."""

        registration = CommandRegistration(
            command_type="test.long_desc_command",
            implementation=DecoratedCommand,
            implementation_type=CommandImplementationType.DECORATOR_BASED,
            data_class=ThinData,
            description=long_line,
        )
        monkeypatch.setattr(
            command_type_registry,
            "get",
            lambda ct, **_kw: registration if ct == "test.long_desc_command" else None,
        )

        store = _store()
        _doc_id, _item, entry = store._build_command_item("test.long_desc_command")

        assert entry is not None
        assert len(entry["description"]) == FunctionDiscoveryVectorStore._ENTRY_DESCRIPTION_MAX_CHARS


class TestIdfEdgeCases:
    def test_ubiquitous_term_still_scores_above_zero(self) -> None:
        """A term in every document floors at a small positive weight, never negative."""
        store = _store()
        _index(
            store,
            {
                "a.one": _tool("common widget alpha"),
                "a.two": _tool("common widget beta"),
                "a.three": _tool("common widget gamma"),
            },
        )
        ki = store._get_keyword_index()
        assert 0.0 < ki.idf("common") < ki.idf("alpha")

    def test_unknown_term_scores_highest(self) -> None:
        store = _store()
        _index(store, _CATALOG)
        ki = store._get_keyword_index()
        assert ki.idf("zzzznotpresent") > ki.idf("browser")

    def test_idf_stems_its_argument(self) -> None:
        """
        Document frequencies are keyed by stemmed token. An unstemmed argument
        must not be mistaken for a rare term (`shared` stems to `shar`).
        """
        store = _store()
        _index(
            store,
            {
                "a.one": _tool("shared resource"),
                "a.two": _tool("shared handle"),
                "a.three": _tool("shared pool"),
            },
        )
        ki = store._get_keyword_index()
        assert ki.idf("shared") == ki.idf("shar")
        assert ki.idf("shared") < ki.idf("pool")
