## Package: memory

**Distributed memory management system** with hierarchical memory tiers, vector similarity search, and intelligent retrieval strategies.

### Purpose
- **Distributed Memory Operations**: All memory operations execute as distributed commands
- **Hierarchical Memory**: Working, short-term, and long-term memory tiers with intelligent routing
- **Vector Similarity Search**: Semantic search via Valkey Search
- **Hybrid Retrieval**: Combined keyword, semantic, and temporal relevance scoring
- **Memory Consolidation**: Promote important short-term memories into long-term storage
- **Multi-Backend Support**: Flexible backend selection with consistent interfaces

### Core Components

#### Memory Manager (`manager.py`)
Central memory coordination with distributed command integration:
- **Hierarchical Routing**: Intelligent routing across working, short-term, and long-term memory
- **Hybrid Retrieval**: `hybrid_retrieve` with keyword, semantic, and temporal scoring
- **Keyword relevance**: query coverage over whole-word tokens, biased toward the
 document head / `metadata.topic` / tags (not Jaccard). Long reports stay findable;
 a word buried in the body cannot clear typical `min_relevance` floors alone.
- **Principal topic recall**: `recall_principal(query=..., tags=..., min_relevance=...)`
 ranks and filters principal-scoped memories without a local scorer in the caller.
- **Memory Consolidation**: `consolidate_memories` to promote STM → LTM
- **Vector Recall**: `apply_vector_recall` for semantic memory enhancement
- **Distributed Integration**: All operations available as distributed commands

#### Storage Backends
- **`inmemory.py`**: `InMemoryStore` - Fast in-memory storage with TTL support
- **`redis_store.py`**: `RedisStore` - Durable Redis-based storage for session data. `conversation_index_count` returns the conversation zset size without decrypting payloads.
- **`valkey_vector_store.py`**: `ValkeyVectorStore` - Valkey Search `FT.SEARCH` KNN + TAG filters for LTM vectors (only supported backend)

#### Infrastructure
- **`base.py`**: Abstract base classes and interfaces for all storage backends
- **`__init__.py`**: Store registry and backend selection logic

### Notes
- Use `store_registry.register/build/get/list/supports` for a consistent API across backends.
- Registries conform to `BaseRegistry[T]` (see `motet.core.types`).
- `GET /api/v1/memories/search` delegates to the `core.memory_recall` distributed command (`mode="semantic"`) so query embedding and KNN run on workers, not in the HTTP process.

### Implemented
- InMemory/Redis KV stores with `upsert/get/delete/search_by_tag/recent` helpers.
- Vector store: Valkey Search (HASH + HNSW; separate index/prefix from function discovery). Chroma and PGVector remain available for non-memory use (e.g. migration, tests).
- Vector tag updates: `update_tags(ids, tags, op, *, tenant_id, principal_id,...)` implemented for Valkey, Chroma, and PGVector. Valkey implementation verifies document ownership when identity filters are provided.
- Targeted forget: `MemoryManager.forget(...)` deletes KV rows via `BaseStore.delete` and matching vector docs via `delete_ids`. Same selectors as `retag` (`memory_ids`, `conversation_id`, or `filter_tag`). Conversation and tag together intersect. Agent tool is `core.memory_forget`; operator HTTP clear is unchanged.
- Hierarchical routing via `MemoryManager.store_memory(...)` and ordered recall via `MemoryManager.recall(...)` (working → short-term → long-term).
- Conversation scope tags: when `conversation_id` is present, `store_memory` auto-adds `conversation:{id}` so `hybrid_retrieve` / chat prepare can filter by that tag (not only `MemoryItem.conversation_id`).
- Conversation attribution: `store_memory` files the item under `metadata["conversation_id"]` when set, falling back to the caller's context id. This lets a command write rows onto another conversation (e.g. `core.spawn_agents` persisting a child's first turn from the parent's tool context).
- Working-memory reset each turn (configurable).

