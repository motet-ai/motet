import os
import pytest


def _test_embedding(text: str) -> list[float]:
    """Small deterministic embedding for slim test-runner integration tests."""

    normalized = text.lower()
    return [
        1.0 if "hello" in normalized else 0.0,
        1.0 if "goodbye" in normalized else 0.0,
        1.0 if "moon" in normalized else 0.0,
    ]


@pytest.mark.skipif(not os.getenv("MOTET_PGVECTOR_DSN") and not os.getenv("CI_PGVECTOR"), reason="pgvector DSN not set")
def test_pgvector_basic_add_and_query():
    from motet.core.memory import PGVectorStore
    from motet.core.types import MemoryItem
    dsn = os.environ.get("MOTET_PGVECTOR_DSN") or "postgresql://motet:example@localhost:5432/imf"
    table = os.environ.get("MOTET_PGVECTOR_TABLE", "imf_embeddings_test")
    store = PGVectorStore(dsn=dsn, table=table, embedding_fn=_test_embedding, embedding_dim=3)
    items = [
        MemoryItem(id="pgt1", type="rag_chunk", content="hello world", tags=["test"], metadata={}),
        MemoryItem(id="pgt2", type="rag_chunk", content="goodbye moon", tags=["test"], metadata={}),
    ]
    store.add(items)
    res = store.query("hello", top_k=1)
    assert res and any("hello" in it.content for it in res)

