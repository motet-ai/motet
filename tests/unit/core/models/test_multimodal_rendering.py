"""
Motet - Multimodal Rendering Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Unit tests for provider-agnostic multimodal rendering (ADR-0062/ADR-0064).
    Validates canonical content parts, MediaPart materialization, and limits
    enforced by the CanonicalMultimodalRenderer.

Dependencies:
    - pytest: test framework
    - motet.core.types: Message, TextPart, MediaPart
    - motet.core.models.rendering: CanonicalMultimodalRenderer, RenderingContext

Usage:
    pytest tests/unit/core/models/test_multimodal_rendering.py

Notes:
    - Renderer outputs canonical Message objects, not provider wire formats.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, Optional

import pytest

from motet.core.models.rendering.base import RenderingContext
from motet.core.models.rendering.canonical import CanonicalMultimodalRenderer
from motet.core.types import MediaPart, Message, TextPart


class _FakeArtifactStore:
    """Minimal artifact store stub for renderer tests (get() only)."""

    def __init__(self, payloads: Dict[str, Any]):
        self._payloads = payloads

    def get(
        self,
        artifact_id: str,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> Optional[Any]:
        # Fail-closed simulation: require tenant + principal to be present.
        if not tenant_id or not principal_id:
            return None
        return self._payloads.get(artifact_id)


def test_message_content_parts_serializes() -> None:
    msg = Message(
        role="user",
        content="fallback text",
        content_parts=[TextPart(text="hello")],
    )
    data = msg.model_dump()
    assert data["content"] == "fallback text"
    assert "content_parts" in data
    assert data["content_parts"][0]["type"] == "text"
    assert data["content_parts"][0]["text"] == "hello"


def test_canonical_renderer_text_parts() -> None:
    renderer = CanonicalMultimodalRenderer()
    msg = Message(role="user", content="ignored", content_parts=[TextPart(text="hi")])

    ctx = RenderingContext(
        provider="openai",
        model_name="gpt-4o-mini",
        tenant_id="t1",
        principal_id="p1",
        motet_id="f1",
        artifact_store=_FakeArtifactStore({}),
    )

    rendered = renderer.render([msg], context=ctx)
    assert rendered[0].role == "user"
    assert isinstance(rendered[0].content_parts, list)
    assert rendered[0].content_parts[0].type == "text"
    assert rendered[0].content_parts[0].text == "hi"


def test_canonical_renderer_image_part_materializes_base64() -> None:
    renderer = CanonicalMultimodalRenderer()
    image_id = "img-1"
    msg = Message(
        role="user",
        content="fallback",
        content_parts=[
            TextPart(text="read this"),
            MediaPart(media_type="image", artifact_id=image_id, mime_type="image/png", detail="low"),
        ],
    )

    store = _FakeArtifactStore({image_id: b"\x89PNG\r\n\x1a\nfake"})
    ctx = RenderingContext(
        provider="openai",
        model_name="gpt-4o-mini",
        tenant_id="t1",
        principal_id="p1",
        motet_id="f1",
        artifact_store=store,
        max_images=4,
        max_image_bytes=1024 * 1024,
    )

    rendered = renderer.render([msg], context=ctx)
    parts = rendered[0].content_parts or []
    assert parts[0].type == "text"
    assert parts[0].text == "read this"
    assert parts[1].type == "media"
    assert parts[1].media_type == "image"
    assert parts[1].detail == "low"
    assert isinstance(parts[1].base64_data, str)
    assert parts[1].base64_data == base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")


def test_canonical_renderer_enforces_max_images() -> None:
    renderer = CanonicalMultimodalRenderer()
    store = _FakeArtifactStore({"img": b"x"})
    ctx = RenderingContext(
        provider="openai",
        model_name="gpt-4o-mini",
        tenant_id="t1",
        principal_id="p1",
        motet_id="f1",
        artifact_store=store,
        max_images=0,
    )

    msg = Message(
        role="user",
        content="fallback",
        content_parts=[MediaPart(media_type="image", artifact_id="img", mime_type="image/png", detail="auto")],
    )

    with pytest.raises(ValueError, match="max_images exceeded"):
        renderer.render([msg], context=ctx)