### Planned
- Composite memory collections (conversation, project) and insights engine.
- Event-driven learning observers for access patterns and consolidation.
- Cross-store health checks and richer `/health` details.
- Fused retrieval improvements and cache hit metrics.

### Typical uses
- Conversation history, tool observations, reflection suggestions, and RAG embeddings.
- Working memory for per-turn scratch (e.g., assistant response), short-term for session context, long-term for summaries/knowledge.

### When memories get vector-indexed

Vector indexing is controlled by several config options that work together:

| Config | Default | Effect |
|--------|---------|--------|
| `MOTET_ENABLE_VECTOR_MEMORY` | `false` | Master switch: must be `true` for any vector indexing |
| `MOTET_VECTOR_BACKEND` | `valkey` | Memory uses Valkey only; chroma/pgvector not used for memory |
| `MOTET_STORE_ASSISTANT_VECTOR` | `false` | When `true`, `assistant_response` memories are indexed |

**Flow:** A memory is written to KV first. If it's destined for LTM (e.g. `user_message` when vector enabled, or `assistant_response` when `store_assistant_vector`), then `core.memory_vector_index` is always dispatched fire-and-forget. Indexing is async; no inline `vector.add`.

### Configuration (env)
- `MOTET_MEMORY_WORKING_TAG` (default `wm`)
- `MOTET_MEMORY_SHORT_TERM_TAG` (default `stm`)
- `MOTET_MEMORY_LONG_TERM_TAG` (default `ltm`)
- `MOTET_WORKING_MEMORY_RESET_EACH_TURN` (default `true`)
- `MOTET_ENABLE_VECTOR_MEMORY`, `MOTET_VECTOR_BACKEND=valkey`, plus Valkey-specific settings
- Valkey LTM index (optional env overrides): `MOTET_MEMORY_VECTOR_VALKEY_INDEX`, `MOTET_MEMORY_VECTOR_VALKEY_PREFIX` (defaults in `valkey_vector_store.py`)
- Async LTM embedding: `MemoryManager.store_memory` always dispatches `core.memory_vector_index` after a successful KV write. No inline `vector.add`; indexing is always async.
- Tenant/principal/conversation isolation: `ValkeyVectorStore.query` and `list_by_tag` accept optional `tenant_id`, `principal_id`, `conversation_id`, `motet_id`. When provided, these are applied as TAG predicates in FT.SEARCH so KNN never returns cross-tenant documents. Memory commands and APIs pass identity context into vector operations.
- Agent isolation: The FT.CREATE schema includes a dedicated `agent_id` TAG field. When `memory_agent_scope_mode` is `strict` or `prefer`, the raw `agent_id` is passed to FT.SEARCH via `@agent_id:{value}` so KNN returns only same-agent memories. The `agent:` prefix tag is still written to `user_tags` for CLI queries (e.g. `--tag agent:core.default`). Applied in memory_recall (semantic), memory_search, hybrid_retrieve, and recall vector augmentation.
- Agent-scope mode override: Chat stamps `memory_agent_scope_mode` from API Config into turn metadata (`SCHEDULE_CONTEXT_KEYS`). `MemoryManager` / memory commands prefer that metadata over the worker stack Config so gateway policy applies when worker boot env differs.
- Embedding decoupling: `ValkeyVectorStore` accepts optional `embedding_fn` and `embedding_dim` to delegate embedding to an external service. `core.memory_vector_index` uses the centralized `EmbeddingService` from worker context when available, calling `add_with_vectors` with pre-computed vectors. Falls back to the store's internal `SentenceTransformer` when no external embedding function is provided.
- Agent-aware memory segmentation: `MOTET_MEMORY_AGENT_SCOPE_MODE=disabled|prefer|strict` (default `prefer`)
- Agent facet tag prefix: `MOTET_MEMORY_AGENT_TAG_PREFIX` (default `agent:`)

