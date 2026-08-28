"""
Motet - Developer Docs Nav Taxonomy Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Unit tests for the exclusive onboarding nav taxonomy: section
    membership, display order, Other fallback, and drift against the
    real docs/developer_onboarding tree.

Dependencies:
    - pytest
    - motet.core.developer_docs

Usage:
    pytest tests/unit/core/developer_docs/test_taxonomy.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from motet.core.developer_docs.corpus import DocMeta, get_docs_dir, list_all_docs
from motet.core.developer_docs.taxonomy import (
    NAV_SECTIONS,
    OTHER_SECTION,
    group_docs,
    nav_doc_ids,
    nav_section_id,
)


def _meta(doc_id: str) -> DocMeta:
    return DocMeta(
        id=doc_id,
        filename=f"{doc_id}.md",
        title=doc_id,
        path=Path(f"{doc_id}.md"),
    )


def test_group_docs_uses_taxonomy_order_and_omits_empty_sections() -> None:
    groups = group_docs(
        [
            _meta("11-workflow-system"),
            _meta("00-landing-page"),
            _meta("17-building-workflows"),
            _meta("04-quick-start-guide"),
        ]
    )
    assert [group.section_id for group in groups] == ["home", "start", "build", "runtime"]
    assert [item.id for item in groups[0].items] == ["00-landing-page"]
    assert [item.id for item in groups[1].items] == ["04-quick-start-guide"]
    assert [item.id for item in groups[2].items] == ["17-building-workflows"]
    assert [item.id for item in groups[3].items] == ["11-workflow-system"]


def test_group_docs_puts_unmapped_files_in_other() -> None:
    groups = group_docs([_meta("99-brand-new-page"), _meta("04-quick-start-guide")])
    assert groups[0].section_id == "start"
    assert groups[-1].section_id == OTHER_SECTION.id
    assert [item.id for item in groups[-1].items] == ["99-brand-new-page"]


def test_nav_section_id_known_and_unknown() -> None:
    assert nav_section_id("00-landing-page") == "home"
    assert nav_section_id("15-building-your-first-command") == "start"
    assert nav_section_id("12-scheduled-commands") == "runtime"
    assert nav_section_id("36-chat-explorer") == "surfaces"
    assert nav_section_id("99-missing") == OTHER_SECTION.id


def test_http_list_returns_sections_in_taxonomy_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "00-landing-page.md").write_text("# Landing\n", encoding="utf-8")
    (tmp_path / "04-quick-start-guide.md").write_text("# Quick Start\n", encoding="utf-8")
    (tmp_path / "11-workflow-system.md").write_text("# Workflows\n", encoding="utf-8")
    monkeypatch.setenv("MOTET_DEVELOPER_DOCS_DIR", str(tmp_path))

    from motet.interfaces.api.v1.developer_docs import list_docs

    listed = list_docs()
    from motet._version import get_version

    assert listed.version == get_version()
    assert [item.id for item in listed.items] == [
        "00-landing-page",
        "04-quick-start-guide",
        "11-workflow-system",
    ]
    assert [section.id for section in listed.sections] == ["home", "start", "runtime"]
    assert listed.items[0].section == "home"
    assert listed.sections[0].title == "Home"


def test_taxonomy_covers_repo_corpus() -> None:
    """Every numbered onboarding file has exactly one nav section."""
    docs_dir = get_docs_dir()
    if docs_dir is None:
        pytest.skip("developer onboarding docs dir not present")
    listed = {item.id for item in list_all_docs()}
    mapped = nav_doc_ids()
    assert listed == mapped, (
        f"unmapped={sorted(listed - mapped)} missing_on_disk={sorted(mapped - listed)}"
    )
    assert [section.id for section in NAV_SECTIONS] == [
        "home",
        "start",
        "concepts",
        "build",
        "runtime",
        "state",
        "operate",
        "surfaces",
        "guides",
    ]
