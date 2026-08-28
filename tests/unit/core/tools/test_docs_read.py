"""
Motet - Docs Read Tool Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Unit tests for the core.docs_read built-in: catalog list, allowlist
    enforcement, section windows, and registration.

Dependencies:
    - pytest
    - motet.core.tools.builtin.docs_read
    - motet.core.tools.registry.ToolRegistry

Usage:
    pytest tests/unit/core/tools/test_docs_read.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from motet.core.tools.builtin import docs_read as docs_read_module
from motet.core.tools.registry import ToolRegistry

_WORKFLOW_MD = """# Workflow System

#### YAML structure

required_inputs is a list of parameter names.
command_type plus command_data.

### Runtime-authored workflows (`user.*`)

Register via core.workflow_builder.
"""


@pytest.fixture
def docs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "00-landing-page.md").write_text("# Landing\nnope\n", encoding="utf-8")
    (tmp_path / "11-workflow-system.md").write_text(_WORKFLOW_MD, encoding="utf-8")
    (tmp_path / "17-building-workflows.md").write_text(
        "# Building Workflows\n\n## Runtime register via API or CLI\nbuilder\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MOTET_DEVELOPER_DOCS_DIR", str(tmp_path))
    return tmp_path


def test_docs_read_lists_catalog(docs_dir: Path) -> None:
    result = docs_read_module.run({})
    assert result["status"] == "success"
    ids = {item["id"] for item in result["result"]["items"]}
    assert ids == {"11-workflow-system", "17-building-workflows"}


def test_docs_read_section(docs_dir: Path) -> None:
    result = docs_read_module.run(
        {"doc_id": "11-workflow-system", "section": "YAML structure"}
    )
    assert result["status"] == "success"
    text = result["result"]["text"]
    assert "required_inputs is a list" in text
    assert "Runtime-authored" not in text


def test_docs_read_refuses_landing_page(docs_dir: Path) -> None:
    result = docs_read_module.run({"doc_id": "00-landing-page"})
    assert result["status"] == "error"
    assert "agent-facing catalog" in result["error"]


def test_docs_read_unknown_section_includes_headings(docs_dir: Path) -> None:
    result = docs_read_module.run(
        {"doc_id": "11-workflow-system", "section": "missing"}
    )
    assert result["status"] == "error"
    assert "YAML structure" in result["error"]
    assert "YAML structure" in result["meta"]["available_sections"]


def test_docs_read_invalid_params() -> None:
    result = docs_read_module.run({"offset_chars": -1})
    assert result["status"] == "error"
    assert "validation error" in result["error"]


def test_docs_read_registers() -> None:
    registry = ToolRegistry()
    docs_read_module.register(registry)
    tool = registry.get("core.docs_read")
    assert tool is not None
    assert "11-workflow-system" in tool.description
    assert "artifact" in tool.description.lower()
