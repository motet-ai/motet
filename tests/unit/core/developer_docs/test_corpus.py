"""
Motet - Developer Docs Corpus Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Unit tests for the shared developer-docs corpus: path safety, HTTP list/read
    of the full numbered tree, and agent-facing allowlist + section windows.

Dependencies:
    - pytest
    - motet.core.developer_docs

Usage:
    pytest tests/unit/core/developer_docs/test_corpus.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from motet.core.developer_docs.allowlist import AGENT_FACING_DOCS, agent_facing_ids
from motet.core.developer_docs.corpus import (
    DocNotAgentFacing,
    DocNotFound,
    InvalidDocId,
    SectionNotFound,
    list_agent_facing,
    list_all_docs,
    read_agent_facing,
    read_doc_text,
)

_WORKFLOW_MD = """# Workflow System

Intro paragraph.

## Workflow Model

Model text.

#### YAML structure

required_inputs is a list of parameter names.
Put schemas in input_parameters.
steps is a map keyed by step_id.
Each step needs command_type and command_data.
Placeholders: {input} and {{step_id.result}}.

#### Sequential foreach (loop over a list)

foreach docs.

### Runtime-authored workflows (`user.*`)

Register via core.workflow_builder.
"""

_BUILDING_MD = """# Building Workflows

Tutorial body.

## Runtime register via API or CLI

Agents can drive the same modes through the core.workflow_builder tool.
"""

_LANDING_MD = """# Landing page

Welcome marketing copy.
"""


def _write_corpus(root: Path) -> None:
    (root / "00-landing-page.md").write_text(_LANDING_MD, encoding="utf-8")
    (root / "11-workflow-system.md").write_text(_WORKFLOW_MD, encoding="utf-8")
    (root / "17-building-workflows.md").write_text(_BUILDING_MD, encoding="utf-8")
    (root / "README.md").write_text("# ignore me\n", encoding="utf-8")


@pytest.fixture
def docs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _write_corpus(tmp_path)
    monkeypatch.setenv("MOTET_DEVELOPER_DOCS_DIR", str(tmp_path))
    return tmp_path


def test_list_all_docs_skips_non_numbered_and_uses_heading_title(docs_dir: Path) -> None:
    items = {item.id: item for item in list_all_docs()}
    assert set(items) == {"00-landing-page", "11-workflow-system", "17-building-workflows"}
    assert items["11-workflow-system"].title == "Workflow System"
    assert items["00-landing-page"].filename == "00-landing-page.md"


def test_read_doc_text_and_invalid_id(docs_dir: Path) -> None:
    text = read_doc_text("11-workflow-system")
    assert "YAML structure" in text
    with pytest.raises(InvalidDocId):
        read_doc_text("../etc/passwd")
    with pytest.raises(InvalidDocId):
        read_doc_text("not a doc")
    with pytest.raises(DocNotFound):
        read_doc_text("99-does-not-exist")


def test_agent_facing_list_marks_availability(docs_dir: Path) -> None:
    items = {row["id"]: row for row in list_agent_facing()}
    assert set(items) == agent_facing_ids()
    assert items["11-workflow-system"]["available"] is True
    assert "00-landing-page" not in items


def test_read_agent_facing_lists_when_id_omitted(docs_dir: Path) -> None:
    payload = read_agent_facing()
    assert payload["mode"] == "list"
    assert {row["id"] for row in payload["items"]} == agent_facing_ids()


def test_read_agent_facing_refuses_non_allowlisted(docs_dir: Path) -> None:
    with pytest.raises(DocNotAgentFacing):
        read_agent_facing(doc_id="00-landing-page")


def test_read_agent_facing_section_window(docs_dir: Path) -> None:
    payload = read_agent_facing(doc_id="11-workflow-system", section="YAML structure")
    assert payload["mode"] == "read"
    assert payload["section"] == "YAML structure"
    assert "required_inputs is a list" in payload["text"]
    assert "Sequential foreach" not in payload["text"]
    assert "Runtime-authored" not in payload["text"]


def test_read_agent_facing_section_slug_and_runtime_heading(docs_dir: Path) -> None:
    yaml = read_agent_facing(doc_id="11-workflow-system", section="yaml-structure")
    assert "command_type and command_data" in yaml["text"]
    runtime = read_agent_facing(
        doc_id="11-workflow-system",
        section="Runtime-authored workflows (user.*)",
    )
    assert "core.workflow_builder" in runtime["text"]


def test_read_agent_facing_unknown_section_lists_headings(docs_dir: Path) -> None:
    with pytest.raises(SectionNotFound) as caught:
        read_agent_facing(doc_id="11-workflow-system", section="no-such-heading")
    assert "YAML structure" in caught.value.available


def test_http_list_and_get_use_shared_corpus(docs_dir: Path) -> None:
    from fastapi import HTTPException

    from motet.interfaces.api.v1.developer_docs import get_doc, list_docs

    listed = list_docs()
    from motet._version import get_version

    assert listed.version == get_version()
    ids = {item.id for item in listed.items}
    assert "00-landing-page" in ids
    assert "11-workflow-system" in ids
    assert listed.sections
    assert all(item.section for item in listed.items)
    response = get_doc("11-workflow-system")
    assert "YAML structure" in response.body.decode("utf-8")
    try:
        get_doc("../etc/passwd")
        raise AssertionError("expected HTTP 400 for invalid id")
    except HTTPException as exc:
        assert exc.status_code == 400


def test_read_agent_facing_char_window(docs_dir: Path) -> None:
    first = read_agent_facing(doc_id="11-workflow-system", max_chars=40)
    assert first["truncated"] is True
    assert first["returned_chars"] == 40
    assert first["next_offset_chars"] == 40
    second = read_agent_facing(
        doc_id="11-workflow-system",
        offset_chars=first["next_offset_chars"],
        max_chars=40,
    )
    assert second["offset_chars"] == 40
    combined = first["text"] + second["text"]
    full = read_doc_text("11-workflow-system")
    assert combined == full[:80]


def test_allowlisted_ids_exist_in_repo_corpus() -> None:
    """Guard against allowlist drift when the real onboarding tree is present."""
    from motet.core.developer_docs.corpus import get_docs_dir

    docs_dir = get_docs_dir()
    if docs_dir is None:
        pytest.skip("developer onboarding docs dir not present")
    for entry in AGENT_FACING_DOCS:
        path = docs_dir / f"{entry.id}.md"
        assert path.is_file(), f"allowlisted {entry.id} missing at {path}"
        text = path.read_text(encoding="utf-8")
        for heading in entry.suggested_sections:
            assert heading.replace("`", "") in text.replace("`", ""), (
                f"suggested section {heading!r} not found in {entry.id}"
            )
        if entry.id == "11-workflow-system":
            assert "required_inputs" in text
            assert "command_data" in text
            assert "YAML structure" in text
