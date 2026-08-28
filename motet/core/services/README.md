## Package: services

**Compatibility exports** for the embedding subsystem.

### Purpose
- Preserve the stable import path `motet.core.services.embedding_service` / package-level
 `EmbeddingService` / `create_embedding_service` used by existing workers and tests.
- New embedding code should import from `motet.core.embedding`.

### Components
- `embedding_service.py`: Re-exports `motet.core.embedding` (facade, backends, HTTP client).
- `__init__.py`: Package-level re-exports of `EmbeddingService` and `create_embedding_service`.

### Removed (#175)
- `ingestion.py` (`IngestionService`) — unused outside a single integration test
- `retrieval.py` (`bm25_scores` / `bm25_search`) — no live importers
- `summarization.py` (`Summarizer`) — constructed on `MotetStack` but never called

Memory ingest/index/search and any future summarization strategy live under
`motet/core/memory/`, APIs, and related tickets (#180), not this package.

### Related Packages
- `motet/core/embedding/`: Embedding subsystem (topology-aware facade, sibling server)
- `motet/core/memory/`: Memory manager, Valkey vector store, hybrid retrieve
