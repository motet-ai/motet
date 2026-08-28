"""Regression tests for tool discovery lexical fallback and local capabilities."""

from __future__ import annotations

from motet.core.tools.function_discovery_vector_store import FunctionDiscoveryVectorStore
from motet.core.workers.tasks import _detect_worker_capabilities


class _FakeTool:
    def __init__(self, description: str) -> None:
        self.description = description


class _FakeRegistry:
    def __init__(self, items: dict[str, _FakeTool]) -> None:
        self._items = items

    def list_items(self) -> dict[str, _FakeTool]:
        return self._items


def test_discovery_lexical_fallback_finds_clipboard_tools() -> None:
    registry = _FakeRegistry(
        {
            "core.clipboard_write": _FakeTool("Write text to clipboard on local worker"),
            "core.clipboard_read": _FakeTool("Read plain text from clipboard"),
            "mcp.playwright.browser_take_screenshot": _FakeTool("Take browser screenshot"),
        }
    )

    selected = FunctionDiscoveryVectorStore.lexical_preselect_tools(
        query='save "test" to clipboard',
        tool_registry=registry,
        limit=5,
    )

    assert "core.clipboard_write" in selected
    assert "core.clipboard_read" in selected
    assert "mcp.playwright.browser_take_screenshot" not in selected


def test_semantic_name_overlap_browse_matches_browser_tools() -> None:
    """browse↔browser synonym expansion must count http_get_browser / playwright navigate."""
    tools = [
        "core.http_get_browser",
        "mcp.playwright.browser_navigate",
        "mcp.google_workspace.draft_gmail_message",
    ]
    short = FunctionDiscoveryVectorStore.semantic_name_overlap_count(
        "browse cnn.com and read it", tools
    )
    long = FunctionDiscoveryVectorStore.semantic_name_overlap_count(
        "browse cnn.com and read it to me", tools
    )
    assert short >= 2
    assert long >= 2


def test_discovery_lexical_fallback_prefers_http_get_browser_for_browse_query() -> None:
    """Lexical fallback should surface page-reader tools for browse intents, not only *_read."""
    registry = _FakeRegistry(
        {
            "core.http_get_browser": _FakeTool(
                "Fetch and read content from any website URL using a real browser"
            ),
            "core.artifact_read": _FakeTool("Read the full derived text of an attached artifact"),
            "core.file_read": _FakeTool("Read a local file from the edge worker"),
            "mcp.playwright.browser_navigate": _FakeTool("Navigate the browser to a URL"),
        }
    )

    selected = FunctionDiscoveryVectorStore.lexical_preselect_tools(
        query="browse cnn.com and read it",
        tool_registry=registry,
        limit=5,
    )

    assert "core.http_get_browser" in selected
    assert selected[0] in {
        "core.http_get_browser",
        "mcp.playwright.browser_navigate",
    }


def test_edge_worker_clipboard_capability_follows_registered_tools(monkeypatch) -> None:
    monkeypatch.setenv("MOTET_WORKER_ID", "edge_test_worker")
    monkeypatch.setenv("MOTET_EDGE_WORKER_ID", "edge_test_worker")
    monkeypatch.delenv("MOTET_CLIPBOARD_BRIDGE_URL", raising=False)

    caps = _detect_worker_capabilities(stack=object(), available_tools=["core.clipboard_read"])

    assert "edge_clipboard" in caps
    assert "edge_execution" in caps
    assert "tool_execution" in caps


def test_edge_worker_no_clipboard_tool_no_clipboard_capability(monkeypatch) -> None:
    monkeypatch.setenv("MOTET_WORKER_ID", "edge_test_worker")
    monkeypatch.setenv("MOTET_EDGE_WORKER_ID", "edge_test_worker")
    monkeypatch.delenv("MOTET_CLIPBOARD_BRIDGE_URL", raising=False)

    caps = _detect_worker_capabilities(
        stack=object(),
        available_tools=["core.file_read", "mcp.playwright.browser_take_screenshot"],
    )

    assert "edge_clipboard" not in caps


def test_edge_worker_advertises_file_edit_and_grep_capabilities(monkeypatch) -> None:
    """ADR-0122: file_edit → EDGE_FILE_WRITE; file_grep → EDGE_FILE_SEARCH."""
    monkeypatch.setenv("MOTET_WORKER_ID", "edge_app_builder")
    monkeypatch.setenv("MOTET_EDGE_WORKER_ID", "edge_app_builder")

    caps = _detect_worker_capabilities(
        stack=object(),
        available_tools=["core.file_edit", "core.file_grep"],
    )

    assert "edge_execution" in caps
    assert "edge_file_write" in caps
    assert "edge_file_search" in caps
    assert "worker_shell_exec" in caps


def test_cloud_worker_advertises_embeddings_when_service_is_available(monkeypatch) -> None:
    monkeypatch.setenv("MOTET_WORKER_ID", "cloud_worker")
    monkeypatch.delenv("MOTET_EDGE_WORKER_ID", raising=False)

    caps = _detect_worker_capabilities(
        stack=object(),
        available_tools=[],
        embedding_service=object(),
    )

    assert "vector_operations" in caps
    assert "embeddings" in caps
