"""
Motet - Generated Media Collection Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Unit tests for ADR-0113 generated-media surfacing. Validates the turn-level
    media collector, the agentic-loop media accumulator merge, the tool-result
    media extractor, and transcript persistence of generated media so the chat
    surface/UI can render generated images regardless of how the turn propagated
    its tool output (loop accumulator, structured tool_results, or inlined text).

Dependencies:
    - pytest: test runner
    - motet.core.orchestration.turn.complete: _collect_generated_media and helpers
    - motet.core.reasoning.react.agentic_loop: _merge_tool_result_media
    - motet.core.reasoning.react.loop_results: build_loop_result
    - motet.core.commands.builtin.tool: _extract_result_media
    - motet.core.conversations.transcript_codec: build_transcript_items_for_turn
    - motet.core.tools.tool_transcripts: ToolInvocation, ToolInvocationStatus

Usage:
    pytest tests/unit/core/orchestration/test_generated_media_collection.py

Notes:
    - No Redis/docker required; artifact store is faked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from motet.core.orchestration.turn.complete import (
    _collect_generated_media,
    _media_type_for_content_type,
    _validate_and_enrich_media,
)
from motet.core.reasoning.react.agentic_loop import _merge_tool_result_media
from motet.core.reasoning.react.loop_results import build_loop_result
from motet.core.commands.builtin.tool import _extract_result_media


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeMeta:
    content_type: str


class FakeArtifactStore:
    """Returns metadata for known ids; None for unknown (treated as missing)."""

    def __init__(self, known: Optional[Dict[str, str]] = None) -> None:
        self._known = known or {}

    def get_metadata(self, artifact_id: str) -> Optional[_FakeMeta]:
        ct = self._known.get(artifact_id)
        return _FakeMeta(content_type=ct) if ct else None


def _image_part(artifact_id: str, mime: str = "image/png") -> Dict[str, Any]:
    return {
        "type": "media",
        "media_type": "image",
        "mime_type": mime,
        "artifact_id": artifact_id,
        "alt": "a cat",
    }


def _tool_result_entry(artifact_id: str, status: str = "success") -> Dict[str, Any]:
    return {
        "tool_call_id": f"call_{artifact_id}",
        "tool_name": "core.image_generation",
        "status": status,
        "result": {"status": "ok", "media": [_image_part(artifact_id)]},
    }


# ---------------------------------------------------------------------------
# _media_type_for_content_type
# ---------------------------------------------------------------------------


def test_media_type_mapping():
    assert _media_type_for_content_type("image/png") == "image"
    assert _media_type_for_content_type("AUDIO/mpeg") == "audio"
    assert _media_type_for_content_type("video/mp4") == "video"
    assert _media_type_for_content_type("application/pdf") == "file"
    assert _media_type_for_content_type("") == "file"


# ---------------------------------------------------------------------------
# _collect_generated_media — sources
# ---------------------------------------------------------------------------


def test_collect_from_top_level_media_accumulator():
    payload = {"final_response": "done", "media": [_image_part("a1")]}
    out = _collect_generated_media(payload)
    assert [p["artifact_id"] for p in out] == ["a1"]


def test_collect_from_nested_data_media():
    payload = {"data": {"media": [_image_part("a1")]}}
    out = _collect_generated_media(payload)
    assert [p["artifact_id"] for p in out] == ["a1"]


def test_collect_from_tool_results():
    payload = {"tool_results": [_tool_result_entry("a2")]}
    out = _collect_generated_media(payload)
    assert [p["artifact_id"] for p in out] == ["a2"]


def test_collect_from_text_fallback_synthesizes_image_part():
    text = "Here you go: ![Blue cat](artifact:a3) enjoy"
    out = _collect_generated_media({"tool_results": []}, text)
    assert len(out) == 1
    assert out[0]["artifact_id"] == "a3"
    assert out[0]["media_type"] == "image"
    assert out[0]["alt"] == "Blue cat"


def test_collect_dedupes_across_sources():
    # Same artifact id appears in the accumulator, tool_results, and inline text.
    payload = {
        "media": [_image_part("dup")],
        "tool_results": [_tool_result_entry("dup")],
    }
    text = "![x](artifact:dup)"
    out = _collect_generated_media(payload, text)
    assert [p["artifact_id"] for p in out] == ["dup"]


def test_collect_empty_for_non_dict_payload():
    assert _collect_generated_media(None) == []
    assert _collect_generated_media("nope") == []


# ---------------------------------------------------------------------------
# _validate_and_enrich_media — existence + mime resolution
# ---------------------------------------------------------------------------


def test_validate_drops_missing_artifact():
    store = FakeArtifactStore(known={"present": "image/png"})
    parts = [_image_part("present"), {"type": "media", "artifact_id": "ghost"}]
    out = _validate_and_enrich_media(parts, store)
    assert [p["artifact_id"] for p in out] == ["present"]


def test_validate_enriches_mime_for_text_synthesized_part():
    # A text-synthesized part has no mime_type; the store fills the real one.
    store = FakeArtifactStore(known={"a4": "image/jpeg"})
    out = _collect_generated_media({}, "![y](artifact:a4)", artifact_store=store)
    assert len(out) == 1
    assert out[0]["mime_type"] == "image/jpeg"
    assert out[0]["media_type"] == "image"


def test_validate_resolves_non_image_media_type():
    store = FakeArtifactStore(known={"doc": "application/pdf"})
    out = _validate_and_enrich_media([{"type": "media", "artifact_id": "doc"}], store)
    assert out[0]["media_type"] == "file"
    assert out[0]["mime_type"] == "application/pdf"


def test_validate_no_store_keeps_parts_unchanged():
    parts = [_image_part("a5")]
    out = _validate_and_enrich_media(parts, None)
    assert out == parts


def test_validate_keeps_part_on_lookup_error():
    class BoomStore:
        def get_metadata(self, artifact_id: str):
            raise RuntimeError("backend down")

    parts = [_image_part("a6")]
    out = _validate_and_enrich_media(parts, BoomStore())
    assert [p["artifact_id"] for p in out] == ["a6"]


# ---------------------------------------------------------------------------
# agentic_loop accumulator
# ---------------------------------------------------------------------------


def test_merge_tool_result_media_accumulates_and_dedupes():
    acc: List[Dict[str, Any]] = [_image_part("existing")]
    _merge_tool_result_media(acc, [_tool_result_entry("existing"), _tool_result_entry("new")])
    assert [p["artifact_id"] for p in acc] == ["existing", "new"]


def test_merge_tool_result_media_skips_errors():
    acc: List[Dict[str, Any]] = []
    _merge_tool_result_media(acc, [_tool_result_entry("err", status="error")])
    assert acc == []


def test_loop_result_attaches_media_when_present():
    payload = build_loop_result("done", [], 1, "stop", {}, media=[_image_part("a7")])
    assert payload["media"] == [_image_part("a7")]


def test_loop_result_omits_media_when_empty():
    payload = build_loop_result("done", [], 1, "stop", {}, media=[])
    assert "media" not in payload


# ---------------------------------------------------------------------------
# tool result extraction
# ---------------------------------------------------------------------------


def test_extract_result_media_returns_parts():
    result_value = {"status": "ok", "media": [_image_part("a8")]}
    out = _extract_result_media(result_value)
    assert out is not None
    assert out[0]["artifact_id"] == "a8"


def test_extract_result_media_none_without_media():
    assert _extract_result_media({"status": "ok"}) is None
    assert _extract_result_media("plain string") is None
    assert _extract_result_media({"media": [{"no": "artifact"}]}) is None


# ---------------------------------------------------------------------------
# transcript persistence (ADR-0113)
# ---------------------------------------------------------------------------


def test_transcript_tool_call_result_includes_media():
    from motet.core.conversations.transcript_codec import build_transcript_items_for_turn
    from motet.core.tools.tool_transcripts import ToolInvocation, ToolInvocationStatus

    inv = ToolInvocation(
        tool_name="core.image_generation",
        tool_call_id="call_1",
        status=ToolInvocationStatus.SUCCESS,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        preview_observation="image_generation(status=ok, images=1)",
        artifact_id=None,
        result_media=[_image_part("gen_1")],
    )

    items = build_transcript_items_for_turn(messages=[], invocations=[inv])
    results = [it for it in items if hasattr(it, "output") and isinstance(getattr(it, "output"), dict)]
    assert len(results) == 1
    output = results[0].output
    assert output.get("media") == [_image_part("gen_1")]
