from types import SimpleNamespace

from motet.core.memory.manager import MemoryManager
from motet.core.types import MemoryItem


class _FakeStore:
    def __init__(self) -> None:
        self.last_item = None

    def upsert(self, item: MemoryItem) -> None:
        self.last_item = item


def _ctx(agent_id: str = "core.default") -> SimpleNamespace:
    return SimpleNamespace(
        motet_id="default",
        tenant_id="motet-global",
        principal_id="principal-1",
        conversation_id="conv-1",
        metadata={"agent_id": agent_id},
    )


def test_store_memory_adds_agent_tag_and_metadata() -> None:
    store = _FakeStore()
    stack = SimpleNamespace(
        config=SimpleNamespace(
            memory_working_tag="wm",
            memory_short_term_tag="stm",
            memory_long_term_tag="ltm",
            store_assistant_vector=False,
            enable_vector_memory=False,
            memory_agent_scope_mode="prefer",
            memory_agent_tag_prefix="agent:",
        ),
        memory=store,
        vector=None,
    )
    manager = MemoryManager(stack)

    manager.store_memory(
        content="hello",
        type="assistant_response",
        tags=["conversation:conv-1"],
        metadata={},
        motet_context=_ctx("core.default"),
    )

    assert store.last_item is not None
    assert "agent:core.default" in (store.last_item.tags or [])
    assert store.last_item.metadata.get("agent_id") == "core.default"


def test_store_memory_adds_conversation_scope_tag() -> None:
    """KV stores must tag conversation scope so hybrid_retrieve can filter."""
    store = _FakeStore()
    stack = SimpleNamespace(
        config=SimpleNamespace(
            memory_working_tag="wm",
            memory_short_term_tag="stm",
            memory_long_term_tag="ltm",
            store_assistant_vector=False,
            enable_vector_memory=False,
            memory_agent_scope_mode="prefer",
            memory_agent_tag_prefix="agent:",
        ),
        memory=store,
        vector=None,
    )
    manager = MemoryManager(stack)

    manager.store_memory(
        content="hello",
        type="note",
        tags=["agent_scope_test"],
        metadata={},
        motet_context=_ctx("core.foreign"),
    )

    assert store.last_item is not None
    assert "conversation:conv-1" in (store.last_item.tags or [])
    assert store.last_item.conversation_id == "conv-1"


def test_apply_agent_scope_prefer_and_strict() -> None:
    stack = SimpleNamespace(config=SimpleNamespace(memory_agent_scope_mode="prefer", memory_agent_tag_prefix="agent:"))
    manager = MemoryManager(stack)
    identity = SimpleNamespace(agent_id="core.default")

    same = MemoryItem(id="1", type="assistant_response", content="a", tags=["agent:core.default"], metadata={})
    other = MemoryItem(id="2", type="assistant_response", content="b", tags=["agent:core.other"], metadata={})
    unknown = MemoryItem(id="3", type="assistant_response", content="c", tags=[], metadata={})

    preferred = manager._apply_agent_scope(items=[other, same, unknown], identity_context=identity, cfg=stack.config)
    assert [i.id for i in preferred] == ["1"]

    # Prefer mode falls back when there are no same-agent matches.
    fallback = manager._apply_agent_scope(items=[other, unknown], identity_context=identity, cfg=stack.config)
    assert [i.id for i in fallback] == ["2", "3"]

    strict_cfg = SimpleNamespace(memory_agent_scope_mode="strict", memory_agent_tag_prefix="agent:")
    strict = manager._apply_agent_scope(items=[other, unknown], identity_context=identity, cfg=strict_cfg)
    assert strict == []


def test_resolve_memory_agent_scope_mode_prefers_metadata() -> None:
    stack = SimpleNamespace(config=SimpleNamespace(memory_agent_scope_mode="prefer"))
    manager = MemoryManager(stack)
    motet = SimpleNamespace(metadata={"memory_agent_scope_mode": "strict"})
    assert manager._resolve_memory_agent_scope_mode(stack.config, motet) == "strict"
