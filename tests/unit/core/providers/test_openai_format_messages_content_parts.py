"""
Motet - OpenAI Provider content_parts Formatting Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Unit tests ensuring Chat Completions formatting does not drop text-only
    `content_parts` when multimodal rendering is disabled. This is critical for
    ADR-0062 derived-text injection from documents (PDF/DOCX/etc.) where the model
    may not be a vision model.

Dependencies:
    - pytest
    - motet.core.models.adapters.providers.openai_chat_completions._format_messages_for_openai

Usage:
    pytest tests/unit/core/providers/test_openai_format_messages_content_parts.py
"""

from __future__ import annotations


def test_openai_format_messages_flattens_text_parts_when_multimodal_disabled():
    from motet.core.types import Message, RequestContext, TextPart
    from motet.core.models.adapters.providers.openai_chat_completions import _format_messages_for_openai

    messages = [
        Message(
            role="user",
            content="fallback",
            content_parts=[
                TextPart(text="what does this say?"),
                TextPart(text="EXTRACTED PDF TEXT"),
            ],
        )
    ]

    formatted = _format_messages_for_openai(
        messages,
        model_name="gpt-4.1-mini",
        request_context=RequestContext(enable_multimodal=False),
    )

    assert formatted[0]["role"] == "user"
    assert isinstance(formatted[0]["content"], str)
    assert "EXTRACTED PDF TEXT" in formatted[0]["content"]


def test_openai_format_messages_drops_images_but_keeps_text_when_multimodal_disabled():
    from motet.core.types import MediaPart, Message, RequestContext, TextPart
    from motet.core.models.adapters.providers.openai_chat_completions import _format_messages_for_openai

    messages = [
        Message(
            role="user",
            content="fallback",
            content_parts=[
                TextPart(text="question"),
                MediaPart(media_type="image", artifact_id="img-1", mime_type="image/png", detail="auto"),
                TextPart(text="EXTRACTED DOC TEXT"),
            ],
        )
    ]

    formatted = _format_messages_for_openai(
        messages,
        model_name="gpt-4.1-mini",
        request_context=RequestContext(enable_multimodal=False),
    )

    assert isinstance(formatted[0]["content"], str)
    assert "EXTRACTED DOC TEXT" in formatted[0]["content"]
    # No image array rendering should be produced in this mode
    assert "image_url" not in formatted[0]["content"]


