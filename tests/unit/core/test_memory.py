import asyncio
import httpx
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from motet.interfaces.http import create_app
from motet.core.memory.redis_store import RedisStore
from motet.core.memory.manager import MemoryManager
from motet.core.types import MemoryItem


def test_memory_consolidation_promotes_long_text(monkeypatch):
    # Consolidation is API/command driven (no unread MOTET_CONSOLIDATION_* knobs).
    monkeypatch.setenv("MOTET_ENABLE_VECTOR_MEMORY", "true")

    # Override JWT config to avoid auth on endpoints for this test
    monkeypatch.setenv("MOTET_JWT_JWKS_URL", "")
    monkeypatch.setenv("MOTET_JWT_PUBLIC_KEY_PEM", "")
    # Ensure API key is not required
    monkeypatch.setenv("MOTET_API_KEY", "")
    # Allow insecure principal headers for in-process ASGI test
    monkeypatch.setenv("MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS", "true")
    app = create_app()
    transport = httpx.ASGITransport(app=app)

    async def _run():
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Add some messages to memory via chat calls
            long_text = "This is a sufficiently long memory entry for consolidation."
            auth_headers = {"X-API-Key": "", "X-Principal-Id": "test-principal", "X-Tenant-Id": "test-tenant"}
            r = await client.post("/api/v1/chat", json={"messages":[{"role":"user","content": long_text}], "stream": False}, headers=auth_headers)
            assert r.status_code == 200
            # Trigger consolidation via endpoint
            r = await client.post("/api/v1/memories/consolidate", headers=auth_headers)
            assert r.status_code == 200
            # Check vector summaries list for tag
            r = await client.get("/api/v1/memories/vector/list", params={"limit": 10, "tag": "episodic"}, headers=auth_headers)
            assert r.status_code == 200
            # Either nothing (if vector disabled in env) or a list
            assert isinstance(r.json(), list)

    asyncio.run(_run())


def test_redis_store_defaults_tenant_to_default():
    store = RedisStore(redis_client=object(), motet_id="default")
    assert store._tenant_id == "default"
    assert store._prefix.startswith("default:mem:default:")


def test_memory_manager_store_requires_identity_context():
    manager = MemoryManager(SimpleNamespace(memory=object(), vector=None, config=None))

    try:
        manager.store_memory(content="x", type="note", tags=["t"])
        assert False, "Expected ValueError when MotetContext is missing"
    except ValueError as e:
        assert "store_memory requires MotetContext" in str(e)


def test_memory_manager_recall_requires_identity_context():
    manager = MemoryManager(SimpleNamespace(memory=object(), vector=None, config=None))

    try:
        manager.recall(tags=["t"], limit=5)
        assert False, "Expected ValueError when MotetContext is missing"
    except ValueError as e:
        assert "recall requires MotetContext" in str(e)


def test_memory_manager_recall_conversation_requires_identity_context():
    manager = MemoryManager(SimpleNamespace(memory=object(), vector=None, config=None))

    try:
        manager.recall_conversation(conversation_id="conv-1", limit=5)
        assert False, "Expected ValueError when MotetContext is missing"
    except ValueError as e:
        assert "recall_conversation requires MotetContext" in str(e)


def test_memory_manager_hybrid_retrieve_requires_identity_context():
    manager = MemoryManager(SimpleNamespace(memory=object(), vector=None, config=None))

    try:
        manager.hybrid_retrieve(query="hello", limit=5)
        assert False, "Expected ValueError when MotetContext is missing"
    except ValueError as e:
        assert "hybrid_retrieve requires MotetContext" in str(e)


def test_memory_manager_store_with_explicit_context_succeeds():
    class _Mem:
        def __init__(self) -> None:
            self.items = []

        def upsert(self, item):
            self.items.append(item)

    mem = _Mem()
    manager = MemoryManager(SimpleNamespace(memory=mem, vector=None, config=None))
    ctx = SimpleNamespace(
        principal_id="user-1",
        tenant_id="tenant-1",
        motet_id="motet-1",
        conversation_id="conv-1",
    )

    result = manager.store_memory(
        content="hello",
        type="note",
        tags=["demo"],
        motet_context=ctx,
    )

    assert result.get("id")
    assert "memory" in (result.get("stored_in") or [])
    assert len(mem.items) == 1
    stored = mem.items[0]
    assert stored.principal_id == "user-1"
    assert stored.tenant_id == "tenant-1"
    assert stored.motet_id == "motet-1"


def test_hybrid_retrieve_does_not_implicitly_scope_vector_to_conversation():
    """Vector branch must not inject identity conversation_id unless caller asked."""

    class _Mem:
        def recent(self, limit: int):
            return []

    class _Vector:
        def __init__(self):
            self.kwargs = None

        def query(self, query, top_k=5, **kwargs):
            self.kwargs = {"query": query, "top_k": top_k, **kwargs}
            return []

    vector = _Vector()
    manager = MemoryManager(SimpleNamespace(memory=_Mem(), vector=vector, config=None))
    ctx = SimpleNamespace(
        principal_id="user-1",
        tenant_id="tenant-1",
        motet_id="motet-1",
        conversation_id="conv-current",
        agent_id=None,
    )

    manager.hybrid_retrieve(
        query="boats",
        tags=["deep-research"],
        limit=3,
        min_relevance=0.0,
        motet_context=ctx,
    )

    assert vector.kwargs is not None
    assert vector.kwargs.get("conversation_id") is None
    assert vector.kwargs.get("principal_id") == "user-1"


