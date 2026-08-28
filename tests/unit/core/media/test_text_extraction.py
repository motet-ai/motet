"""
Motet - Text Extraction Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-16

Description:
    Unit tests for document text extraction fallbacks used by artifact derivation
    and ADR-0110 artifact preparation.

Dependencies:
    - io and zipfile for constructing lightweight OOXML fixtures
    - pytest monkeypatch for exercising fallback paths

Usage:
    pytest tests/unit/core/media/test_text_extraction.py

Notes:
    - PPTX fallback tests use minimal ZIP/XML payloads rather than requiring
      binary fixture files.
"""

from __future__ import annotations

import io
import zipfile


def _minimal_pptx() -> bytes:
    buffer = io.BytesIO()
    slide_xml = """
    <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
           xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
      <p:cSld>
        <p:spTree>
          <p:sp><p:txBody><a:p><a:r><a:t>Slide Title</a:t></a:r></a:p></p:txBody></p:sp>
          <p:sp><p:txBody><a:p><a:r><a:t>Important body text</a:t></a:r></a:p></p:txBody></p:sp>
        </p:spTree>
      </p:cSld>
    </p:sld>
    """
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", slide_xml)
    return buffer.getvalue()


def test_pptx_ooxml_fallback_extracts_slide_text(monkeypatch) -> None:
    from motet.core.media import text_extraction

    class _EmptyPresentation:
        slides: list = []

    monkeypatch.setattr(text_extraction, "Presentation", lambda _stream: _EmptyPresentation(), raising=False)

    extracted = text_extraction.extract_text_from_bytes(
        _minimal_pptx(),
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )

    assert "Slide Title" in extracted
    assert "Important body text" in extracted
    assert "--- Page 1 (Slide) ---" in extracted
