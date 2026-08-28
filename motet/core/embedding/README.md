## Package: embedding

**Runtime embedding subsystem** for text embeddings, sibling-server access, and future multimodal embedding support.

### Purpose
- **Stable worker API**: `EmbeddingService` preserves the existing `embed`, `embed_batch`, and `get_embedding_dimension` interface.
- **Topology selection**: `create_embedding_service` chooses in-process or sibling-server execution from configuration.
- **Service boundary**: `server/` exposes the FastAPI app for deployments that hoist embedding models out of worker processes.
- **Future multimodal support**: The package gives text and future image-text embedding code one owner instead of spreading it through generic services.

### Components
- `service.py`: Topology-aware `EmbeddingService` facade.
- `backends.py`: In-process `SentenceTransformer` backend and shared backend protocol.
- `client.py`: HTTP client for the sibling embedding server.
- `server/`: FastAPI app, request/response models, and module entrypoint. ``GET /healthz`` includes ``motet_version`` for ``GET /api/v1/version``.

### Runtime
- Distributed Docker dev uses `docker/images/embedding-server/` for the sibling server image.
- Local-edge compose runs the server beside the worker in the WireGuard network namespace and points the worker at `http://localhost:8091`.
- Distributed compose points workers at `http://embedding-server:8091`.

### Compatibility
- Existing imports from `motet.core.services.embedding_service` continue to work through a re-export shim.
- New embedding code should import from `motet.core.embedding`.
