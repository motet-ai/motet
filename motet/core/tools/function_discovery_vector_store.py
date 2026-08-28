"""
Motet - Function Discovery Vector Store

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Vector store for discovering functions for native LLM function calling.
    Provides hybrid search (Valkey vector + app-layer keyword fusion) across tools, workflows,
    and distributed commands. All discoverable items (built-in tools, MCP tools,
    workflows, and command types) are indexed in a unified collection for efficient
    hybrid search.

    Uses Valkey Search vector retrieval plus application-layer keyword ranking (RRF fusion)
    to preserve hybrid behavior.

    Custom tokenization: Tool names are pre-processed (underscores replaced with spaces)
    during indexing to improve keyword matching (e.g., "list_docs_in_folder" → "list docs in folder").

    Keyword half: BM25-style scoring (IDF + document-length normalization) over the
    persisted entry manifest, which carries each item's name, description, and keywords.
    Workflow entries carry the same description/keywords fields as tools (schema v4);
    keywords come from ``workflow_discovery_keywords`` (author tags plus step tool
    tokens). Entry shape is versioned (`_ENTRY_SCHEMA_VERSION`) because entry
    changes do not alter document content hashes, so a stale manifest must be
    rejected rather than incrementally reconciled.

    Command entry descriptions come from ``CommandRegistration.description`` (set at
    register time, tool-parity; #194). Full reindex and incremental command indexing
    share `_build_command_item`.

    Cross-worker coordination (#156): the index is shared, and rebuilding it is
    destructive — it drops the index and repopulates from the calling worker's
    registry. The manifest therefore lives in Redis next to the index it describes
    (the copy under persist_dir is a per-container cache; workers share no
    filesystem), incremental publishes merge rather than replace, and
    `ensure_shared_index()` serializes rebuilds so at most one worker rebuilds
    while the others adopt what it published.

    Custom boosting: Post-processes results to boost exact matches and matches whose
    IDF-weighted coverage of the original query terms is high.

    Synonym clusters (browse↔browser, read↔get/fetch, …) are owned here and reused for:
    - hybrid-search keyword boosting
    - semantic-name overlap checks (agentic_loop safety net)
    - lexical registry preselect fallback when semantic hits lack query overlap

    Conversation context enhancement: When conversation_history is provided, the search query
    is enhanced with context extracted from recent messages, including:
    - Tool names from recent tool calls (e.g., "gmail" from "mcp.google_workspace.get_gmail")
    - Domain/service keywords (e.g., "gmail", "slack", "google" from message content)
    - This improves tool selection accuracy by incorporating conversation context into semantic search.

    User workflows (``user.*``) are indexed as ``workflow:{tenant_id}:{id}``
    from the Redis catalog so two tenants may share the same callable name.
    Core/bundle workflows stay ``workflow:{id}``.

    Terminology: "Functions" refers to the LLM's perspective - tools and workflows
    are both exposed as callable functions via native function calling.

Dependencies:
    - Valkey/Redis Search: Shared vector retrieval backend
    - MemoryItem: Type for vector store items
    - ToolRegistry: For accessing tool definitions
    - WorkflowRegistry: For accessing workflow definitions
    - structlog: Structured logging

Usage:
    from motet.core.tools.function_discovery_vector_store import FunctionDiscoveryVectorStore
    from motet.core.tools.registry import ToolRegistry
    from motet.core.workflow import WorkflowRegistry
    
    # Create store
    store = FunctionDiscoveryVectorStore(persist_dir="/shared/volumes/function_discovery")
    
    # Index tools and workflows
    tool_registry = ToolRegistry()
    workflow_registry = WorkflowRegistry
    store.index_tools_and_workflows(tool_registry, workflow_registry)
    
    # Search for functions (all types: tools, workflows, commands)
    results = store.search_functions("get weather data", top_k=10)
    for result in results:
        print(f"{result['type']}: {result['name']}")
    
    # Search with conversation context (enhances query with recent tool calls and domain keywords)
    from motet.core.types import Message
    history = [Message(role="user", content="I need to check my Gmail")]
    results = store.search_functions("get recent messages", top_k=10, conversation_history=history)
    
    # Search only tools and workflows (exclude commands) - useful for tool discovery
    results = store.search_functions("get gmail messages", top_k=10, search_types=["tool", "workflow"])
    
    # Search only commands - useful for command discovery
    results = store.search_functions("reset memory", top_k=10, search_types=["command"])

Notes:
    - Uses Valkey Search for shared vector retrieval
    - Indexes both tools and workflows in unified collection
    - Pre-processes tool names (underscores → spaces) for better keyword matching
    - Post-processes results to boost exact matches and keyword-rich matches
    - Includes MCP tools automatically (registered in same ToolRegistry)
    - Handles lazy initialization and re-indexing on updates
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple, cast
from array import array
import base64
import gzip
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
import structlog

from ..registry import ScopeGrant
from ..types import MemoryItem, Message

logger = structlog.get_logger(__name__)


class _ValkeyVectorRetriever:
    """Vector retriever backed by Valkey Search (FT.CREATE/FT.SEARCH over HASH keys)."""

    def __init__(
        self,
        *,
        redis_client: Any,
        index_name: str,
        key_prefix: str,
        embedding_model: str,
        embedding_fn: Optional[Any] = None,
        dim: int = 384,
    ) -> None:
        self._redis = redis_client
        self._index_name = index_name
        self._key_prefix = key_prefix
        self._dim = dim
        self._embedding_model = embedding_model
        self._embedding_fn = embedding_fn
        self._embedder = None
        self._ensure_index()

    def _ensure_index(self) -> None:
        try:
            self._redis.execute_command("FT.INFO", self._index_name)
            return
        except Exception:
            pass  # index check best-effort; fall through to create
        self._redis.execute_command(
            "FT.CREATE",
            self._index_name,
            "ON",
            "HASH",
            "PREFIX",
            "1",
            self._key_prefix,
            "SCHEMA",
            "doc_id",
            "TAG",
            "embedding",
            "VECTOR",
            "HNSW",
            "10",
            "TYPE",
            "FLOAT32",
            "DIM",
            str(self._dim),
            "DISTANCE_METRIC",
            "COSINE",
            "M",
            "16",
            "EF_CONSTRUCTION",
            "200",
        )

    @staticmethod
    def _to_float32_bytes(values: List[float]) -> bytes:
        buf = array("f", [float(v) for v in values])
        return buf.tobytes()

    def _build_knn_search_command(self, *, n_results: int, vec_bytes: bytes) -> List[Any]:
        """
        Build FT.SEARCH command arguments for Valkey vector KNN queries.

        Keeping command assembly in one place prevents query-shape drift across
        call sites and simplifies regression testing for backend compatibility.

        The explicit LIMIT is required: FT.SEARCH defaults to returning 10 rows,
        so a KNN of 60 still yielded 10 candidates and silently truncated the
        fusion pool no matter what the caller asked for.
        """
        limit = max(int(n_results), 1)
        return [
            "FT.SEARCH",
            self._index_name,
            f"*=>[KNN {limit} @embedding $vec AS vector_distance]",
            "PARAMS",
            "2",
            "vec",
            vec_bytes,
            "RETURN",
            "2",
            "doc_id",
            "vector_distance",
            "LIMIT",
            "0",
            str(limit),
            "DIALECT",
            "2",
        ]

    def add_documents_batch(
        self,
        documents: List[str],
        *,
        doc_ids: List[str],
        mode: str = "unified",
        show_progress: bool = False,
    ) -> None:
        if not documents:
            return
        embeddings = [self._embed_text(document) for document in documents]
        for idx, doc_id in enumerate(doc_ids):
            key = f"{self._key_prefix}{doc_id}"
            vector_bytes = self._to_float32_bytes(embeddings[idx])
            self._redis.hset(
                key,
                mapping={
                    "doc_id": str(doc_id),
                    "content": str(documents[idx]),
                    "embedding": vector_bytes,
                },
            )

    def remove_documents_batch(self, doc_ids: List[str]) -> None:
        if not doc_ids:
            return
        keys = [f"{self._key_prefix}{doc_id}" for doc_id in doc_ids]
        self._redis.delete(*keys)

    def clear_all(self) -> None:
        """
        Empty the index and its keyspace, leaving a freshly created empty index.

        The index is dropped *before* the keys are deleted, then recreated. Valkey
        Search applies key deletions to the vector graph asynchronously, so a full
        reindex that deleted every key and immediately rewrote the same keys raced
        its own pending deletions: the rewritten documents were counted in
        num_docs (and reported zero indexing failures) but were never inserted
        into the HNSW graph, making them permanently unreachable by KNN while
        looking healthy. Dropping the index first discards that pending work.
        """
        try:
            self._redis.execute_command("FT.DROPINDEX", self._index_name)
        except Exception as e:
            # Absent index is the expected case on first run; anything else is
            # still non-fatal because _ensure_index recreates below.
            logger.debug(
                "function_discovery_dropindex_skipped",
                index=self._index_name,
                error=str(e),
            )

        cursor = 0
        keys: List[str] = []
        while True:
            cursor, batch = self._redis.scan(cursor=cursor, match=f"{self._key_prefix}*", count=500)
            if batch:
                keys.extend(batch)
            if cursor == 0:
                break
        if keys:
            self._redis.delete(*keys)

        self._ensure_index()

    def count_searchable_documents(self, *, limit: int = 1000) -> int:
        """
        Number of documents reachable by vector KNN.

        Distinct from FT.INFO num_docs, which counts documents known to the index
        whether or not they made it into the vector graph. Used to detect silent
        partial indexing after a rebuild.
        """
        # Unit vector, not zeros: cosine distance against a zero-norm vector is
        # undefined and the backend may reject the query outright.
        dim = max(int(self._dim), 1)
        probe_values = [0.0] * dim
        probe_values[0] = 1.0
        probe = self._to_float32_bytes(probe_values)
        try:
            response = self._redis.execute_command(
                *self._build_knn_search_command(n_results=limit, vec_bytes=probe)
            )
        except Exception as e:
            logger.warning(
                "function_discovery_searchable_count_failed",
                index=self._index_name,
                error=str(e),
                error_type=type(e).__name__,
            )
            return -1
        if not isinstance(response, list) or not response:
            return -1
        return max((len(response) - 1) // 2, 0)

    def query(
        self,
        *,
        query_texts: List[str],
        n_results: int,
        include: Optional[List[str]] = None,
        bm25_ratio: float = 0.5,
    ) -> Dict[str, List[List[Any]]]:
        if not query_texts:
            return {"ids": [[]], "distances": [[]]}
        query_vec = self._embed_text(query_texts[0])
        vec_bytes = self._to_float32_bytes(query_vec)
        command_args = self._build_knn_search_command(n_results=n_results, vec_bytes=vec_bytes)
        resp = self._redis.execute_command(*command_args)

        ids: List[str] = []
        distances: List[float] = []
        if not resp or len(resp) < 2:
            return {"ids": [ids], "distances": [distances]}
        # RESP structure: [total, key1, [field,val,...], key2, [field,val,...], ...]
        for i in range(1, len(resp), 2):
            if i + 1 >= len(resp):
                break
            fields = resp[i + 1]
            if not isinstance(fields, list):
                continue
            field_map: Dict[str, Any] = {}
            for j in range(0, len(fields), 2):
                if j + 1 >= len(fields):
                    break
                k = fields[j]
                v = fields[j + 1]
                key = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
                field_map[key] = v
            doc_id = field_map.get("doc_id")
            if isinstance(doc_id, (bytes, bytearray)):
                doc_id = doc_id.decode()
            if not doc_id:
                continue
            raw_dist = field_map.get("vector_distance", 1.0)
            if isinstance(raw_dist, (bytes, bytearray)):
                raw_dist = raw_dist.decode()
            ids.append(str(doc_id))
            distances.append(float(raw_dist))
        return {"ids": [ids], "distances": [distances]}

    def _embed_text(self, text: str) -> List[float]:
        """Embed text through the injected embedding function or local fallback."""

        if self._embedding_fn is not None:
            embedding = self._embedding_fn(text)
            embedding_list = embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
            return [float(value) for value in embedding_list]

        if self._embedder is None:
            from sentence_transformers import SentenceTransformer  # type: ignore

            self._embedder = SentenceTransformer(self._embedding_model)
        embedding = self._embedder.encode([text], convert_to_numpy=True)[0].tolist()
        return [float(value) for value in embedding]


class _KeywordIndex:
    """
    Tokenized corpus + IDF table for the keyword half of hybrid search.

    Built from ``_id_to_entry`` and cached until the entry map changes. Exists so
    keyword ranking can weight terms by discriminative power instead of counting
    raw hits: a token present in every sibling of a large MCP family contributes
    almost nothing, while a rare token dominates.
    """

    __slots__ = (
        "doc_tokens",
        "n_docs",
        "avgdl",
        "fingerprint",
        "_df",
        "_idf_cache",
        "_k1",
        "_b",
        "_normalize",
    )

    def __init__(
        self,
        doc_tokens: Dict[str, set],
        df: Dict[str, int],
        fingerprint: Tuple[Any, ...],
        *,
        k1: float,
        b: float,
        normalize: Callable[[str], str],
    ) -> None:
        self.doc_tokens = doc_tokens
        self._df = df
        self.fingerprint = fingerprint
        self.n_docs = len(doc_tokens)
        total_len = sum(len(tokens) for tokens in doc_tokens.values())
        self.avgdl = (total_len / float(self.n_docs)) if self.n_docs else 0.0
        self._idf_cache: Dict[str, float] = {}
        self._k1 = k1
        self._b = b
        self._normalize = normalize

    def idf(self, token: str) -> float:
        """
        Robertson-Sparck-Jones IDF, floored so a ubiquitous term still scores > 0.

        The token is stemmed before lookup. Document frequencies are keyed by
        stemmed token, so an unstemmed argument would miss the table and be
        treated as maximally rare — silently inverting its weight.
        """
        cached = self._idf_cache.get(token)
        if cached is not None:
            return cached
        if self.n_docs <= 0:
            value = 0.0
        else:
            df = self._df.get(self._normalize(token), 0)
            value = max(
                math.log(1.0 + ((self.n_docs - df + 0.5) / (df + 0.5))), 0.01
            )
        self._idf_cache[token] = value
        return value

    def length_norm(self, doc_len: int) -> float:
        """BM25 length normalization for a set-based (tf = 1) match."""
        if self.avgdl <= 0:
            return 1.0
        denom = 1.0 + self._k1 * (
            1.0 - self._b + self._b * (float(doc_len) / self.avgdl)
        )
        return (self._k1 + 1.0) / denom


class FunctionDiscoveryVectorStore:
    """
    Vector store for discovering functions for native LLM function calling (ADR-0051).
    
    Provides semantic search across tools and workflows that will be exposed as
    callable functions to the LLM. All discoverable items (built-in tools, MCP tools,
    and workflows) are indexed in a unified collection for efficient semantic search.
    
    Terminology: "Functions" refers to the LLM's perspective - tools and workflows
    are both exposed as callable functions via native function calling (ADR-0045).
    """
    _COMMON_QUERY_WORDS = {
        "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of", "with",
        "by", "from", "is", "are", "was", "were", "be", "this", "that", "it", "as",
    }
    # Shape version for `_id_to_entry` records persisted in the manifest.
    # Bump when the entry payload changes so existing shared manifests are
    # rebuilt instead of loaded with a stale shape. v2 added `description` /
    # `keywords` to tool entries; before that the keyword half of hybrid search
    # matched tool *names* only. v3 prefers command implementation docstrings
    # over data-class docs for entry `description` (#194). v4 adds the same
    # description/keywords fields to workflow entries so composed workflows
    # (navigate_screenshot) are not name-only on the keyword half.
    _ENTRY_SCHEMA_VERSION = 4

    # Cap on description text copied into an entry for keyword matching. Some MCP
    # servers ship multi-KB descriptions; the manifest is loaded by every worker
    # at startup, and BM25 length normalization penalizes long docs anyway.
    _ENTRY_DESCRIPTION_MAX_CHARS = 1000

    # BM25 parameters for the keyword half. Term frequency is set-based (0/1) per
    # document, so k1 only shapes the length-normalization curve.
    _BM25_K1 = 1.2
    _BM25_B = 0.75

    # Credit for a prefix/stem match relative to an exact token match.
    _PREFIX_MATCH_WEIGHT = 0.35

    # Minimum candidate pool handed to reciprocal-rank fusion, independent of
    # top_k. Fusion quality depends on both halves proposing real candidates.
    _MIN_FUSION_POOL = 60

    # Optimistic-concurrency retries when merging into the shared manifest.
    _MANIFEST_PUBLISH_ATTEMPTS = 5

    # RRF smoothing constant. The customary 60 is tuned for web-scale result
    # lists; on a few-hundred-document function index it flattens rank
    # differences so much that appearing mid-pack in both halves beats ranking
    # first in one. A smaller k restores discrimination at this corpus size.
    _RRF_K = 12.0

    # Generic synonym clusters for lightweight expansion in relevance boosting.
    _SYNONYM_CLUSTERS = [
        {"browse", "browser", "visit", "open", "navigate"},
        {"read", "get", "fetch", "retrieve", "lookup"},
        {"search", "find", "discover", "lookup"},
        {"news", "headline", "headlines", "article", "articles"},
        {"url", "website", "web", "webpage", "site"},
    ]
    
    def __init__(
        self,
        persist_dir: Optional[str] = None,
        embedding_model: str = "sentence-transformers/all-MiniLM-L12-v2",
        *,
        embedding_fn: Optional[Any] = None,
        embedding_dim: Optional[int] = None,
        enable_embedding_cache: bool = True,
        enable_result_cache: bool = True,
    ):
        """
        Initialize function discovery vector store.
        
        Args:
            persist_dir: Directory for manifest persistence (shared across workers)
            embedding_model: Embedding model for semantic search (default: all-MiniLM-L12-v2, standard across stack)
            embedding_fn: Optional embedding function, usually `EmbeddingService.embed`.
            embedding_dim: Dimension for embeddings returned by `embedding_fn`.
            enable_embedding_cache: Cache embeddings for performance
            enable_result_cache: Cache search results for common queries
        
        Note:
            Changing the embedding model requires reindexing to get the benefits of the new model.
            Set MOTET_FORCE_REINDEX_FUNCTIONS=true or clear the persist_dir to force reindexing.
        """
        self.backend = "valkey"
        self._valkey_index_name = os.getenv("MOTET_FUNCTION_DISCOVERY_VALKEY_INDEX", "imf_function_discovery_idx")
        self._valkey_key_prefix = os.getenv("MOTET_FUNCTION_DISCOVERY_VALKEY_PREFIX", "motet:fd:")

        if not persist_dir:
            persist_dir = os.getenv("MOTET_FUNCTION_DISCOVERY_PERSIST_DIR", "/tmp/imf_function_discovery_shared")
            logger.warning(
                "function_discovery_persist_dir_defaulted",
                persist_dir=persist_dir,
                note="persist_dir was None; defaulting to shared local path",
            )

        # Ensure directory exists (best-effort)
        try:
            Path(persist_dir).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(
                "function_discovery_persist_dir_create_failed",
                persist_dir=persist_dir,
                error=str(e),
                error_type=type(e).__name__,
                note="Continuing; manifest persistence may fail if directory is unusable",
                exc_info=True,
            )

        self.persist_dir = persist_dir
        self.embedding_model = embedding_model
        self._embedding_fn = embedding_fn
        self._embedding_dim = int(embedding_dim or 384)
        self._initialized = False
        self._index_version = None
        self._keyword_index_cache: Optional["_KeywordIndex"] = None

        self._manifest_path = os.path.join(
            self.persist_dir,
            os.getenv("MOTET_FUNCTION_DISCOVERY_MANIFEST_FILE", "function_discovery_manifest.json"),
        )
        # Authoritative manifest location. Workers do not share persist_dir — it
        # is a per-container /tmp path — so the file above is only a local cache
        # (#156).
        self._shared_manifest_key = os.getenv(
            "MOTET_FUNCTION_DISCOVERY_MANIFEST_REDIS_KEY",
            "motet:function_discovery:manifest",
        )

        # In-memory mapping from stored doc_id -> tool/workflow entry.
        self._id_to_entry: Dict[str, Dict[str, Any]] = {}
        # Doc ids this worker removed since its last publish, so a merge does not
        # resurrect them from the shared copy.
        self._removed_doc_ids: Set[str] = set()

        # Hard requirement: Valkey Search backend must be available.
        from ..distributed.redis_manager import get_sync_redis_client
        self._valkey_redis = get_sync_redis_client("function_discovery_valkey")

        # Initialize hybrid retriever lazily during indexing
        self._hybrid_retriever = None
        
        logger.info(
            "function_discovery_vector_store_initialized",
            persist_dir=persist_dir,
            embedding_model=embedding_model,
            embedding_dim=self._embedding_dim,
            embedding_source="injected" if embedding_fn is not None else "local",
            backend=self.backend,
        )

    def _initialize_hybrid_retriever(self) -> None:
        """Initialize Valkey Search retriever."""
        self._hybrid_retriever = _ValkeyVectorRetriever(
            redis_client=self._valkey_redis,
            index_name=self._valkey_index_name,
            key_prefix=self._valkey_key_prefix,
            embedding_model=self.embedding_model,
            embedding_fn=self._embedding_fn,
            dim=self._embedding_dim,
        )

    def _manifest_payload(self, entries: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Manifest document describing a set of indexed entries."""
        return {
            "embedding_model": getattr(self, "embedding_model", ""),
            "entry_schema_version": self._ENTRY_SCHEMA_VERSION,
            "index_name": getattr(self, "_valkey_index_name", ""),
            "index_version": len(entries),
            "id_to_entry": entries,
        }

    @staticmethod
    def _encode_manifest(payload: Dict[str, Any]) -> str:
        """
        Serialize a manifest for Redis: JSON, gzipped, base64.

        The shared Redis client decodes responses to str, so the value has to be
        text. A live manifest is ~160 KB of JSON and every worker reads it at
        startup and again on each incremental publish, so it is compressed.
        """
        raw = json.dumps(payload, sort_keys=True).encode("utf-8")
        return base64.b64encode(gzip.compress(raw, compresslevel=6)).decode("ascii")

    @staticmethod
    def _decode_manifest(blob: Any) -> Optional[Dict[str, Any]]:
        """Inverse of _encode_manifest; None when the value is absent or unreadable."""
        if not blob:
            return None
        try:
            data = blob.encode("ascii") if isinstance(blob, str) else bytes(blob)
            payload = json.loads(gzip.decompress(base64.b64decode(data)).decode("utf-8"))
            return payload if isinstance(payload, dict) else None
        except Exception as e:
            logger.warning(
                "function_discovery_shared_manifest_decode_failed",
                error=str(e),
                error_type=type(e).__name__,
            )
            return None

    def _read_shared_manifest(self) -> Optional[Dict[str, Any]]:
        """Fetch the manifest published in Redis, or None when absent/unavailable."""
        redis_client = getattr(self, "_valkey_redis", None)
        key = getattr(self, "_shared_manifest_key", None)
        if redis_client is None or not key:
            return None
        try:
            from motet.core.distributed.tenant_keys import product_key

            return self._decode_manifest(redis_client.get(product_key(str(key))))
        except Exception as e:
            logger.warning(
                "function_discovery_shared_manifest_read_failed",
                key=key,
                error=str(e),
                error_type=type(e).__name__,
            )
            return None

    def _manifest_is_current(self, payload: Optional[Dict[str, Any]]) -> bool:
        """
        Whether a manifest describes an index this worker can use as-is.

        Guards the embedding model (vectors are not comparable across models),
        the entry schema version (entry changes do not alter document content
        hashes, so a stale shape would look unchanged to incremental sync
        forever), and the index name (a reconfigured index is a different one).
        """
        if not payload:
            return False
        stored_model = str(payload.get("embedding_model") or "")
        if stored_model and stored_model != self.embedding_model:
            logger.warning(
                "function_discovery_manifest_embedding_mismatch",
                stored_model=stored_model,
                requested_model=self.embedding_model,
                note="Existing shared index will be rebuilt by writer.",
            )
            return False
        if int(payload.get("entry_schema_version") or 1) != self._ENTRY_SCHEMA_VERSION:
            logger.warning(
                "function_discovery_manifest_entry_schema_mismatch",
                stored_entry_schema=int(payload.get("entry_schema_version") or 1),
                expected_entry_schema=self._ENTRY_SCHEMA_VERSION,
                note="Existing shared index will be rebuilt by writer.",
            )
            return False
        stored_index = str(payload.get("index_name") or "")
        expected_index = str(getattr(self, "_valkey_index_name", "") or "")
        if stored_index and expected_index and stored_index != expected_index:
            logger.warning(
                "function_discovery_manifest_index_mismatch",
                stored_index=stored_index,
                expected_index=expected_index,
            )
            return False
        return True

    def shared_index_is_current(self) -> bool:
        """
        Whether Redis already advertises a usable index, without loading it.

        Used to decide whether a rebuild is still needed after winning the
        writer lock: another worker may have rebuilt while this one waited.
        """
        return self._manifest_is_current(self._read_shared_manifest())

    def _save_manifest(self, *, authoritative: bool = False) -> None:
        """
        Persist entry metadata to Redis (shared) and to the local file (cache).

        Args:
            authoritative: True only for a full reindex, whose entry set replaces
                whatever was published. Incremental updates merge instead: a
                worker's `_id_to_entry` holds only the catalog it knows about, so
                publishing it wholesale would drop other workers' MCP tools from
                the manifest even though their vectors remain in the index.
        """
        self._publish_shared_manifest(authoritative=authoritative)
        self._save_manifest_file()

    def _save_manifest_file(self) -> None:
        """Write the local cache copy; best-effort, never fatal."""
        try:
            manifest_path = getattr(self, "_manifest_path", None)
            if not manifest_path:
                return
            tmp_path = f"{manifest_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._manifest_payload(self._id_to_entry), f, sort_keys=True)
            os.replace(tmp_path, manifest_path)
        except Exception as e:
            logger.warning(
                "function_discovery_manifest_save_failed",
                error=str(e),
                error_type=type(e).__name__,
            )

    def _publish_shared_manifest(self, *, authoritative: bool) -> None:
        """
        Publish this worker's entries to the shared manifest key.

        Non-authoritative writes are a read-modify-write, so they run inside
        WATCH/MULTI and retry on contention rather than clobbering a concurrent
        publisher. The merged result is adopted locally as well, which is how a
        worker comes to rank tools that only other workers registered.
        """
        redis_client = getattr(self, "_valkey_redis", None)
        key = getattr(self, "_shared_manifest_key", None)
        if redis_client is None or not key:
            return

        for attempt in range(self._MANIFEST_PUBLISH_ATTEMPTS):
            try:
                if authoritative:
                    redis_client.set(key, self._encode_manifest(self._manifest_payload(self._id_to_entry)))
                    return

                with redis_client.pipeline() as pipe:
                    pipe.watch(key)
                    existing = self._manifest_payload({})
                    current = self._decode_manifest(pipe.get(key))
                    if self._manifest_is_current(current) and current is not None:
                        existing = current
                    merged = dict(existing.get("id_to_entry") or {})
                    merged.update(self._id_to_entry)
                    for doc_id in self._removed_doc_ids:
                        merged.pop(doc_id, None)
                    pipe.multi()
                    pipe.set(key, self._encode_manifest(self._manifest_payload(merged)))
                    pipe.execute()

                self._id_to_entry = merged
                self._index_version = len(merged)
                self._removed_doc_ids.clear()
                return
            except Exception as e:
                # redis.WatchError is the expected retry case; treat any failure
                # the same way, since the local file copy still gets written and
                # the next mutation republishes.
                if attempt == self._MANIFEST_PUBLISH_ATTEMPTS - 1:
                    logger.warning(
                        "function_discovery_shared_manifest_publish_failed",
                        key=key,
                        attempts=attempt + 1,
                        authoritative=authoritative,
                        error=str(e),
                        error_type=type(e).__name__,
                    )
                    return

    def _load_existing_index(self) -> bool:
        """
        Adopt an already-published index rather than rebuilding one.

        Reads the shared manifest from Redis first, since that is the only copy
        every worker can see; the on-disk manifest is a per-container cache and
        is consulted only if Redis is unreachable. Trusting the file first is
        what let a restarting worker conclude no index existed and rebuild the
        shared one from its own partial catalog (#156).

        Returns True when a usable existing index was loaded.
        """
        payload = self._read_shared_manifest()
        source = "redis"
        if payload is None:
            payload = self._read_manifest_file()
            source = "file"
        if not self._manifest_is_current(payload) or payload is None:
            return False

        try:
            self._initialize_hybrid_retriever()
        except Exception as e:
            logger.warning(
                "function_discovery_retriever_init_failed_on_load",
                error=str(e),
                error_type=type(e).__name__,
            )
            return False

        self._id_to_entry = dict(payload.get("id_to_entry") or {})
        self._index_version = payload.get("index_version") or len(self._id_to_entry)
        self._removed_doc_ids.clear()
        self._initialized = True
        logger.info(
            "function_discovery_loaded_existing_index",
            source=source,
            persist_dir=self.persist_dir,
            doc_count=len(self._id_to_entry),
        )
        return True

    def _read_manifest_file(self) -> Optional[Dict[str, Any]]:
        """Read the local cache copy of the manifest, or None when unusable."""
        manifest_path = getattr(self, "_manifest_path", None)
        if not manifest_path or not os.path.exists(manifest_path):
            return None
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            return payload if isinstance(payload, dict) else None
        except Exception as e:
            logger.warning(
                "function_discovery_manifest_load_failed",
                manifest_path=manifest_path,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            return None

    def ensure_shared_index(
        self,
        tool_registry: Any,
        workflow_registry: Any,
        *,
        lock_factory: Callable[[], Any],
        include_commands: bool = True,
        force_reindex: bool = False,
        wait_timeout_seconds: float = 180.0,
        poll_interval_seconds: float = 2.0,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> str:
        """
        Populate the shared index, rebuilding it at most once across all workers.

        A rebuild is destructive: it drops the index and repopulates it from the
        calling worker's registry. Workers do not have identical registries at
        startup (MCP tools register asynchronously, afterwards), so an unguarded
        rebuild evicts whatever another worker had already contributed. That was
        happening on every parallel restart (#156).

        The sequence is: adopt a published index if one exists; otherwise take
        the writer lock and re-check before rebuilding, because the worker that
        held the lock while we waited has very likely just published one. A
        worker that cannot get the lock waits for the winner rather than
        rebuilding alongside it.

        Args:
            lock_factory: Returns an acquired distributed lock, or None if it is
                held elsewhere. Injected so the store does not depend on the
                locking implementation and so tests can drive contention.
            wait_timeout_seconds: How long to wait for the lock holder to publish
                before rebuilding anyway. Running with no index at all is worse
                than a redundant rebuild.
            sleep_fn: Override for the poll sleep (tests).

        Returns:
            One of "loaded", "rebuilt", "loaded_after_wait", or
            "rebuilt_after_timeout" — the last indicating the timeout path, which
            can still race and is logged at warning level.
        """
        if sleep_fn is None:
            from ..workers.concurrency_primitives import worker_sleep as sleep_fn  # type: ignore[assignment]
        assert sleep_fn is not None

        def _rebuild() -> None:
            self.index_tools_and_workflows(
                tool_registry=tool_registry,
                workflow_registry=workflow_registry,
                force_reindex=True,
                include_commands=include_commands,
            )

        if not force_reindex and self._load_existing_index():
            return "loaded"

        deadline = time.monotonic() + max(float(wait_timeout_seconds), 0.0)
        waited = False
        while True:
            lock = None
            try:
                lock = lock_factory()
            except Exception as e:
                logger.warning(
                    "function_discovery_writer_lock_error",
                    error=str(e),
                    error_type=type(e).__name__,
                )

            if lock is not None:
                try:
                    # Re-check under the lock. Whoever held it before us was very
                    # likely rebuilding, and their result is as good as ours.
                    if not force_reindex and self._load_existing_index():
                        return "loaded_after_wait" if waited else "loaded"
                    _rebuild()
                    return "rebuilt"
                finally:
                    try:
                        lock.release_sync()
                    except Exception:
                        pass  # lock release best-effort

            if time.monotonic() >= deadline:
                break

            waited = True
            sleep_fn(poll_interval_seconds)
            if not force_reindex and self._load_existing_index():
                return "loaded_after_wait"

        logger.warning(
            "function_discovery_writer_lock_wait_timeout",
            wait_timeout_seconds=wait_timeout_seconds,
            note=(
                "Rebuilding without the writer lock; a concurrent rebuild may "
                "evict entries contributed by other workers."
            ),
        )
        _rebuild()
        return "rebuilt_after_timeout"

    def index_tools_and_workflows(
        self,
        tool_registry: Any,  # ToolRegistry
        workflow_registry: Any,  # WorkflowRegistry (class)
        force_reindex: bool = False,
        *,
        include_commands: bool = True,
    ) -> int:
        """
        Index tools, workflows, and distributed commands for semantic search.
        
        Also known as `index_functions` - indexes all callable "functions" from the LLM's
        perspective: tools, workflows, and distributed command types.
        
        Indexing strategy:
        - Tools: name (normalized) + description + keywords + parameter names + parameter descriptions
        - Workflows: name + description + keywords + workflow_function_name
        - Commands: command_type (normalized) + description + field names + field descriptions
        
        Args:
            tool_registry: ToolRegistry instance with all registered tools (including MCP tools)
            workflow_registry: WorkflowRegistry class with all registered workflows
            force_reindex: If True, re-index even if already initialized
            include_commands: If True, also index distributed command types (default: True)
        
        Returns:
            Number of items indexed (tools + workflows + commands)
        """
        if not force_reindex and self._load_existing_index():
            return 0
        if self._initialized and not force_reindex:
            logger.debug("function_discovery_already_indexed")
            return 0

        # Always clear shared Valkey discovery keys before full (re)indexing.
        logger.info(
            "function_discovery_index_reset",
            force_reindex=force_reindex,
            persist_dir=self.persist_dir,
            hybrid_required=True,
        )
        self._initialize_hybrid_retriever()
        hybrid = self._hybrid_retriever
        if hybrid is None:
            raise RuntimeError("Valkey hybrid retriever failed to initialize")
        try:
            hybrid.clear_all()
            logger.info(
                "function_discovery_valkey_cleared",
                index=self._valkey_index_name,
                prefix=self._valkey_key_prefix,
            )
        except Exception as e:
            logger.error(
                "failed_to_clear_valkey_discovery_index",
                error=str(e),
                error_type=type(e).__name__,
                index=self._valkey_index_name,
                exc_info=True,
            )
            raise
        self._initialized = False
        
        items = []
        self._id_to_entry = {}
        
        # Index tools (both built-in and MCP tools)
        # MCP tools are registered in the same registry with format: mcp.<server_id>.<tool_name>
        # They have the same RegisteredTool structure (name, description, keywords, category)
        # Uses shared _build_tool_item() helper (also used by index_tools_incremental).
        tool_count = 0
        for name, tool in tool_registry.list_items().items():
            item = self._build_tool_item(
                name,
                tool,
                scope_keys=self._resolve_tool_scope_keys(tool_registry, name),
            )
            if item is not None:
                items.append(item)
                tool_count += 1
        
        # Index workflows (as "tools" from LLM perspective)
        # Only index workflows used for tool discovery (use_for includes "tool")
        workflow_count = 0
        from motet.core.workflow.user_catalog import is_user_workflow_id

        for workflow in workflow_registry.list_all():
            if not workflow.is_used_for_tool():
                continue
            if is_user_workflow_id(str(getattr(workflow, "workflow_id", "") or "")):
                continue
            workflow_scope_keys = self._resolve_workflow_scope_keys(
                workflow_registry, workflow.workflow_id
            )
            doc_id, workflow_item, entry = self._build_workflow_item(
                workflow,
                scope_keys=workflow_scope_keys,
            )
            items.append(workflow_item)
            self._id_to_entry[doc_id] = entry
            workflow_count += 1
        for doc_id, workflow_item, entry in self._catalog_user_workflow_index_items():
            items.append(workflow_item)
            self._id_to_entry[doc_id] = entry
            workflow_count += 1
        
        # Index distributed command types (for help/internal operations routing).
        # Uses shared `_build_command_item()` (also used by index_commands_incremental).
        command_count = 0
        if include_commands:
            try:
                from motet.core.commands.command_type_registry import command_type_registry

                for command_type in command_type_registry.get_all_registrations().keys():
                    ct = str(command_type)
                    _doc_id, command_item, entry = self._build_command_item(
                        ct,
                        scope_keys=self._resolve_command_scope_keys(ct),
                    )
                    if command_item is None or entry is None:
                        continue
                    items.append(command_item)
                    self._id_to_entry[_doc_id] = entry
                    command_count += 1

            except Exception as e:
                logger.warning(
                    "function_discovery_command_indexing_failed",
                    error=str(e),
                    error_type=type(e).__name__,
                    note="Commands will not be included in vector search",
                    exc_info=True,
                )
        
        # Add all items to hybrid retriever (required).
        if items:
            self._initialize_hybrid_retriever()
            hybrid_idx = self._hybrid_retriever
            if hybrid_idx is None:
                raise RuntimeError("Valkey hybrid retriever failed to initialize")

            documents_to_index = [item.content for item in items]
            doc_ids = [item.id for item in items]

            # Valkey retriever will embed and store vectors for all docs.
            hybrid_idx.add_documents_batch(
                documents_to_index,
                doc_ids=doc_ids,
                mode="unified",
                show_progress=False,
            )

            self._initialized = True
            self._index_version = len(items)
            # Authoritative: a full reindex just rewrote the whole index, so its
            # entry set replaces whatever was published rather than merging.
            self._removed_doc_ids.clear()
            self._save_manifest(authoritative=True)

            logger.info(
                "hybrid_retriever_indexed",
                document_count=len(documents_to_index),
                note="valkey hybrid retriever indexed and ready",
            )

            # Partial indexing is otherwise invisible: the backend reports every
            # document in num_docs with zero failures even when some never made
            # it into the vector graph, so those tools are silently unreachable
            # by semantic search. Compare against what KNN can actually return.
            searchable = hybrid_idx.count_searchable_documents(
                limit=max(len(doc_ids) * 2, 100)
            )
            if 0 <= searchable < len(doc_ids):
                logger.error(
                    "function_discovery_index_incomplete",
                    written=len(doc_ids),
                    searchable=searchable,
                    unreachable=len(doc_ids) - searchable,
                    note=(
                        "Documents were written but are not reachable by vector "
                        "KNN; semantic tool discovery will miss them."
                    ),
                )
        
        logger.info(
            "function_discovery_indexed",
            tool_count=tool_count,
            workflow_count=workflow_count,
            command_count=command_count,
            total_items=len(items),
            hybrid_search_enabled=self._hybrid_retriever is not None
        )
        
        return len(items)

    # ------------------------------------------------------------------
    # Incremental indexing for event-driven MCP tool registration (ADR-0069)
    # ------------------------------------------------------------------

    def _compute_item_hash(self, item: "MemoryItem") -> str:
        """Compute a stable content hash for one indexed document."""
        payload = {
            "id": item.id,
            "content": item.content,
            "tags": sorted(item.tags or []),
            "metadata": item.metadata or {},
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _build_tool_item(
        self,
        name: str,
        tool: Any,
        *,
        record_entry: bool = True,
        scope_keys: Optional[List[str]] = None,
    ) -> Optional["MemoryItem"]:
        """
        Build a MemoryItem + _id_to_entry for a single tool.

        Returns None when the tool cannot be indexed (e.g. missing schema).
        Side-effect: updates ``self._id_to_entry``.
        """
        # Replace both underscores and dots with spaces so BM25 tokenizes
        # "core.memory_tag" as ["core", "memory", "tag"] rather than
        # keeping "core.memory" as a single opaque token.
        normalized_name = name.replace("_", " ").replace(".", " ")

        param_names: List[str] = []
        param_descriptions: List[str] = []

        if tool.tool_schema:
            try:
                from .schema_normalizer import ToolSchemaNormalizer

                param_names_set = ToolSchemaNormalizer.get_parameter_names(tool)
                param_names = list(param_names_set)

                full_schema = ToolSchemaNormalizer.get_full_schema(tool)
                properties = full_schema.get("properties", {})

                for pname in param_names:
                    pdesc = properties.get(pname, {}).get("description")
                    param_descriptions.append(f"{pname}: {pdesc}" if pdesc else pname)
            except Exception:
                pass  # param metadata extraction best-effort; continue with basic info

        searchable_parts = [normalized_name, tool.description]
        if tool.keywords:
            searchable_parts.extend(tool.keywords)
        searchable_parts.extend(param_names)
        if param_descriptions:
            searchable_parts.extend(param_descriptions)
        searchable_text = " ".join(searchable_parts)

        is_mcp = name.startswith("mcp.")
        metadata: Dict[str, Any] = {
            "type": "tool",
            "tool_name": name,
            "category": tool.category or "general",
            "is_mcp": is_mcp,
            "scope_keys": list(scope_keys or ["*:*:*:*"]),
            # Carried so `_tool_entry_from_item` can build the searchable entry.
            # The keyword half of hybrid search reads these; without them it can
            # only match tokenized names.
            "description": self._clip_entry_description(tool.description),
            "keywords": list(tool.keywords or []),
        }

        if is_mcp:
            # Canonical MCP format: mcp.server_id.tool_name (dots)
            parts = name.split(".", 2)
            if len(parts) >= 2:
                metadata["mcp_service_id"] = parts[1]
                metadata["mcp_tool_name"] = parts[2] if len(parts) > 2 else name

        doc_id = f"tool:{name}"
        item = MemoryItem(
            id=doc_id,
            type="function_discovery",
            content=searchable_text,
            tags=[f"tool:{name}", f"category:{tool.category or 'general'}"],
            metadata=metadata,
        )
        if record_entry:
            self._id_to_entry[doc_id] = self._tool_entry_from_item(item)
        return item

    @classmethod
    def _clip_entry_description(cls, description: Optional[str]) -> str:
        """Bound description text stored per entry (see _ENTRY_DESCRIPTION_MAX_CHARS)."""
        text = str(description or "").strip()
        if len(text) <= cls._ENTRY_DESCRIPTION_MAX_CHARS:
            return text
        return text[: cls._ENTRY_DESCRIPTION_MAX_CHARS]

    def _tool_entry_from_item(self, item: "MemoryItem") -> Dict[str, Any]:
        """
        Build the persisted `_id_to_entry` record for a tool from its MemoryItem.

        Single source of truth for tool entry shape. Full reindex, incremental MCP
        sync, and bundle sync all route through here — they previously duplicated
        this dict, which is how `description` came to be missing on some paths.
        """
        meta = item.metadata or {}
        return {
            "type": "tool",
            "tool_name": meta.get("tool_name", ""),
            "category": meta.get("category", "general"),
            "is_mcp": bool(meta.get("is_mcp", False)),
            "mcp_service_id": meta.get("mcp_service_id"),
            "mcp_tool_name": meta.get("mcp_tool_name"),
            "scope_keys": list(meta.get("scope_keys") or ["*:*:*:*"]),
            "description": self._clip_entry_description(meta.get("description")),
            "keywords": list(meta.get("keywords") or []),
            "content_hash": self._compute_item_hash(item),
        }

    def _resolve_tool_scope_keys(self, tool_registry: Any, name: str) -> List[str]:
        """Resolve scope keys for a tool from registry metadata."""
        try:
            get_scope = getattr(tool_registry, "get_scope", None)
            if callable(get_scope):
                scope = get_scope(name)
                if scope is not None:
                    sk = getattr(scope, "scope_keys", None)
                    if callable(sk):
                        keys = sk()
                        if keys:
                            return list(cast(Iterable[str], keys))
        except Exception:
            pass  # scope resolution optional; default to wildcard
        return ["*:*:*:*"]

    def _resolve_workflow_scope_keys(self, workflow_registry: Any, workflow_id: str) -> List[str]:
        """Resolve scope keys for a workflow from registry metadata."""
        try:
            get_scope = getattr(workflow_registry, "get_scope", None)
            if callable(get_scope):
                scope = get_scope(workflow_id)
                if scope is not None:
                    sk = getattr(scope, "scope_keys", None)
                    if callable(sk):
                        keys = sk()
                        if keys:
                            return list(cast(Iterable[str], keys))
        except Exception:
            pass  # scope resolution optional; default to wildcard
        return ["*:*:*:*"]

    def _resolve_command_scope_keys(self, command_type: str) -> List[str]:
        """Resolve scope keys for a command type from command registry metadata."""
        try:
            from motet.core.commands.command_type_registry import command_type_registry

            scope = command_type_registry.get_scope(command_type)
            if scope is not None:
                sk = getattr(scope, "scope_keys", None)
                if callable(sk):
                    keys = sk()
                    if keys:
                        return list(cast(Iterable[str], keys))
        except Exception:
            pass  # scope resolution optional; default to wildcard
        return ["*:*:*:*"]

    def index_tools_incremental(
        self,
        tool_names: List[str],
        tool_registry: Any,
    ) -> int:
        """
        Incrementally add newly-registered tools to the hybrid index (ADR-0069).

        Called by the MCP watcher when new MCP tools are discovered after the
        initial ``index_tools_and_workflows`` has already run.  Uses
        ``HybridRetriever.add_documents_batch`` which natively supports
        incremental BM25 + vector updates without full re-index.

        Args:
            tool_names: List of tool names to add (e.g. ``["mcp.weather.get_forecast"]``).
            tool_registry: ToolRegistry instance to look up tool metadata.

        Returns:
            Number of tools actually indexed (skips tools already present).
        """
        if not self._initialized or self._hybrid_retriever is None:
            logger.warning(
                "function_discovery_incremental_skip",
                reason="store not initialized or hybrid retriever missing",
                tool_names=tool_names,
            )
            return 0

        all_tools = tool_registry.list_items()
        items_to_add: List[MemoryItem] = []
        items_to_update: List[MemoryItem] = []

        for name in tool_names:
            doc_id = f"tool:{name}"
            tool = all_tools.get(name)
            if tool is None:
                logger.debug("function_discovery_incremental_tool_missing", tool_name=name)
                continue
            item = self._build_tool_item(
                name,
                tool,
                record_entry=False,
                scope_keys=self._resolve_tool_scope_keys(tool_registry, name),
            )
            if item is not None:
                new_hash = self._compute_item_hash(item)
                existing_hash = (self._id_to_entry.get(doc_id) or {}).get("content_hash")
                if doc_id not in self._id_to_entry:
                    items_to_add.append(item)
                elif existing_hash != new_hash:
                    items_to_update.append(item)

        if not items_to_add and not items_to_update:
            return 0

        added_count = 0
        updated_count = 0

        try:
            if items_to_update:
                update_doc_ids = [item.id for item in items_to_update]
                self._hybrid_retriever.remove_documents_batch(update_doc_ids)
                self._hybrid_retriever.add_documents_batch(
                    [item.content for item in items_to_update],
                    doc_ids=update_doc_ids,
                    mode="unified",
                    show_progress=False,
                )
                for item in items_to_update:
                    self._id_to_entry[item.id] = self._tool_entry_from_item(item)
                updated_count = len(items_to_update)

            if items_to_add:
                add_doc_ids = [item.id for item in items_to_add]
                self._hybrid_retriever.add_documents_batch(
                    [item.content for item in items_to_add],
                    doc_ids=add_doc_ids,
                    mode="unified",
                    show_progress=False,
                )
                for item in items_to_add:
                    self._id_to_entry[item.id] = self._tool_entry_from_item(item)
                added_count = len(items_to_add)

            self._index_version = (self._index_version or 0) + added_count
            logger.info(
                "function_discovery_incremental_indexed",
                tool_count_added=added_count,
                tool_count_updated=updated_count,
                tool_names_added=[item.id for item in items_to_add],
                tool_names_updated=[item.id for item in items_to_update],
                index_version=self._index_version,
            )
            self._save_manifest()
        except Exception as e:
            logger.error(
                "function_discovery_incremental_index_failed",
                error=str(e),
                error_type=type(e).__name__,
                tool_count=len(items_to_add) + len(items_to_update),
                exc_info=True,
            )
            return 0

        return added_count + updated_count

    def index_workflows_incremental(
        self,
        workflow_ids: List[str],
        workflow_registry: Any,
    ) -> int:
        """
        Incrementally add newly-registered workflows to the hybrid index.

        Mirrors ``index_tools_incremental`` for workflows. Called when workflows
        are registered after the initial ``index_tools_and_workflows`` has already
        run — e.g. when motet bundles load per-motet workflows at runtime.

        Args:
            workflow_ids: List of workflow IDs to add (e.g. ``["navigate_screenshot"]``).
            workflow_registry: WorkflowRegistry class to look up workflow metadata.

        Returns:
            Number of workflows actually indexed (skips those already present).
        """
        if not self._initialized or self._hybrid_retriever is None:
            logger.warning(
                "function_discovery_workflow_incremental_skip",
                reason="store not initialized or hybrid retriever missing",
                workflow_ids=workflow_ids,
            )
            return 0

        items_to_add: List[Tuple[str, MemoryItem, Dict[str, Any]]] = []
        from motet.core.workflow.user_catalog import is_user_workflow_id

        for workflow_id in workflow_ids:
            if is_user_workflow_id(str(workflow_id or "")):
                logger.debug(
                    "function_discovery_workflow_incremental_skip_user",
                    workflow_id=workflow_id,
                    note="user.* workflows are catalog-indexed as workflow:{tenant}:{id}",
                )
                continue
            workflow = workflow_registry.get(workflow_id)
            if workflow is None:
                logger.debug(
                    "function_discovery_workflow_incremental_missing",
                    workflow_id=workflow_id,
                )
                continue
            if not workflow.is_used_for_tool():
                logger.debug(
                    "function_discovery_workflow_incremental_skip_use_for",
                    workflow_id=workflow_id,
                    use_for=workflow.use_for,
                )
                continue
            doc_id, item, entry = self._build_workflow_item(
                workflow,
                scope_keys=self._resolve_workflow_scope_keys(workflow_registry, workflow_id),
            )
            items_to_add.append((doc_id, item, entry))

        return self._upsert_workflow_index_items(items_to_add)

    def index_commands_incremental(self, command_types: List[str]) -> int:
        """
        Incrementally add/update distributed command docs in the hybrid index.

        Uses per-doc content hashes so unchanged command docs are skipped, while
        changed command docs are refreshed via remove+add for the same doc_id.
        """
        if not self._initialized or self._hybrid_retriever is None:
            logger.warning(
                "function_discovery_command_incremental_skip",
                reason="store not initialized or hybrid retriever missing",
                command_types=command_types,
            )
            return 0

        items_to_add: List[Tuple[str, MemoryItem, Dict[str, Any]]] = []
        items_to_update: List[Tuple[str, MemoryItem, Dict[str, Any]]] = []

        for command_type in command_types:
            doc_id = f"command:{command_type}"
            _doc_id, item, entry = self._build_command_item(
                command_type,
                scope_keys=self._resolve_command_scope_keys(command_type),
            )
            if item is None or entry is None:
                logger.debug(
                    "function_discovery_command_incremental_missing",
                    command_type=command_type,
                )
                continue

            existing_hash = (self._id_to_entry.get(doc_id) or {}).get("content_hash")
            new_hash = entry.get("content_hash")
            if doc_id not in self._id_to_entry:
                items_to_add.append((doc_id, item, entry))
            elif existing_hash != new_hash:
                items_to_update.append((doc_id, item, entry))

        if not items_to_add and not items_to_update:
            return 0

        added_count = 0
        updated_count = 0

        try:
            if items_to_update:
                update_doc_ids = [doc_id for doc_id, _item, _entry in items_to_update]
                self._hybrid_retriever.remove_documents_batch(update_doc_ids)
                self._hybrid_retriever.add_documents_batch(
                    [item.content for _doc_id, item, _entry in items_to_update],
                    doc_ids=update_doc_ids,
                    mode="unified",
                    show_progress=False,
                )
                for doc_id, _item, entry in items_to_update:
                    self._id_to_entry[doc_id] = entry
                updated_count = len(items_to_update)

            if items_to_add:
                add_doc_ids = [doc_id for doc_id, _item, _entry in items_to_add]
                self._hybrid_retriever.add_documents_batch(
                    [item.content for _doc_id, item, _entry in items_to_add],
                    doc_ids=add_doc_ids,
                    mode="unified",
                    show_progress=False,
                )
                for doc_id, _item, entry in items_to_add:
                    self._id_to_entry[doc_id] = entry
                added_count = len(items_to_add)

            self._index_version = (self._index_version or 0) + added_count
            logger.info(
                "function_discovery_command_incremental_indexed",
                command_count_added=added_count,
                command_count_updated=updated_count,
                command_ids_added=[doc_id for doc_id, _item, _entry in items_to_add],
                command_ids_updated=[doc_id for doc_id, _item, _entry in items_to_update],
                index_version=self._index_version,
            )
            self._save_manifest()
        except Exception as e:
            logger.error(
                "function_discovery_command_incremental_failed",
                error=str(e),
                error_type=type(e).__name__,
                command_count=len(items_to_add) + len(items_to_update),
                exc_info=True,
            )
            return 0

        return added_count + updated_count

    def remove_workflows_incremental(self, workflow_ids: List[str]) -> int:
        """
        Remove core/bundle workflow docs by id.

        ``user.*`` ids only drop leftover unscoped ``workflow:{id}`` docs.
        Tenant-qualified user docs are removed via ``remove_user_workflow``.
        """
        if not self._initialized or self._hybrid_retriever is None:
            return 0
        doc_ids = [
            f"workflow:{wid}"
            for wid in (workflow_ids or [])
            if isinstance(wid, str) and wid.strip()
        ]
        if not doc_ids:
            return 0
        try:
            removed = self._remove_doc_ids(doc_ids)
            logger.info(
                "function_discovery_workflows_removed",
                workflow_ids=workflow_ids,
                removed=removed,
                index_version=self._index_version,
            )
            return removed
        except Exception as exc:
            logger.warning(
                "function_discovery_workflows_remove_failed",
                workflow_ids=workflow_ids,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return 0

    def remove_tools_for_service(self, service_id: str) -> int:
        """
        Remove all indexed tools belonging to an MCP service (ADR-0069).

        Called by the MCP watcher when a ``service_removed`` event is received.
        Uses ``HybridRetriever.remove_documents_batch`` for incremental removal.

        Args:
            service_id: MCP service ID (e.g. ``"weather"``).

        Returns:
            Number of tools removed from the index.
        """
        if not self._initialized or self._hybrid_retriever is None:
            return 0

        prefix = f"tool:mcp.{service_id}."
        try:
            removed = self._remove_doc_ids(
                [doc_id for doc_id in self._id_to_entry if doc_id.startswith(prefix)]
            )
            logger.info(
                "function_discovery_service_removed",
                service_id=service_id,
                tools_removed=removed,
                index_version=self._index_version,
            )
        except Exception as e:
            logger.error(
                "function_discovery_service_remove_failed",
                service_id=service_id,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            return 0

        return removed

    def _remove_doc_ids(self, doc_ids: List[str]) -> int:
        """Remove a list of doc IDs from retriever + in-memory entry map."""
        if not doc_ids:
            return 0
        hybrid_rm = self._hybrid_retriever
        if hybrid_rm is None:
            return 0
        unique_doc_ids = sorted(set(doc_ids))
        hybrid_rm.remove_documents_batch(unique_doc_ids)
        for doc_id in unique_doc_ids:
            self._id_to_entry.pop(doc_id, None)
            self._removed_doc_ids.add(doc_id)
        self._index_version = (self._index_version or 0) - len(unique_doc_ids)
        self._save_manifest()
        return len(unique_doc_ids)

    def _remove_stale_docs(self, kind_prefix: str, desired_doc_ids: set[str]) -> int:
        """
        Remove indexed docs for a kind that are no longer desired.

        Args:
            kind_prefix: Prefix like ``"tool:"``, ``"workflow:"``, ``"command:"``.
            desired_doc_ids: Exact doc IDs that should remain for this kind.
        """
        indexed_doc_ids = [
            doc_id for doc_id in self._id_to_entry
            if doc_id.startswith(kind_prefix)
        ]
        stale_doc_ids = sorted(set(indexed_doc_ids) - desired_doc_ids)
        return self._remove_doc_ids(stale_doc_ids)

    def _build_workflow_item(
        self,
        workflow: Any,
        *,
        scope_keys: Optional[List[str]] = None,
        tenant_id: Optional[str] = None,
    ) -> Tuple[str, "MemoryItem", Dict[str, Any]]:
        """Build a MemoryItem + entry metadata for a single workflow."""
        from motet.core.workflow.user_catalog import (
            is_user_workflow_id,
            user_workflow_discovery_doc_id,
        )

        from motet.core.workflow import workflow_discovery_keywords

        workflow_id = str(getattr(workflow, "workflow_id", "") or "")
        workflow_function_name = f"workflow_{workflow_id}"
        normalized_function_name = workflow_function_name.replace("_", " ")
        description = self._clip_entry_description(getattr(workflow, "description", "") or "")
        keywords = workflow_discovery_keywords(workflow)
        searchable_parts = [
            getattr(workflow, "name", "") or "",
            description,
            normalized_function_name,
            *keywords,
        ]
        searchable_text = " ".join(part for part in searchable_parts if part)
        tid = (tenant_id or "").strip()
        if is_user_workflow_id(workflow_id):
            if not tid:
                meta = getattr(workflow, "metadata", None)
                if isinstance(meta, dict):
                    tid = str(meta.get("tenant_id") or "").strip()
            if not tid:
                raise ValueError(
                    f"user workflow {workflow_id!r} requires tenant_id for discovery index"
                )
            doc_id = user_workflow_discovery_doc_id(tid, workflow_id)
        else:
            doc_id = f"workflow:{workflow_id}"
        item = MemoryItem(
            id=doc_id,
            type="function_discovery",
            content=searchable_text,
            tags=[f"workflow:{workflow_id}", f"function:{workflow_function_name}"],
            metadata={
                "type": "workflow",
                "workflow_id": workflow_id,
                "workflow_function_name": workflow_function_name,
                "name": workflow.name,
                "description": description,
                "keywords": list(keywords),
                "tenant_id": tid or None,
                "scope_keys": list(scope_keys or ["*:*:*:*"]),
            },
        )
        entry = {
            "type": "workflow",
            "workflow_id": workflow_id,
            "workflow_function_name": workflow_function_name,
            "name": workflow.name,
            "description": description,
            "keywords": list(keywords),
            "tenant_id": tid or None,
            "scope_keys": list(scope_keys or ["*:*:*:*"]),
            "content_hash": self._compute_item_hash(item),
        }
        return doc_id, item, entry

    def _catalog_user_workflow_index_items(
        self,
    ) -> List[Tuple[str, "MemoryItem", Dict[str, Any]]]:
        """Build discovery items for every tenant catalog row. Fail-soft."""
        items: List[Tuple[str, MemoryItem, Dict[str, Any]]] = []
        try:
            from motet.core.workflow import Workflow
            from motet.core.workflow.user_catalog import (
                iter_catalog_user_workflows,
                scope_for_user_workflow,
            )
        except Exception as exc:
            logger.warning(
                "function_discovery_user_catalog_import_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return items
        try:
            for tenant_id, _workflow_id, raw in iter_catalog_user_workflows():
                try:
                    workflow = Workflow.from_dict(raw)
                except Exception as exc:
                    logger.warning(
                        "function_discovery_user_catalog_parse_failed",
                        workflow_id=raw.get("workflow_id") if isinstance(raw, dict) else None,
                        tenant_id=tenant_id,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    continue
                if not workflow.is_used_for_tool():
                    continue
                scope = scope_for_user_workflow(workflow)
                doc_id, item, entry = self._build_workflow_item(
                    workflow,
                    scope_keys=list(scope.scope_keys()) if scope is not None else None,
                    tenant_id=tenant_id,
                )
                items.append((doc_id, item, entry))
        except Exception as exc:
            logger.warning(
                "function_discovery_user_catalog_index_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
        return items

    def index_user_workflow(self, workflow_id: str, tenant_id: str) -> int:
        """Index one catalog ``user.*`` row as ``workflow:{tenant}:{id}``."""
        if not self._initialized or self._hybrid_retriever is None:
            return 0
        from motet.core.workflow import Workflow
        from motet.core.workflow.user_catalog import (
            fetch_user_workflow_dict,
            leftover_user_workflow_discovery_doc_id,
            scope_for_user_workflow,
        )

        tid = (tenant_id or "").strip()
        wid = (workflow_id or "").strip()
        if not tid or not wid:
            return 0
        leftover = leftover_user_workflow_discovery_doc_id(wid)
        if leftover in self._id_to_entry:
            try:
                self._remove_doc_ids([leftover])
            except Exception as exc:
                logger.warning(
                    "function_discovery_user_leftover_remove_failed",
                    workflow_id=wid,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        raw = fetch_user_workflow_dict(wid, tenant_id=tid)
        if not raw:
            return 0
        workflow = Workflow.from_dict(raw)
        if not workflow.is_used_for_tool():
            return 0
        scope = scope_for_user_workflow(workflow)
        doc_id, item, entry = self._build_workflow_item(
            workflow,
            scope_keys=list(scope.scope_keys()) if scope is not None else None,
            tenant_id=tid,
        )
        return self._upsert_workflow_index_items([(doc_id, item, entry)])

    def index_user_workflows_from_catalog(self) -> int:
        """Add or refresh every tenant catalog ``user.*`` discovery doc."""
        if not self._initialized or self._hybrid_retriever is None:
            return 0
        return self._upsert_workflow_index_items(self._catalog_user_workflow_index_items())

    def remove_user_workflow(self, workflow_id: str, tenant_id: str) -> int:
        """Remove ``workflow:{tenant}:{id}`` and leftover ``workflow:{id}``."""
        if not self._initialized or self._hybrid_retriever is None:
            return 0
        from motet.core.workflow.user_catalog import (
            leftover_user_workflow_discovery_doc_id,
            user_workflow_discovery_doc_id,
        )

        tid = (tenant_id or "").strip()
        wid = (workflow_id or "").strip()
        if not tid or not wid:
            return 0
        doc_ids = [
            user_workflow_discovery_doc_id(tid, wid),
            leftover_user_workflow_discovery_doc_id(wid),
        ]
        try:
            return self._remove_doc_ids(doc_ids)
        except Exception as exc:
            logger.warning(
                "function_discovery_user_workflow_remove_failed",
                workflow_id=wid,
                tenant_id=tid,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return 0

    def _upsert_workflow_index_items(
        self,
        built: List[Tuple[str, "MemoryItem", Dict[str, Any]]],
    ) -> int:
        """Add or hash-update workflow discovery docs."""
        if not built or self._hybrid_retriever is None:
            return 0
        items_to_add: List[Tuple[str, MemoryItem, Dict[str, Any]]] = []
        items_to_update: List[Tuple[str, MemoryItem, Dict[str, Any]]] = []
        for doc_id, item, entry in built:
            existing_hash = (self._id_to_entry.get(doc_id) or {}).get("content_hash")
            new_hash = entry.get("content_hash")
            if doc_id not in self._id_to_entry:
                items_to_add.append((doc_id, item, entry))
            elif existing_hash != new_hash:
                items_to_update.append((doc_id, item, entry))
        if not items_to_add and not items_to_update:
            return 0
        added_count = 0
        updated_count = 0
        try:
            if items_to_update:
                update_doc_ids = [doc_id for doc_id, _item, _entry in items_to_update]
                self._hybrid_retriever.remove_documents_batch(update_doc_ids)
                self._hybrid_retriever.add_documents_batch(
                    [item.content for _doc_id, item, _entry in items_to_update],
                    doc_ids=update_doc_ids,
                    mode="unified",
                    show_progress=False,
                )
                for doc_id, _item, entry in items_to_update:
                    self._id_to_entry[doc_id] = entry
                updated_count = len(items_to_update)
            if items_to_add:
                add_doc_ids = [doc_id for doc_id, _item, _entry in items_to_add]
                self._hybrid_retriever.add_documents_batch(
                    [item.content for _doc_id, item, _entry in items_to_add],
                    doc_ids=add_doc_ids,
                    mode="unified",
                    show_progress=False,
                )
                for doc_id, _item, entry in items_to_add:
                    self._id_to_entry[doc_id] = entry
                added_count = len(items_to_add)
            self._index_version = (self._index_version or 0) + added_count
            self._save_manifest()
        except Exception as exc:
            logger.error(
                "function_discovery_workflow_upsert_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                count=len(items_to_add) + len(items_to_update),
                exc_info=True,
            )
            return 0
        return added_count + updated_count

    def _build_command_item(
        self,
        command_type: str,
        *,
        scope_keys: Optional[List[str]] = None,
    ) -> Tuple[str, Optional["MemoryItem"], Optional[Dict[str, Any]]]:
        """Build a MemoryItem + entry metadata for a single distributed command type."""
        from motet.core.commands.command_type_registry import command_type_registry
        from motet.core.commands.command_data_registry import command_data_registry

        registration = command_type_registry.get(command_type)
        if registration is None:
            return f"command:{command_type}", None, None

        normalized_ct = command_type.replace("_", " ")
        data_class = getattr(registration, "data_class", None) or command_data_registry.get(
            command_type
        )
        # Tool-parity: discovery prose lives on the registration (#194).
        desc = self._clip_entry_description(getattr(registration, "description", None))
        if not desc:
            # Soft fallback for hand-built registrations that omitted description.
            from motet.core.commands.command_type_registry import derive_command_description

            desc = self._clip_entry_description(
                derive_command_description(registration.implementation, data_class)
            )
        field_descriptions: List[str] = []
        field_names: List[str] = []

        if data_class:
            try:
                if hasattr(data_class, "model_fields"):
                    fields = data_class.model_fields
                elif hasattr(data_class, "__fields__"):
                    fields = data_class.__fields__
                else:
                    fields = {}

                for field_name, field_info in fields.items():
                    field_names.append(field_name)
                    field_desc = None
                    if hasattr(field_info, "description"):
                        field_desc = field_info.description
                    else:
                        nested_fi = getattr(field_info, "field_info", None)
                        if nested_fi is not None:
                            field_desc = getattr(nested_fi, "description", None)

                    if not field_desc:
                        try:
                            if hasattr(data_class, "model_json_schema"):
                                schema = data_class.model_json_schema()
                                properties = schema.get("properties", {})
                                if field_name in properties:
                                    field_desc = properties[field_name].get("description")
                        except Exception:
                            pass  # schema extraction best-effort; field may lack JSON schema

                    if field_desc:
                        field_descriptions.append(f"{field_name}: {field_desc}")
                    else:
                        field_descriptions.append(field_name)
            except Exception as e:
                logger.debug(
                    "function_discovery_field_extraction_failed",
                    command_type=command_type,
                    error=str(e),
                )

        searchable_parts = [normalized_ct]
        if desc:
            searchable_parts.append(desc)
        if field_names:
            searchable_parts.extend(field_names)
        if field_descriptions:
            searchable_parts.extend(field_descriptions)
        searchable_text = " ".join(searchable_parts)

        doc_id = f"command:{command_type}"
        item = MemoryItem(
            id=doc_id,
            type="function_discovery",
            content=searchable_text,
            tags=[f"command:{command_type}"],
            metadata={
                "type": "command",
                "command_type": command_type,
                "description": desc,
                "scope_keys": list(scope_keys or ["*:*:*:*"]),
            },
        )
        entry = {
            "type": "command",
            "command_type": command_type,
            "description": desc,
            "scope_keys": list(scope_keys or ["*:*:*:*"]),
            "content_hash": self._compute_item_hash(item),
        }
        return doc_id, item, entry

    def _collect_bundle_doc_ids(self, bundle_id: str) -> List[str]:
        """Collect currently indexed doc IDs for a bundle namespace."""
        tool_prefix = f"tool:{bundle_id}."
        workflow_prefix = f"workflow:{bundle_id}."
        command_prefix = f"command:{bundle_id}."
        return sorted(
            doc_id
            for doc_id in self._id_to_entry
            if doc_id.startswith(tool_prefix)
            or doc_id.startswith(workflow_prefix)
            or doc_id.startswith(command_prefix)
        )

    def sync_bundle_entries(
        self,
        bundle_id: str,
        *,
        tool_names: List[str],
        workflow_ids: List[str],
        command_types: List[str],
        tool_registry: Any,
        workflow_registry: Any,
    ) -> Dict[str, int]:
        """
        Replace indexed entries for one bundle with current registry state.

        This removes the bundle's existing indexed docs and incrementally re-adds
        only the bundle's current tools/workflows/commands.
        """
        if not self._initialized or self._hybrid_retriever is None:
            raise RuntimeError(
                "function_discovery_bundle_sync_unavailable: store not initialized or hybrid retriever missing"
            )

        existing_doc_ids = self._collect_bundle_doc_ids(bundle_id)
        previous_entries = dict(self._id_to_entry)
        existing_hashes = {
            doc_id: (previous_entries.get(doc_id) or {}).get("content_hash")
            for doc_id in existing_doc_ids
        }

        desired_items_by_id: Dict[str, MemoryItem] = {}
        desired_entries_by_id: Dict[str, Dict[str, Any]] = {}

        all_tools = tool_registry.list_items()
        for name in sorted(set(tool_names)):
            if not name.startswith(f"{bundle_id}."):
                continue
            tool = all_tools.get(name)
            if tool is None:
                logger.debug("function_discovery_bundle_tool_missing", bundle_id=bundle_id, tool_name=name)
                continue
            item = self._build_tool_item(
                name,
                tool,
                record_entry=False,
                scope_keys=self._resolve_tool_scope_keys(tool_registry, name),
            )
            if item is not None:
                desired_items_by_id[item.id] = item
                desired_entries_by_id[item.id] = self._tool_entry_from_item(item)

        for workflow_id in sorted(set(workflow_ids)):
            if not workflow_id.startswith(f"{bundle_id}."):
                continue
            workflow = workflow_registry.get(workflow_id)
            if workflow is None:
                logger.debug(
                    "function_discovery_bundle_workflow_missing",
                    bundle_id=bundle_id,
                    workflow_id=workflow_id,
                )
                continue
            doc_id, item, entry = self._build_workflow_item(
                workflow,
                scope_keys=self._resolve_workflow_scope_keys(workflow_registry, workflow_id),
            )
            desired_items_by_id[doc_id] = item
            desired_entries_by_id[doc_id] = entry

        for command_type in sorted(set(command_types)):
            if not command_type.startswith(f"{bundle_id}."):
                continue
            doc_id, item, entry = self._build_command_item(
                command_type,
                scope_keys=self._resolve_command_scope_keys(command_type),
            )
            if item is not None and entry is not None:
                desired_items_by_id[doc_id] = item
                desired_entries_by_id[doc_id] = entry
            else:
                logger.debug(
                    "function_discovery_bundle_command_missing",
                    bundle_id=bundle_id,
                    command_type=command_type,
                )

        try:
            existing_set = set(existing_doc_ids)
            desired_ids = set(desired_items_by_id.keys())
            desired_hashes = {
                doc_id: (desired_entries_by_id.get(doc_id) or {}).get("content_hash")
                for doc_id in desired_ids
            }

            doc_ids_to_remove = sorted(existing_set - desired_ids)
            doc_ids_to_add = sorted(desired_ids - existing_set)
            doc_ids_to_update = sorted(
                doc_id
                for doc_id in (existing_set & desired_ids)
                if existing_hashes.get(doc_id) != desired_hashes.get(doc_id)
            )

            doc_ids_to_delete = sorted(set(doc_ids_to_remove + doc_ids_to_update))
            doc_ids_to_write = sorted(set(doc_ids_to_add + doc_ids_to_update))

            if doc_ids_to_delete:
                self._hybrid_retriever.remove_documents_batch(doc_ids_to_delete)
                for doc_id in doc_ids_to_delete:
                    self._id_to_entry.pop(doc_id, None)
                    self._removed_doc_ids.add(doc_id)

            if doc_ids_to_write:
                write_items = [desired_items_by_id[doc_id] for doc_id in doc_ids_to_write]
                self._hybrid_retriever.add_documents_batch(
                    [item.content for item in write_items],
                    doc_ids=doc_ids_to_write,
                    mode="unified",
                    show_progress=False,
                )
                for doc_id in doc_ids_to_write:
                    self._id_to_entry[doc_id] = desired_entries_by_id[doc_id]

            unchanged_count = len(existing_set & desired_ids) - len(doc_ids_to_update)
            self._index_version = (self._index_version or 0) - len(doc_ids_to_delete) + len(doc_ids_to_write)
            logger.info(
                "function_discovery_bundle_synced",
                bundle_id=bundle_id,
                removed=len(doc_ids_to_remove),
                added=len(doc_ids_to_add),
                updated=len(doc_ids_to_update),
                unchanged=unchanged_count,
                index_version=self._index_version,
            )
            self._save_manifest()
            return {
                "removed": len(doc_ids_to_remove),
                "added": len(doc_ids_to_add),
                "updated": len(doc_ids_to_update),
                "unchanged": unchanged_count,
            }
        except Exception as e:
            self._id_to_entry = previous_entries
            logger.error(
                "function_discovery_bundle_sync_failed",
                bundle_id=bundle_id,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise

    def reconcile_registry_state(
        self,
        *,
        tool_names: List[str],
        workflow_ids: List[str],
        command_types: List[str],
        tool_registry: Any,
        workflow_registry: Any,
    ) -> Dict[str, int]:
        """
        Reconcile index docs against the current registry state for all namespaces.

        This is used by worker startup/runtime reconciliation to converge tool,
        workflow, and command docs with minimal churn.
        """
        if not self._initialized or self._hybrid_retriever is None:
            raise RuntimeError(
                "function_discovery_registry_reconcile_unavailable: store not initialized or hybrid retriever missing"
            )

        from motet.core.workflow.user_catalog import (
            catalog_user_workflow_discovery_doc_ids,
            is_user_workflow_id,
        )

        # 1) Remove stale docs first.
        stale_tools_removed = self._remove_stale_docs(
            "tool:",
            {f"tool:{name}" for name in tool_names},
        )
        desired_workflows = {
            f"workflow:{wid}"
            for wid in workflow_ids
            if not is_user_workflow_id(str(wid or ""))
        }
        desired_workflows.update(catalog_user_workflow_discovery_doc_ids())
        stale_workflows_removed = self._remove_stale_docs(
            "workflow:",
            desired_workflows,
        )
        stale_commands_removed = self._remove_stale_docs(
            "command:",
            {f"command:{ct}" for ct in command_types},
        )

        # 2) Reconcile current docs (adds + hash-based updates).
        non_user_workflow_ids = [
            wid for wid in workflow_ids if not is_user_workflow_id(str(wid or ""))
        ]
        reconciled_tools = self.index_tools_incremental(tool_names, tool_registry)
        reconciled_workflows = self.index_workflows_incremental(
            non_user_workflow_ids, workflow_registry
        )
        reconciled_user_workflows = self.index_user_workflows_from_catalog()
        reconciled_commands = self.index_commands_incremental(command_types)

        stats = {
            "stale_tools_removed": stale_tools_removed,
            "stale_workflows_removed": stale_workflows_removed,
            "stale_commands_removed": stale_commands_removed,
            "tools_reconciled": reconciled_tools,
            "workflows_reconciled": reconciled_workflows,
            "user_workflows_reconciled": reconciled_user_workflows,
            "commands_reconciled": reconciled_commands,
        }
        logger.info("function_discovery_registry_reconciled", **stats)
        return stats

    def _extract_context_from_history(
        self,
        conversation_history: Optional[List[Message]],
        max_recent_messages: int = 5
    ) -> str:
        """
        Extract relevant context from conversation history to enhance search queries.
        
        Extracts:
        - Tool names from recent tool calls
        - Keywords from recent user messages
        - Domain/service names (e.g., "gmail", "slack", "google")
        
        Args:
            conversation_history: List of conversation messages
            max_recent_messages: Maximum number of recent messages to analyze
        
        Returns:
            Context string to append to search query
        """
        if not conversation_history:
            return ""
        
        context_parts = []
        
        # Analyze recent messages (most recent first)
        recent_messages = conversation_history[-max_recent_messages:] if len(conversation_history) > max_recent_messages else conversation_history
        
        # Extract tool names from tool calls
        tool_names = set()
        for msg in recent_messages:
            from motet.core.models.adapters.tool_call_codec import tool_calls_from_message

            for tc in tool_calls_from_message(msg):
                tool_name = tc.tool_name
                if not tool_name:
                    continue
                parts = tool_name.split(".")
                if len(parts) > 1:
                    tool_names.add(parts[1].replace("_", " "))  # Service name
                    tool_names.add(tool_name.split("--")[-1].replace("_", " "))  # Tool name
                else:
                    tool_names.add(tool_name.replace("_", " "))
        
        # Extract domain/service keywords from messages
        domain_keywords = set()
        common_services = {
            "gmail", "email", "mail", "google", "workspace", "drive", "calendar",
            "slack", "zoom", "github", "jira", "confluence", "notion",
            "aws", "azure", "gcp", "cloud", "s3", "lambda"
        }
        
        for msg in recent_messages:
            content = getattr(msg, "content", "") or str(msg)
            content_lower = content.lower()
            for service in common_services:
                if service in content_lower:
                    domain_keywords.add(service)
        
        # Combine context parts
        if tool_names:
            context_parts.extend(list(tool_names))
        if domain_keywords:
            context_parts.extend(list(domain_keywords))
        
        return " ".join(context_parts) if context_parts else ""

    @classmethod
    def _normalize_token(cls, token: str) -> str:
        token = (token or "").strip().lower()
        if not token:
            return ""
        # Lightweight stemming keeps matching broad without heavy dependencies.
        if len(token) > 5 and token.endswith("ing"):
            token = token[:-3]
        elif len(token) > 4 and token.endswith("ed"):
            token = token[:-2]
        elif len(token) > 4 and token.endswith("es"):
            token = token[:-2]
        elif len(token) > 3 and token.endswith("s"):
            token = token[:-1]
        return token

    @classmethod
    def _tokenize_meaningful(cls, text: str) -> List[str]:
        normalized = (text or "").replace("_", " ").replace("--", " ").lower()
        raw_tokens = re.split(r"[^a-z0-9]+", normalized)
        tokens: List[str] = []
        for token in raw_tokens:
            norm = cls._normalize_token(token)
            if not norm or norm in cls._COMMON_QUERY_WORDS or len(norm) <= 1:
                continue
            tokens.append(norm)
        return tokens

    @classmethod
    def _entry_searchable_text(cls, entry: Dict[str, Any]) -> str:
        """
        Text the keyword half matches against for one indexed entry.

        Mirrors the embedded document (name + description + keywords) so both
        halves of hybrid search see the same signal. Tool entries written before
        entry schema v2 have no description and degrade to name-only matching.
        """
        keywords = entry.get("keywords") or []
        return " ".join(
            [
                str(entry.get("tool_name") or ""),
                str(entry.get("workflow_function_name") or ""),
                str(entry.get("command_type") or ""),
                str(entry.get("description") or ""),
                " ".join(str(k) for k in keywords),
            ]
        ).lower()

    def _get_keyword_index(self) -> "_KeywordIndex":
        """Return the cached tokenized corpus + IDF table, rebuilding when entries change."""
        entries = self._id_to_entry
        fingerprint = (
            len(entries),
            self._index_version,
            hash(tuple(sorted((entries.get(k) or {}).get("content_hash") or k for k in entries))),
        )
        cached = getattr(self, "_keyword_index_cache", None)
        if cached is not None and cached.fingerprint == fingerprint:
            return cached

        doc_tokens: Dict[str, set] = {}
        df: Dict[str, int] = {}
        for doc_id, entry in entries.items():
            tokens = set(self._tokenize_meaningful(self._entry_searchable_text(entry)))
            if not tokens:
                continue
            doc_tokens[doc_id] = tokens
            for token in tokens:
                df[token] = df.get(token, 0) + 1

        index = _KeywordIndex(
            doc_tokens,
            df,
            fingerprint,
            k1=self._BM25_K1,
            b=self._BM25_B,
            normalize=self._normalize_token,
        )
        self._keyword_index_cache = index
        logger.debug(
            "function_discovery_keyword_index_built",
            doc_count=index.n_docs,
            vocab_size=len(df),
            avg_doc_len=round(index.avgdl, 2),
        )
        return index

    def _rank_by_keywords(
        self,
        expanded_query_tokens: Iterable[str],
        *,
        limit: Optional[int] = None,
    ) -> List[Tuple[str, float]]:
        """
        BM25-style keyword ranking over the entry manifest.

        Scores each indexed document by the IDF of the query tokens it contains,
        normalized for document length. Term frequency is set-based, so a token
        counts once per document and only its IDF and the document length matter.
        Without IDF, generic tokens (get / content / web) scored the same as
        discriminative ones and every sibling in a large MCP family tied.

        Args:
            expanded_query_tokens: Normalized query tokens, synonym-expanded.
            limit: Truncate to this many results; None returns all matches.

        Returns:
            (doc_id, score) pairs sorted by descending score.
        """
        keyword_index = self._get_keyword_index()
        tokens = set(expanded_query_tokens)
        if not tokens:
            return []

        scored: List[Tuple[str, float]] = []
        for doc_id, doc_tokens in keyword_index.doc_tokens.items():
            norm = keyword_index.length_norm(len(doc_tokens))
            score = 0.0
            for token in tokens:
                if token in doc_tokens:
                    score += keyword_index.idf(token) * norm
                elif len(token) >= 4 and any(
                    dt.startswith(token) or token.startswith(dt) for dt in doc_tokens
                ):
                    score += keyword_index.idf(token) * norm * self._PREFIX_MATCH_WEIGHT
            if score > 0:
                scored.append((doc_id, score))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored if limit is None else scored[:limit]

    @staticmethod
    def _weighted_coverage(
        query_tokens: Iterable[str],
        doc_tokens: Any,
        keyword_index: "_KeywordIndex",
    ) -> Tuple[float, int]:
        """
        Fraction of the query's IDF mass that a document covers.

        Weighting by IDF means a document matching only generic terms scores low
        even when it matches several of them, while one matching rare terms scores
        high — which is what stops coincidental name overlap (`get_page`) from
        outranking a semantic fit.

        Returns:
            (coverage in 0..1, count of matched query tokens)
        """
        total_weight = 0.0
        matched_weight = 0.0
        matched = 0
        for token in query_tokens:
            weight = keyword_index.idf(token)
            total_weight += weight
            if token in doc_tokens or any(token in dt or dt in token for dt in doc_tokens):
                matched_weight += weight
                matched += 1
        coverage = (matched_weight / total_weight) if total_weight > 0 else 0.0
        return coverage, matched

    @classmethod
    def _expand_with_synonyms(cls, tokens: List[str]) -> List[str]:
        expanded: set[str] = set(tokens)
        token_set = set(tokens)
        for cluster in cls._SYNONYM_CLUSTERS:
            normalized_cluster = {cls._normalize_token(term) for term in cluster}
            if token_set.intersection(normalized_cluster):
                expanded.update(normalized_cluster)
        return list(expanded)

    @classmethod
    def expand_tokens(cls, tokens: Any) -> set:
        """Public synonym expansion over token iterables (browse→browser, read→get, …)."""
        return set(cls._expand_with_synonyms(list(tokens or [])))

    @classmethod
    def semantic_name_overlap_count(cls, query: str, tool_names: List[str]) -> int:
        """
        Count how many tool names share intent tokens with the query.

        Uses the same synonym clusters as hybrid search boosting so ``browse``
        overlaps ``browser`` in names like ``core.http_get_browser``. Callers
        (e.g. agentic_loop) use this before deciding whether lexical fallback
        should run.
        """
        query_tokens = cls.expand_tokens(cls._tokenize_meaningful(query))
        if not query_tokens or not tool_names:
            return 0
        overlap = 0
        for tool_name in tool_names:
            if not isinstance(tool_name, str) or not tool_name:
                continue
            name_tokens = cls.expand_tokens(
                cls._tokenize_meaningful(tool_name.replace(".", " ").replace("_", " "))
            )
            if query_tokens.intersection(name_tokens):
                overlap += 1
        return overlap

    @classmethod
    def lexical_preselect_tools(
        cls,
        query: str,
        tool_registry: Any,
        limit: int,
    ) -> List[str]:
        """
        Lexical registry fallback when semantic discovery returns no useful candidates.

        Scores tools by synonym-aware token overlap on name + description so browse
        queries prefer ``core.http_get_browser`` / Playwright navigate over unrelated
        ``*_read`` tools that only share the literal token ``read``.
        """
        if not tool_registry:
            return []
        all_tools = tool_registry.list_items() or {}
        if not isinstance(all_tools, dict) or not all_tools:
            return []

        query_tokens = cls.expand_tokens(cls._tokenize_meaningful(query))
        if not query_tokens:
            return []

        ranked: List[tuple] = []
        for tool_name, tool in all_tools.items():
            if not isinstance(tool_name, str) or not tool_name:
                continue
            name_tokens = set(
                cls._tokenize_meaningful(tool_name.replace(".", " ").replace("_", " "))
            )
            desc_tokens = set(
                cls._tokenize_meaningful(str(getattr(tool, "description", "") or ""))
            )
            searchable_tokens = cls.expand_tokens(name_tokens | desc_tokens)
            if not searchable_tokens:
                continue
            exact_hits = len(query_tokens.intersection(searchable_tokens))
            if exact_hits == 0:
                continue
            name_hits = len(query_tokens.intersection(cls.expand_tokens(name_tokens)))
            score = float(exact_hits) + (0.5 * float(name_hits))
            ranked.append((score, tool_name))

        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [name for _, name in ranked[: max(1, limit)]]

    def _entry_visible_to_scope(
        self,
        entry: Dict[str, Any],
        *,
        tenant_id: Optional[str],
        motet_id: Optional[str],
        role: Optional[str],
        principal_id: Optional[str],
    ) -> bool:
        """Return True when an entry's scope keys allow the request context."""
        scope_keys = entry.get("scope_keys")
        if not scope_keys:
            return True

        req_tenant = tenant_id or "*"
        req_motet = motet_id or "*"
        req_role = role or "*"
        req_principal = principal_id or "*"

        for key in scope_keys:
            try:
                grant = ScopeGrant.from_scope_key(str(key))
                if grant.matches(req_tenant, req_motet, req_role, req_principal):
                    return True
            except Exception:
                continue
        return False
    
    def search_functions(
        self,
        query: str,
        top_k: int = 20,
        *,
        bm25_ratio: float = 0.5,
        enable_boosting: bool = True,
        conversation_history: Optional[List[Message]] = None,
        search_types: Optional[List[str]] = None,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        role: Optional[str] = None,
        principal_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Unified hybrid search (Valkey vector + app-layer keyword fusion) across tools and workflows.
        
        Post-processes results to boost:
        - Exact name matches (2x boost)
        - Keyword-overlap matches with lightweight synonym expansion
        
        Args:
            query: Search query text
            top_k: Maximum number of results to return
            bm25_ratio: Balance between keyword and vector signals (0.0 = vector only, 1.0 = keyword only)
            enable_boosting: If True, apply custom boosting for exact/keyword matches
            conversation_history: Optional conversation history to enhance search query with context
            search_types: Optional list of types to include in results. Valid values: "tool", "workflow", "command".
                         If None (default), includes all types. Useful for filtering (e.g., ["tool", "workflow"] 
                         for tool discovery, or ["command"] for command discovery).
        
        Returns:
            List of dicts with:
            - type: "tool", "workflow", or "command" (filtered by search_types if provided)
            - name: tool_name, workflow_function_name, or command_type
            - metadata: full metadata
            - similarity_score: hybrid similarity score (combined keyword + vector)
        """
        if not self._initialized:
            raise RuntimeError("FunctionDiscoveryVectorStore is not indexed; hybrid search is required.")
        
        # Start timing
        t0_search = time.perf_counter()
        
        # Enhance query with conversation context
        enhanced_query = query
        context_extraction_ms = 0.0
        if conversation_history:
            t0_context = time.perf_counter()
            context = self._extract_context_from_history(conversation_history)
            context_extraction_ms = (time.perf_counter() - t0_context) * 1000
            if context:
                enhanced_query = f"{query} {context}".strip()
                logger.debug(
                    "enhanced_search_query_with_context",
                    original_query=query,
                    context=context,
                    enhanced_query=enhanced_query
                )
        
        # Normalize query for keyword matching (same as indexing: replace underscores with spaces)
        normalized_query = enhanced_query.replace("_", " ")

        # Build normalized tokens once for boosting.
        query_tokens = self._tokenize_meaningful(normalized_query)
        expanded_query_tokens = self._expand_with_synonyms(query_tokens)

        items: List[Dict[str, Any]] = []

        if self._hybrid_retriever is None:
            raise RuntimeError("Hybrid search is required but HybridRetriever is not initialized.")

        # Hybrid path: trust returned IDs as doc_ids.
        # If filtering by type, request more results to compensate for filtering
        # Normalize search_types to set for efficient lookup
        allowed_types = set(search_types) if search_types else None
        # Fusion pool, not the answer size. RRF can only rank what it is given, so
        # a pool of top_k * 2 meant the vector half contributed ~10 of a few
        # hundred documents and any tool it ranked poorly was unrecoverable.
        n_results = max(top_k * (3 if allowed_types else 2), self._MIN_FUSION_POOL)
        t0_hybrid = time.perf_counter()
        hybrid_results = self._hybrid_retriever.query(
            query_texts=[normalized_query],
            n_results=n_results,
            include=["ids", "distances"],
            bm25_ratio=bm25_ratio,
        )
        hybrid_query_ms = (time.perf_counter() - t0_hybrid) * 1000
        if not isinstance(hybrid_results, dict):
            raise RuntimeError(
                f"Expected dict from hybrid_retriever.query(), got {type(hybrid_results).__name__}: {hybrid_results}"
            )

        ids_data = hybrid_results.get("ids") or []
        distances_data = hybrid_results.get("distances") or []

        result_ids = ids_data[0] if isinstance(ids_data, list) and ids_data and isinstance(ids_data[0], list) else ids_data
        result_distances = (
            distances_data[0]
            if isinstance(distances_data, list) and distances_data and isinstance(distances_data[0], list)
            else distances_data
        )

        # ADR-0075: Valkey vector KNN + application-layer keyword fusion (RRF).
        vector_ids: List[str] = []
        for x in list(result_ids or []):
            if isinstance(x, str):
                vector_ids.append(x)
            else:
                vector_ids.append(str(x))
        vector_rank: Dict[str, int] = {doc_id: idx + 1 for idx, doc_id in enumerate(vector_ids)}
        keyword_index = self._get_keyword_index()
        keyword_rank: Dict[str, int] = {
            doc_id: idx
            for idx, (doc_id, _score) in enumerate(
                self._rank_by_keywords(
                    expanded_query_tokens, limit=max(n_results, top_k * 3)
                ),
                start=1,
            )
        }

        k = self._RRF_K
        rrf_scores: List[Tuple[str, float]] = []
        all_ids = set(vector_rank.keys()) | set(keyword_rank.keys())
        vector_weight = 1.0 - min(max(float(bm25_ratio), 0.0), 1.0)
        keyword_weight = 1.0 - vector_weight
        for doc_id in all_ids:
            score = 0.0
            v_rank = vector_rank.get(doc_id)
            k_rank = keyword_rank.get(doc_id)
            if v_rank:
                score += vector_weight / (k + float(v_rank))
            if k_rank:
                score += keyword_weight / (k + float(k_rank))
            rrf_scores.append((doc_id, score))
        rrf_scores.sort(key=lambda x: x[1], reverse=True)
        rrf_scores = rrf_scores[: max(top_k * 3, top_k)]
        result_ids = [doc_id for doc_id, _ in rrf_scores]
        result_distances = [max((1.0 / max(score, 1e-9)) - 1.0, 0.0) for _doc_id, score in rrf_scores]

        filtered_count = 0
        for idx, doc_id in enumerate(result_ids or []):
            entry = self._id_to_entry.get(doc_id)
            if not entry:
                logger.debug(
                    "function_discovery_hybrid_id_not_in_index",
                    query=query,
                    doc_id=doc_id,
                    note="Skipping id not present in current tool/workflow index",
                )
                continue

            # Filter by search_types if specified
            entry_type = entry.get("type")
            if allowed_types and entry_type not in allowed_types:
                filtered_count += 1
                continue

            if not self._entry_visible_to_scope(
                entry,
                tenant_id=tenant_id,
                motet_id=motet_id,
                role=role,
                principal_id=principal_id,
            ):
                filtered_count += 1
                continue

            distance = result_distances[idx] if idx < len(result_distances or []) else 1.0
            similarity_score = 1.0 / (1.0 + float(distance)) if distance and float(distance) > 0 else 1.0

            # Apply boosting
            if enable_boosting:
                original_score = similarity_score
                if entry["type"] == "tool":
                    name_for_boosting = entry.get("tool_name", "") or ""
                elif entry["type"] == "workflow":
                    name_for_boosting = entry.get("workflow_function_name", "") or ""
                elif entry["type"] == "command":
                    name_for_boosting = entry.get("command_type", "") or ""
                else:
                    name_for_boosting = ""

                if name_for_boosting.lower() == normalized_query.lower():
                    similarity_score *= 2.0
                    logger.debug(
                        "exact_match_boosted",
                        query=query,
                        name=name_for_boosting,
                        original_score=original_score,
                        boosted_score=similarity_score,
                    )
                else:
                    # Coverage is measured against the *original* query tokens —
                    # synonym expansion is for matching only. Using the expanded
                    # set as the denominator punished intent phrases, whose
                    # expansion is large, relative to short keyword bags.
                    #
                    # Weighting each token by IDF means a document matching only
                    # generic terms (get / content) scores low even if it matches
                    # several, while one matching rare terms scores high. Coverage
                    # is computed over the full entry text (name + description +
                    # keywords), not the name alone, so semantic fit outranks
                    # coincidental name-token overlap.
                    doc_tokens = keyword_index.doc_tokens.get(doc_id) or set()
                    if query_tokens and doc_tokens:
                        coverage, matched = self._weighted_coverage(
                            query_tokens, doc_tokens, keyword_index
                        )
                        if matched >= 2 or coverage >= 0.35:
                            if coverage >= 0.70:
                                factor = 1.5
                            elif coverage >= 0.50:
                                factor = 1.35
                            else:
                                factor = 1.2
                            similarity_score *= factor
                            logger.debug(
                                "keyword_overlap_boosted",
                                query=query,
                                name=name_for_boosting,
                                matched_tokens=matched,
                                query_token_count=len(query_tokens),
                                coverage=round(coverage, 3),
                                factor=factor,
                                original_score=original_score,
                                boosted_score=similarity_score,
                            )

            if entry["type"] == "tool":
                items.append(
                    {
                        "type": "tool",
                        "name": entry["tool_name"],
                        "metadata": dict(entry),
                        "similarity_score": similarity_score,
                    }
                )
            elif entry["type"] == "workflow":
                items.append(
                    {
                        "type": "workflow",
                        "name": entry["workflow_function_name"],
                        "workflow_id": entry["workflow_id"],
                        "metadata": dict(entry),
                        "similarity_score": similarity_score,
                    }
                )
            elif entry["type"] == "command":
                items.append(
                    {
                        "type": "command",
                        "name": entry["command_type"],
                        "command_type": entry["command_type"],
                        "description": entry.get("description", ""),
                        "metadata": dict(entry),
                        "similarity_score": similarity_score,
                    }
                )
        
        # Sort by similarity score (descending) after boosting
        items.sort(key=lambda x: x.get("similarity_score", 0.0), reverse=True)
        
        # Calculate total search time
        total_search_ms = (time.perf_counter() - t0_search) * 1000
        boosting_ms = total_search_ms - hybrid_query_ms - context_extraction_ms
        
        # Log timing information
        logger.info(
            "semantic_search_timing",
            query=query[:100],  # Truncate for logging
            top_k=top_k,
            results_count=len(items[:top_k]),
            total_ms=round(total_search_ms, 2),
            hybrid_query_ms=round(hybrid_query_ms, 2),
            context_extraction_ms=round(context_extraction_ms, 2),
            boosting_ms=round(boosting_ms, 2),
            has_context=bool(conversation_history),
            search_types=search_types,
            filtered_count=filtered_count
        )
        
        # Return top_k results
        items = items[:top_k]
        
        logger.debug(
            "function_discovery_search_completed",
            query=query,
            normalized_query=normalized_query,
            top_k=top_k,
            results_count=len(items),
            tool_count=sum(1 for item in items if item["type"] == "tool"),
            workflow_count=sum(1 for item in items if item["type"] == "workflow"),
            command_count=sum(1 for item in items if item["type"] == "command"),
            hybrid_search_used=self._hybrid_retriever is not None,
            boosting_enabled=enable_boosting
        )
        
        return items
    
    def is_initialized(self) -> bool:
        """Check if the vector store has been indexed."""
        return self._initialized


__all__ = ["FunctionDiscoveryVectorStore"]

