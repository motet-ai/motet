"""
Motet - Model Command Citation Forwarding Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Unit tests for adapter citation serialization on model_inference results.
    Grok and DeepSeek native web_search depend on these dicts reaching
    core.web_search; an empty or dropped list falls through to ddgs.

Usage:
    pytest tests/unit/core/commands/test_model_citations.py
"""

from __future__ import annotations

from types import SimpleNamespace

from motet.core.commands.builtin.model import _citations_payload
from motet.core.types import Citation


def test_citations_payload_forwards_non_openai_urls() -> None:
    resp = SimpleNamespace(
        citations=[
            Citation(source_type="web", url="https://www.boston.gov", title="Boston"),
        ]
    )
    out = _citations_payload(resp)
    assert out is not None
    assert out[0]["url"] == "https://www.boston.gov"


def test_citations_payload_empty_is_none() -> None:
    assert _citations_payload(SimpleNamespace(citations=None)) is None
    assert _citations_payload(SimpleNamespace(citations=[])) is None
    assert _citations_payload(SimpleNamespace()) is None
