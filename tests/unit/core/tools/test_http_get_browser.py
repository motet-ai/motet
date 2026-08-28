"""
Motet - Browser HTTP GET Extract Bounds Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    Unit tests for ``http_get_browser`` extract bounding. The tool used to
    slice ``main_content`` at 10k and drop the rest before observation
    offload could store it. These tests cover the helper that now keeps a
    real page and reports the pre-clip size.

Dependencies:
    - motet.core.tools.builtin.http_get_browser: extract bound helper

Usage:
    pytest tests/unit/core/tools/test_http_get_browser.py
"""

from __future__ import annotations

from motet.core.tools.builtin.http_get_browser import (
    MAIN_CONTENT_MAX_CHARS,
    _bound_main_content,
)


def test_bound_main_content_keeps_a_short_page_intact() -> None:
    text = "pricing table " * 20
    bounded = _bound_main_content(text)
    assert bounded["main_content"] == text
    assert bounded["content_length"] == len(text)
    assert bounded["truncated"] is False


def test_bound_main_content_keeps_more_than_the_old_10k_slice() -> None:
    text = "x" * 25_000
    bounded = _bound_main_content(text)
    assert bounded["truncated"] is False
    assert bounded["content_length"] == 25_000
    assert len(bounded["main_content"]) == 25_000


def test_bound_main_content_clips_at_the_extract_rail() -> None:
    text = "y" * (MAIN_CONTENT_MAX_CHARS + 4_000)
    bounded = _bound_main_content(text)
    assert bounded["truncated"] is True
    assert bounded["content_length"] == MAIN_CONTENT_MAX_CHARS + 4_000
    assert bounded["main_content"] == text[:MAIN_CONTENT_MAX_CHARS]


def test_bound_main_content_treats_empty_as_untruncated() -> None:
    bounded = _bound_main_content("")
    assert bounded == {
        "main_content": "",
        "content_length": 0,
        "truncated": False,
    }
