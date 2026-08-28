"""
Contract tests for ToolDiscoveryService after NFC removal (issue #112 / ADR-0074).

Locks the post-cleanup surface before production deletion:
- native_function_calling module and NativeFunctionCallingService are gone
- ToolDiscoveryService.discover_tools is embedding-only via FunctionDiscoveryVectorStore
- NFC plumbing (prepare_tools_and_workflows, last-call getters) is absent
- tool_discovery command serializes ToolCandidate lists without NFC
"""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from motet.core.tools.distributed_discovery import (
    ToolCandidate,
    ToolDiscoveryContext,
    ToolDiscoveryService,
)


NFC_MODULE = "motet.core.tools.native_function_calling"
REMOVED_SERVICE_METHODS = (
    "prepare_tools_and_workflows",
    "get_last_tool_calls",
    "get_last_llm_response",
    "get_last_tool_schemas",
    "get_last_usage",
    "get_last_reasoning_content",
    "get_last_reasoning_blocks",
    "_discover_with_native_calling",
)


def test_native_function_calling_module_not_importable() -> None:
    """ADR-0074 cleanup: native_function_calling must not be importable."""
    sys.modules.pop(NFC_MODULE, None)
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(NFC_MODULE)


def test_native_function_calling_service_not_importable() -> None:
    """NativeFunctionCallingService symbol must not resolve."""
    sys.modules.pop(NFC_MODULE, None)
    with pytest.raises((ImportError, ModuleNotFoundError)):
        from motet.core.tools.native_function_calling import (  # noqa: F401
            NativeFunctionCallingService,
        )


def test_tool_discovery_service_has_no_nfc_plumbing() -> None:
    """NFC-only entry points and last-call getters must be removed from the service."""
    for method_name in REMOVED_SERVICE_METHODS:
        assert not hasattr(ToolDiscoveryService, method_name), (
            f"ToolDiscoveryService still exposes NFC plumbing: {method_name}"
        )


def test_discover_tools_returns_tool_candidates_from_vector_store() -> None:
    """discover_tools maps FunctionDiscoveryVectorStore.search_functions → ToolCandidate."""
    store = MagicMock()
    store.search_functions.return_value = [
        {
            "type": "tool",
            "name": "web_search",
            "metadata": {"description": "Search the web"},
            "similarity_score": 0.91,
        },
        {
            "type": "tool",
            "name": "http_get",
            "metadata": {"description": "HTTP GET"},
            "similarity_score": 0.77,
        },
    ]
    registry = MagicMock()
    registry.get.return_value = None

    service = ToolDiscoveryService(
        tool_registry=registry,
        function_discovery_store=store,
    )

    candidates = service.discover_tools(
        content="search the web for weather",
        context_type=ToolDiscoveryContext.DIRECT_QUERY,
        max_tools=5,
    )

    assert isinstance(candidates, list)
    assert len(candidates) == 2
    assert all(isinstance(c, ToolCandidate) for c in candidates)
    assert candidates[0].name == "web_search"
    assert candidates[0].confidence == pytest.approx(0.91)
    assert isinstance(candidates[0].parameters, dict)
    assert isinstance(candidates[0].reasoning, str)
    assert candidates[0].discovery_method  # non-empty embedding/semantic label
    assert candidates[1].name == "http_get"

    store.search_functions.assert_called_once()
    call_kwargs = store.search_functions.call_args
    # query is positional or keyword
    args, kwargs = call_kwargs
    query = kwargs.get("query", args[0] if args else None)
    assert query == "search the web for weather"
    top_k = kwargs.get("top_k", args[1] if len(args) > 1 else None)
    assert top_k == 5


def test_discover_tools_returns_empty_when_store_missing() -> None:
    """Without a vector store, discover_tools yields no candidates (no NFC fallback)."""
    service = ToolDiscoveryService(
        tool_registry=MagicMock(),
        function_discovery_store=None,
    )
    candidates = service.discover_tools(
        content="anything",
        context_type=ToolDiscoveryContext.USER_PROMPT,
        max_tools=3,
    )
    assert candidates == []


def test_tool_discovery_command_serializes_candidates_without_nfc() -> None:
    """
    tool_discovery command path uses discover_tools + candidate serialization only.

    Mocks motet context and the embedding store; asserts NFC is never imported and
    the response shape matches the command contract (candidates list).
    """
    from motet.core.commands.command_data_classes import ToolDiscoveryData
    from motet.core.commands.builtin.tool import tool_discovery

    store = MagicMock()
    store.search_functions.return_value = [
        {
            "type": "tool",
            "name": "calculator",
            "metadata": {"description": "Math"},
            "similarity_score": 0.88,
        }
    ]
    registry = MagicMock()
    registry.list_items.return_value = {"calculator": object()}
    registry.get.return_value = None

    mock_motet = SimpleNamespace(
        stack=SimpleNamespace(tool_registry=registry),
        function_discovery_store=store,
    )

    data = ToolDiscoveryData(
        content="add numbers",
        context_type=ToolDiscoveryContext.USER_PROMPT,
        max_tools=3,
    )

    # Ensure NFC is not pulled in during the command path
    sys.modules.pop(NFC_MODULE, None)
    before_modules = set(sys.modules.keys())

    with patch(
        "motet.core.commands.builtin.tool.get_motet_context",
        return_value=mock_motet,
    ):
        cmd = tool_discovery(
            data=data,
            task_id="task-1",  # type: ignore[call-arg]
            conversation_id="conv-1",  # type: ignore[call-arg]
            tenant_id="tenant-1",  # type: ignore[call-arg]
        )
        envelope: Dict[str, Any] = cmd._do_execute({})

    assert NFC_MODULE not in sys.modules or NFC_MODULE in before_modules
    assert envelope.get("status") == "success", envelope
    result = envelope.get("data") or {}
    assert "candidates" in result
    assert result["candidates"]
    first = result["candidates"][0]
    assert first["name"] == "calculator"
    assert first["confidence"] == pytest.approx(0.88)
    assert "parameters" in first
    assert "discovery_method" in first
    assert result.get("registry_tool_count") == 1
    store.search_functions.assert_called()
