"""
Motet - prepare_context Attachment Injection Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Unit tests for ADR-0062 attachment-derived text injection in `prepare_context`.

    Specifically validates that derived text injection works even when:
    - there is no conversation history (first turn / missing conversation_id)
    - the attachment does not include `derived_artifact_ids`

Dependencies:
    - pytest
    - unittest.mock
    - motet.core.orchestration.turn.phases.prepare_context

Usage:
    pytest tests/unit/core/orchestration/test_prepare_context_attachments.py
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest


class FakeStore:
    def __init__(self, derived_id: str, text: str):
        self._derived_id = derived_id
        self._text = text

    def list(self, *args, **kwargs):
        class _Meta:
            def __init__(self, _id: str):
                self.id = _id
                self.source_artifact_id = kwargs.get("source_artifact_id")

        return [_Meta(self._derived_id)]

    def find_derived(self, source_artifact_id: str = "", kind: Any = None):
        class _Meta:
            def __init__(self, _id: str):
                self.id = _id

        return _Meta(self._derived_id)

    def get(self, artifact_id: str, *args, **kwargs):
        if artifact_id == self._derived_id:
            return self._text
        return None


class FakeImageStore:
    def __init__(self, *, derived_id: str | None = None, derived_content_type: str | None = None):
        self._derived_id = derived_id
        self._derived_content_type = derived_content_type

    def find_derived(self, source_artifact_id: str = "", kind: Any = None):
        if not self._derived_id:
            return None

        class _Meta:
            def __init__(self, _id: str, content_type: str | None):
                self.id = _id
                self.content_type = content_type

        return _Meta(self._derived_id, self._derived_content_type)


class FakeNoDerivedStore:
    def list(self, *args, **kwargs):
        return []

    def find_derived(self, source_artifact_id: str = "", kind: Any = None):
        return None

    def get(self, artifact_id: str, *args, **kwargs):
        return None


@dataclass
class FakeMotet:
    tenant_id: str = "tenant-a"
    principal_id: str = "principal-1"
    motet_id: str = "default"
    task_id: str = "task-123"
    command_id: str = "cmd-123"
    conversation_id: str = ""  # No conversation history
    memory: Any = None
    stack: Any = None
    artifact_store: Any = None

    def log_fields(self, **extra) -> Dict[str, Any]:
        return {"tenant_id": self.tenant_id, "task_id": self.task_id, **extra}


def test_prepare_context_injects_derived_text_without_conversation_history():
    from motet.core.types import Message
    from motet.core.commands.command_data_classes import PrepareContextData
    from motet.core.orchestration.turn.phases import prepare_context

    source_artifact_id = "source-abc"
    derived_id = "derived-xyz"
    extracted = "Hello from PDF"

    fake_store = FakeStore(derived_id=derived_id, text=extracted)
    fake_motet = FakeMotet(artifact_store=fake_store)

    data = PrepareContextData(
        messages=[
            Message(
                role="user",
                content="what does this say?",
                attachments=[
                    {
                        "artifact_id": source_artifact_id,
                        "filename": "doc.pdf",
                        "content_type": "application/pdf",
                        "bytes": 10,
                    }
                ],
            )
        ],
        include_memory_recall=False,
    )

    with patch("motet.core.orchestration.turn.phases.get_motet_context", return_value=fake_motet), patch(
        "motet.core.artifacts.get_artifact_store", return_value=fake_store
    ):
        out = prepare_context.__wrapped__(data)

    prepared = out["prepared_messages"]
    assert len(prepared) == 1
    msg = prepared[0]
    assert "content_parts" in msg
    # Should contain extracted text payload somewhere in text parts
    combined_chunks: List[str] = []
    for p in msg["content_parts"]:
        # Parts may be Pydantic models (TextPart/MediaPart) or dict-shaped parts.
        p_type = getattr(p, "type", None) if not isinstance(p, dict) else p.get("type")
        if p_type != "text":
            continue
        p_text = getattr(p, "text", None) if not isinstance(p, dict) else p.get("text")
        if isinstance(p_text, str):
            combined_chunks.append(p_text)
    combined = "\n".join(combined_chunks)
    assert "Hello from PDF" in combined
    assert f"artifact_id='{derived_id}'" in combined
    assert f"source_artifact_id='{source_artifact_id}'" in combined
    assert "Use source_artifact_id for tools that need the original binary file" in combined


@pytest.mark.parametrize(
    ("filename", "expected_content_type"),
    [
        ("sample.AVIF", "image/avif"),
        ("sample.HEIC", "image/heic"),
        ("sample.heif", "image/heif"),
    ],
)
def test_prepare_context_infers_modern_image_content_types_from_filename(
    filename: str,
    expected_content_type: str,
):
    from motet.core.types import Message
    from motet.core.commands.command_data_classes import PrepareContextData
    from motet.core.orchestration.turn.phases import prepare_context

    fake_store = FakeImageStore()
    fake_motet = FakeMotet(artifact_store=fake_store)

    data = PrepareContextData(
        messages=[
            Message(
                role="user",
                content="analyze this image",
                attachments=[
                    {
                        "artifact_id": "image-source-1",
                        "filename": filename,
                        "content_type": "application/octet-stream",
                        "bytes": 10,
                    }
                ],
            )
        ],
        include_memory_recall=False,
    )

    with patch("motet.core.orchestration.turn.phases.get_motet_context", return_value=fake_motet):
        out = prepare_context.__wrapped__(data)

    media_parts = [
        part
        for part in out["prepared_messages"][0]["content_parts"]
        if (getattr(part, "type", None) if not isinstance(part, dict) else part.get("type")) == "media"
    ]
    assert len(media_parts) == 1
    media_part = media_parts[0]
    assert getattr(media_part, "artifact_id", None) == "image-source-1"
    assert getattr(media_part, "mime_type", None) == expected_content_type


def test_prepare_context_uses_derived_image_content_type_for_media_part():
    from motet.core.types import Message
    from motet.core.commands.command_data_classes import PrepareContextData
    from motet.core.orchestration.turn.phases import prepare_context

    fake_store = FakeImageStore(
        derived_id="image-derived-base",
        derived_content_type="image/jpeg",
    )
    fake_motet = FakeMotet(artifact_store=fake_store)

    data = PrepareContextData(
        messages=[
            Message(
                role="user",
                content="analyze this image",
                attachments=[
                    {
                        "artifact_id": "image-source-avif",
                        "filename": "sample.avif",
                        "content_type": "image/avif",
                        "bytes": 10,
                    }
                ],
            )
        ],
        include_memory_recall=False,
    )

    with patch("motet.core.orchestration.turn.phases.get_motet_context", return_value=fake_motet):
        out = prepare_context.__wrapped__(data)

    media_parts = [
        part
        for part in out["prepared_messages"][0]["content_parts"]
        if (getattr(part, "type", None) if not isinstance(part, dict) else part.get("type")) == "media"
    ]
    assert len(media_parts) == 1
    media_part = media_parts[0]
    assert getattr(media_part, "artifact_id", None) == "image-derived-base"
    assert getattr(media_part, "mime_type", None) == "image/jpeg"


def test_prepare_context_auto_includes_prior_attachment_metadata_when_text_not_ready():
    from motet.core.types import Message
    from motet.core.commands.command_data_classes import PrepareContextData
    from motet.core.orchestration.turn.phases import prepare_context

    source_artifact_id = "pptx-source-1"
    fake_store = FakeNoDerivedStore()
    fake_motet = FakeMotet(conversation_id="conv-123", artifact_store=fake_store)

    data = PrepareContextData(
        messages=[
            Message(
                role="user",
                content="can you see this?",
                attachments=[
                    {
                        "artifact_id": source_artifact_id,
                        "filename": "DISCORD_ProposersDay_releasable.pptx",
                        "content_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        "bytes": 4546986,
                    }
                ],
            ),
            Message(role="assistant", content="Yes - I can see your message."),
            Message(role="user", content="do you see the uploaded artifact?"),
        ],
        include_memory_recall=False,
    )

    with patch("motet.core.orchestration.turn.phases.get_motet_context", return_value=fake_motet):
        out = prepare_context.__wrapped__(data)

    current_msg = out["prepared_messages"][-1]
    combined_chunks: List[str] = []
    for part in current_msg["content_parts"]:
        part_type = getattr(part, "type", None) if not isinstance(part, dict) else part.get("type")
        if part_type != "text":
            continue
        text = getattr(part, "text", None) if not isinstance(part, dict) else part.get("text")
        if isinstance(text, str):
            combined_chunks.append(text)

    combined = "\n".join(combined_chunks)
    assert f"artifact_id='{source_artifact_id}'" in combined
    assert "DISCORD_ProposersDay_releasable.pptx" in combined
    assert "content_status='pending_text_extraction'" in combined
    assert "do not claim to have read its contents" in combined


def test_prepare_context_injects_derived_video_transcript_without_conversation_history():
    from motet.core.types import Message
    from motet.core.commands.command_data_classes import PrepareContextData
    from motet.core.orchestration.turn.phases import prepare_context

    source_artifact_id = "video-source-1"
    transcript_id = "transcript-derived-1"
    transcript_text = "[0-1200] Hello from the video"

    fake_store = FakeStore(derived_id=transcript_id, text=transcript_text)
    fake_motet = FakeMotet(artifact_store=fake_store)

    data = PrepareContextData(
        messages=[
            Message(
                role="user",
                content="what does this video say?",
                attachments=[
                    {
                        "artifact_id": source_artifact_id,
                        "filename": "IMG_2178.MOV",
                        "content_type": "video/quicktime",
                        "bytes": 1000,
                    }
                ],
            )
        ],
        include_memory_recall=False,
    )

    with patch("motet.core.orchestration.turn.phases.get_motet_context", return_value=fake_motet), patch(
        "motet.core.artifacts.get_artifact_store", return_value=fake_store
    ):
        out = prepare_context.__wrapped__(data)

    prepared = out["prepared_messages"]
    assert len(prepared) == 1
    combined_chunks: List[str] = []
    for part in prepared[0]["content_parts"]:
        part_type = getattr(part, "type", None) if not isinstance(part, dict) else part.get("type")
        if part_type != "text":
            continue
        part_text = getattr(part, "text", None) if not isinstance(part, dict) else part.get("text")
        if isinstance(part_text, str):
            combined_chunks.append(part_text)
    combined = "\n".join(combined_chunks)
    assert "Hello from the video" in combined
    assert f"artifact_id='{transcript_id}'" in combined
    assert f"source_artifact_id='{source_artifact_id}'" in combined
    assert "artifact_id is the video transcript" in combined


def test_prepare_context_injects_pending_video_metadata_when_transcript_not_ready():
    from motet.core.types import Message
    from motet.core.commands.command_data_classes import PrepareContextData
    from motet.core.orchestration.turn.phases import prepare_context

    source_artifact_id = "video-source-2"
    fake_store = FakeNoDerivedStore()
    fake_motet = FakeMotet(artifact_store=fake_store)

    data = PrepareContextData(
        messages=[
            Message(
                role="user",
                content="is there a transcript?",
                attachments=[
                    {
                        "artifact_id": source_artifact_id,
                        "filename": "clip.mp4",
                        "content_type": "video/mp4",
                        "bytes": 500,
                    }
                ],
            )
        ],
        include_memory_recall=False,
    )

    with patch("motet.core.orchestration.turn.phases.get_motet_context", return_value=fake_motet):
        out = prepare_context.__wrapped__(data)

    combined_chunks: List[str] = []
    for part in out["prepared_messages"][0]["content_parts"]:
        part_type = getattr(part, "type", None) if not isinstance(part, dict) else part.get("type")
        if part_type != "text":
            continue
        part_text = getattr(part, "text", None) if not isinstance(part, dict) else part.get("text")
        if isinstance(part_text, str):
            combined_chunks.append(part_text)
    combined = "\n".join(combined_chunks)
    assert f"artifact_id='{source_artifact_id}'" in combined
    assert "content_status='pending_video_transcript'" in combined
    assert "do not claim to know spoken content" in combined


