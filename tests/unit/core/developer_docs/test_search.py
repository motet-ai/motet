"""
Motet - Developer Docs Lexical Search Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Unit tests for title/body lexical search over the onboarding corpus
    and the HTTP ``GET /api/v1/developer-docs/search`` route.

Dependencies:
    - pytest
    - fastapi TestClient
    - motet.core.developer_docs.search
    - motet.interfaces.api.v1.developer_docs

Usage:
    pytest tests/unit/core/developer_docs/test_search.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from motet.core.developer_docs.search import search_docs


@pytest.fixture
def docs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "04-quick-start-guide.md").write_text(
        "# Quick Start Guide\n\nInstall Motet and run your first command.\n",
        encoding="utf-8",
    )
    (tmp_path / "08a-worker-targeting-guide.md").write_text(
        "# Worker Targeting Guide\n\n"
        "## Targeting a worker\n\n"
        "Route a command to a specific worker pool.\n",
        encoding="utf-8",
    )
    (tmp_path / "11-workflow-system.md").write_text(
        "# Workflow System\n\n"
        "YAML steps can call another command. Targeting is mentioned only here.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MOTET_DEVELOPER_DOCS_DIR", str(tmp_path))
    return tmp_path


def test_short_query_returns_no_hits(docs_dir: Path) -> None:
    assert search_docs("") == []
    assert search_docs("w") == []
    assert search_docs("   ") == []


def test_and_tokens_require_every_token(docs_dir: Path) -> None:
    hits = search_docs("worker targeting")
    assert [hit.id for hit in hits] == ["08a-worker-targeting-guide"]


def test_title_match_ranks_above_body_only(docs_dir: Path) -> None:
    hits = search_docs("targeting")
    assert [hit.id for hit in hits] == [
        "08a-worker-targeting-guide",
        "11-workflow-system",
    ]
    assert hits[0].score > hits[1].score
    assert hits[0].heading == "Targeting a worker"
    assert "worker" in hits[0].snippet.lower()


def test_search_endpoint_is_not_captured_as_doc_id(docs_dir: Path) -> None:
    from motet.interfaces.api.v1.developer_docs import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get(
        "/api/v1/developer-docs/search",
        params={"q": "quick start"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["query"] == "quick start"
    assert [item["id"] for item in payload["items"]] == ["04-quick-start-guide"]
    assert payload["items"][0]["section"] == "start"
    assert payload["items"][0]["section_title"] == "Start"