def test_memory_manager_hybrid_retrieve_query_with_tags_includes_older_candidates():
    """Tagged query recall should search a broader recent window and find older matches."""

    class _Mem:
        def __init__(self, items):
            self._items = items

        def recent(self, limit: int):
            return self._items[:limit]

    now = datetime.now(timezone.utc)
    non_matching = [
        MemoryItem(
            id=f"m{i}",
            type="research_report",
            content=f"unrelated topic {i}",
            tags=["deep-research"],
            metadata={"topic": f"other {i}"},
            created_at=now - timedelta(minutes=i),
        )
        for i in range(6)
    ]
    banana = MemoryItem(
        id="banana-hit",
        type="research_report",
        content="banana market outlook report",
        tags=["deep-research"],
        metadata={"topic": "banana research"},
        created_at=now - timedelta(minutes=7),
    )
    mem = _Mem(non_matching + [banana])
    manager = MemoryManager(SimpleNamespace(memory=mem, vector=None, config=None))
    ctx = SimpleNamespace(
        principal_id="user-1",
        tenant_id="tenant-1",
        motet_id="motet-1",
        conversation_id=None,
    )

    found = manager.hybrid_retrieve(
        query="banana",
        tags=["deep-research"],
        limit=3,  # small limit, but tagged-query window should be broadened
        motet_context=ctx,
    )

    assert any(m.id == "banana-hit" for m in found)


def test_apply_vector_recall_uses_passed_memory_items():
    """apply_vector_recall should skip internal hybrid_retrieve when memory_items is set (#132)."""
    from motet.core.types import Message

    manager = MemoryManager(SimpleNamespace(memory=None, vector=None, config=None))

    class _FakeItem:
        def __init__(self, content: str, score: float):
            self.content = content
            self.metadata = {"hybrid_score": score}

    pre_retrieved = [_FakeItem("already fetched item", 0.85)]
    messages_in = [Message(role="user", content="query")]

    # Should succeed without needing hybrid_retrieve — no ValueError raised
    # when the stack has no real memory backend
    messages_out = manager.apply_vector_recall(
        messages=messages_in,
        query="ignored when memory_items is set",
        memory_items=pre_retrieved,
        max_context_items=3,
        min_relevance=0.5,
    )

    assert messages_out is not None
    assert len(messages_out) > len(messages_in), (
        "Expected memory context message to be inserted"
    )


def test_keyword_relevance_is_query_coverage_not_jaccard():
    """Long reports that name the topic in the head must score high; buried hits must not."""
    manager = MemoryManager(SimpleNamespace(memory=None, vector=None, config=None))

    matching = (
        "# Synthesis of AI and Future Jobs Analyses\n\n"
        "## Executive Summary\n\n"
        "The integration of Artificial Intelligence into the workforce "
        + ("padding " * 400)
    )
    # Put the shared words past the head window so only the body can match.
    buried = (
        "# Synthesis of the Four-Day Work Week Analyses\n\n"
        "## Executive Summary\n\n"
        + ("padding " * 80)
        + "Shorter weeks reshape how teams plan for the future, and the jobs "
        "most affected are those with rigid coverage requirements."
    )

    match_score = manager._calculate_keyword_relevance(
        "AI and future jobs",
        matching,
        metadata={"topic": "AI and future jobs"},
    )
    buried_score = manager._calculate_keyword_relevance(
        "AI and future jobs",
        buried,
        metadata={"topic": "four-day work week"},
    )

    assert match_score >= 0.8
    assert buried_score < 0.5


def test_keyword_relevance_keeps_two_character_topic_words():
    """'AI' carries the topic; dropping it made 'future jobs' match unrelated panels."""
    manager = MemoryManager(SimpleNamespace(memory=None, vector=None, config=None))
    score = manager._calculate_keyword_relevance(
        "AI regulation",
        "# Synthesis of AI Regulation Analyses\n\nPolicy trade-offs.",
    )
    assert score >= 0.8


def test_recall_principal_ranks_and_filters_by_query():
    """Topic recall on principal scope should not return unrelated recent memories."""
    from motet.core.types import MemoryScopeType

    now = datetime.now(timezone.utc)

    class _Mem:
        def by_principal(self, *, principal_id, limit, types=None):
            items = [
                MemoryItem(
                    id="work-week",
                    type="research_report",
                    content=(
                        "# Synthesis of the Four-Day Work Week Analyses\n\n"
                        + ("padding " * 80)
                        + "Teams plan for the future of jobs under coverage constraints."
                    ),
                    tags=["deep-research", "research_report"],
                    metadata={"topic": "four-day work week"},
                    created_at=now,
                    scope_type=MemoryScopeType.PRINCIPAL.value,
                    principal_id=principal_id,
                ),
                MemoryItem(
                    id="ai-jobs",
                    type="research_report",
                    content="# Synthesis of AI and Future Jobs Analyses\n\n"
                    "AI reshapes labor markets.",
                    tags=["deep-research", "research_report"],
                    metadata={"topic": "AI and future jobs"},
                    created_at=now - timedelta(minutes=5),
                    scope_type=MemoryScopeType.PRINCIPAL.value,
                    principal_id=principal_id,
                ),
            ]
            if types:
                items = [m for m in items if m.type in types]
            return items[:limit]

    manager = MemoryManager(SimpleNamespace(memory=_Mem(), vector=None, config=None))
    ctx = SimpleNamespace(
        principal_id="user-1",
        tenant_id="tenant-1",
        motet_id="motet-1",
        conversation_id=None,
    )

    found = manager.recall_principal(
        principal_id="user-1",
        query="AI and future jobs",
        tags=["deep-research"],
        types=["research_report"],
        min_relevance=0.8,
        limit=5,
        motet_context=ctx,
    )

    assert [m.id for m in found] == ["ai-jobs"]
    assert found[0].metadata.get("relevance_score", 0) >= 0.8

