"""
Unit tests for core.web_search backends and model-identity resolution.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from motet.core.tools.builtin import web_search as ws


class _FakeDDGS:
    def __init__(self, hits: List[Dict[str, Any]]):
        self._hits = hits

    def text(self, query: str, max_results: int = 10, backend: str = "auto"):
        return self._hits[:max_results]


def test_search_ddgs_normalizes_href_body() -> None:
    hits = [
        {
            "title": "History of Python - Wikipedia",
            "href": "https://en.wikipedia.org/wiki/History_of_Python",
            "body": "Python was conceived in the late 1980s.",
        }
    ]
    import sys

    sys.modules["ddgs"] = MagicMock(DDGS=lambda: _FakeDDGS(hits))
    result = ws._search_ddgs(
        "history of Python programming language development",
        max_results=3,
    )
    assert result is not None
    assert result["web_search_path"] == "ddgs"
    assert result["total_results"] == 1
    assert result["results"][0]["url"].startswith("https://en.wikipedia.org/")


def test_run_web_search_uses_ddgs_when_llm_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_WEB_SEARCH_BACKEND", "ddgs")
    monkeypatch.setattr(ws, "_get_motet_context_optional", lambda: None)

    hits = [
        {
            "title": "Python",
            "href": "https://www.python.org/",
            "body": "Official site",
        }
    ]

    class _DDGS:
        def text(self, query: str, max_results: int = 10, backend: str = "auto"):
            return hits[:max_results]

    import sys

    sys.modules["ddgs"] = MagicMock(DDGS=_DDGS)
    out = ws.run_web_search({"query": "Python programming language", "max_results": 3})
    assert out["status"] == "success"
    assert out["web_search_path"] == "ddgs"
    assert out["total_results"] >= 1
    assert out["results"][0]["url"]


def test_resolve_model_identity_prefers_params() -> None:
    motet = SimpleNamespace(metadata={"model_provider": "anthropic", "model_name": "claude"})
    provider, model = ws._resolve_model_identity(
        motet,
        {"provider": "openai", "model_name": "gpt-4o-mini"},
    )
    assert provider == "openai"
    assert model == "gpt-4o-mini"


@pytest.mark.parametrize(
    ("provider", "model_name"),
    [
        ("xai", "grok-4.5"),
        ("xai", "grok-4.6"),
        ("deepseek", "deepseek-v4-flash"),
        ("deepseek", "deepseek-v4-pro"),
        ("meta", "muse-spark-1.1"),
        ("meta", "muse-spark-1.2"),
    ],
)
def test_llm_web_search_usable_for_responses_hosts(provider: str, model_name: str) -> None:
    motet = SimpleNamespace(metadata={"model_provider": provider, "model_name": model_name})
    assert ws._current_model_supports_web_search(motet, provider, model_name) is True
    assert ws._llm_web_search_usable(motet, provider, model_name) is True


def test_try_llm_web_search_keeps_llm_path_when_citations_have_urls() -> None:
    motet = MagicMock()
    motet.metadata = {"model_profile_name": "default"}
    motet.tenant_id = "motet-global"
    motet.principal_id = "user"
    motet.motet_id = None
    motet.task_id = "task-1"
    motet.do.return_value = {
        "content": "Boston Common is the oldest public park.",
        "citations": [
            {
                "url": "https://www.boston.gov",
                "title": "Boston",
                "snippets": ["America’s oldest public park."],
            }
        ],
    }
    out = ws._try_llm_web_search(
        "things to do in Boston",
        5,
        motet,
        provider="xai",
        model_name="grok-4.5",
    )
    assert out is not None
    assert out["web_search_path"] == "llm"
    assert out["results"][0]["url"] == "https://www.boston.gov"
    assert out["results"][0]["content"] == "America’s oldest public park."


def test_llm_path_falls_through_without_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM answer-only responses should not short-circuit ddgs (need URLs)."""
    motet = MagicMock()
    motet.metadata = {"model_provider": "openai", "model_name": "gpt-4o"}
    motet.do.return_value = {"content": "Some answer text", "citations": []}

    monkeypatch.setattr(ws, "_llm_web_search_usable", lambda *a, **k: True)
    monkeypatch.setattr(
        ws,
        "_try_llm_web_search",
        lambda *a, **k: None,  # simulates no-URL fallthrough
    )

    hits = [{"title": "T", "href": "https://example.com/a", "body": "snippet"}]

    class _DDGS:
        def text(self, query: str, max_results: int = 10, backend: str = "auto"):
            return hits

    import sys

    sys.modules["ddgs"] = MagicMock(DDGS=_DDGS)
    monkeypatch.setenv("MOTET_WEB_SEARCH_BACKEND", "auto")
    monkeypatch.setattr(ws, "_get_motet_context_optional", lambda: motet)
    monkeypatch.setattr(ws, "_current_model_web_search_passthrough", lambda *a, **k: False)

    out = ws.run_web_search({"query": "long research query about widgets 2026", "max_results": 2})
    assert out["web_search_path"] == "ddgs"
    assert out["results"][0]["url"] == "https://example.com/a"


def test_stamp_model_metadata_from_workflow_context() -> None:
    from motet.core.workflow.executor import WorkflowExecutor

    class _Ctx:
        def __init__(self) -> None:
            self.metadata: Dict[str, Any] = {}

    class _Cmd:
        def __init__(self) -> None:
            self.distributed_context = _Ctx()

    motet = SimpleNamespace(_command=_Cmd(), _metadata_fallback={})
    workflow = SimpleNamespace(
        workflow_id="deep-research.research",
        context={"provider": "openai", "model_name": "gpt-4o-mini", "topic": "Python"},
    )
    WorkflowExecutor()._stamp_model_metadata_from_context(workflow, motet)
    meta = motet._command.distributed_context.metadata
    assert meta["model_provider"] == "openai"
    assert meta["model_name"] == "gpt-4o-mini"


def test_stamp_does_not_overwrite_existing_metadata() -> None:
    from motet.core.workflow.executor import WorkflowExecutor

    class _Ctx:
        def __init__(self) -> None:
            self.metadata = {"model_provider": "anthropic", "model_name": "claude-sonnet"}

    class _Cmd:
        def __init__(self) -> None:
            self.distributed_context = _Ctx()

    motet = SimpleNamespace(_command=_Cmd())
    workflow = SimpleNamespace(
        workflow_id="wf",
        context={"provider": "openai", "model_name": "gpt-4o-mini"},
    )
    WorkflowExecutor()._stamp_model_metadata_from_context(workflow, motet)
    meta = motet._command.distributed_context.metadata
    assert meta["model_provider"] == "anthropic"
    assert meta["model_name"] == "claude-sonnet"


def test_motet_metadata_property_returns_live_empty_dict() -> None:
    """Empty metadata must be the live command dict, not a throwaway {}."""
    from motet.core.commands.decorator import MotetContext

    class _Ctx:
        def __init__(self) -> None:
            self.metadata: Dict[str, Any] = {}

    class _Cmd:
        def __init__(self) -> None:
            self.distributed_context = _Ctx()
            self.command_id = "c1"

    cmd = _Cmd()
    motet = MotetContext(command_instance=cmd)
    meta = motet.metadata
    meta["model_provider"] = "openai"
    assert cmd.distributed_context.metadata["model_provider"] == "openai"
