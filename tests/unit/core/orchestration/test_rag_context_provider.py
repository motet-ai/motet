"""
Motet - RAG Context Provider Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-12

Description:
    Unit tests for the ADR-0063 artifact RAG context provider. Validates disabled
    no-op behavior and enabled retrieval injection with full-document fallback
    removal when semantic chunks are available.

Dependencies:
    - types.SimpleNamespace for lightweight Motet/config stubs
    - motet.core.orchestration.context.rag_context for provider behavior
    - motet.core.types for canonical Message/TextPart objects

Usage:
    pytest tests/unit/core/orchestration/test_rag_context_provider.py

Notes:
    - The distributed retrieval command is represented by a stubbed `motet.do`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from motet.core.orchestration.context.rag_context import RagContextProvider
from motet.core.orchestration.context.types import ContextPipelineState
from motet.core.types import Message, TextPart


class _LoggerStub:
    def warning(self, *args: Any, **kwargs: Any) -> None:
        return None


def test_rag_context_provider_noops_when_disabled() -> None:
    state = ContextPipelineState(messages=[Message(role="user", content="question")])
    motet = SimpleNamespace(stack=SimpleNamespace(config=SimpleNamespace(artifact_rag_enabled=False)))

    out = RagContextProvider().apply(state, data=SimpleNamespace(), motet=motet, logger=_LoggerStub())

    assert out.context_info["rag_context_enabled"] is False
    assert out.messages[0].content_parts is None


def test_rag_context_provider_injects_chunks_and_keeps_small_full_attachment_text() -> None:
    message = Message(
        role="user",
        content="What does this document say?",
        content_parts=[
            TextPart(text="<attachment artifact_id='derived-1'>full document text</attachment>"),
            TextPart(text="What does this document say?"),
        ],
    )
    state = ContextPipelineState(messages=[message])

    class _MotetStub:
        conversation_id = "conv-1"
        stack = SimpleNamespace(
            config=SimpleNamespace(
                artifact_rag_enabled=True,
                artifact_rag_top_k=5,
                artifact_rag_similarity_threshold=0.0,
                artifact_rag_token_budget=4000,
            )
        )

        def do(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "chunks": [
                    {
                        "source_artifact_id": "source-1",
                        "derived_artifact_id": "derived-1",
                        "chunk_index": 2,
                        "chunk_kind": "text",
                        "content_text": "semantic hit",
                        "coordinates": {
                            "kind": "text",
                            "byte_start": 0,
                            "byte_end": 12,
                            "heading_path": ["EPIC 3", "Issue 3.1"],
                        },
                        "prep_strategy_id": "text_default",
                        "prep_strategy_version": "1.0.0",
                        "filename": "sample.pdf",
                        "page_number": 4,
                        "similarity": 0.91,
                    }
                ],
                "context_text": "[Source: sample.pdf] semantic hit",
            }

    out = RagContextProvider().apply(state, data=SimpleNamespace(), motet=_MotetStub(), logger=_LoggerStub())

    parts = out.messages[0].content_parts or []
    texts = [part.text for part in parts if getattr(part, "type", None) == "text"]
    assert out.context_info["rag_context_enabled"] is True
    assert out.context_info["rag_context_skipped"] == "full_text_in_budget"
    assert "artifact_rag_chunk_count" not in out.context_info
    assert any("<attachment " in text and "full document text" in text for text in texts)
    assert all(not text.startswith("<artifact_rag_context>") for text in texts)


def test_rag_context_provider_skips_when_turn_has_no_artifact_intent() -> None:
    state = ContextPipelineState(messages=[Message(role="user", content="What is the weather today?")])
    motet = SimpleNamespace(
        stack=SimpleNamespace(config=SimpleNamespace(artifact_rag_enabled=True)),
        conversation_id="conv-1",
    )

    out = RagContextProvider().apply(state, data=SimpleNamespace(), motet=motet, logger=_LoggerStub())

    assert out.context_info["rag_context_enabled"] is True
    assert out.context_info["rag_context_skipped"] == "no_artifact_rag_intent"
    assert out.messages[0].content_parts is None


def test_rag_context_provider_uses_recent_attachment_for_followup_question() -> None:
    prior_message = Message(
        role="user",
        content="what does it say?",
        content_parts=[TextPart(text="what does it say?")],
        attachments=[
            {
                "artifact_id": "source-1",
                "filename": "risk-analysis.pdf",
                "content_type": "application/pdf",
                "bytes": 211906,
            }
        ],
    )
    current_message = Message(
        role="user",
        content="what is the analysis?",
        content_parts=[TextPart(text="what is the analysis?")],
    )
    state = ContextPipelineState(
        messages=[
            prior_message,
            Message(role="assistant", content="I do not see the document."),
            current_message,
        ]
    )
    captured_data: dict[str, Any] = {}

    class _MotetStub:
        conversation_id = "conv-1"
        stack = SimpleNamespace(
            config=SimpleNamespace(
                artifact_rag_enabled=True,
                artifact_rag_top_k=5,
                artifact_rag_similarity_threshold=0.0,
                artifact_rag_token_budget=4000,
                artifact_rag_hybrid_enabled=True,
                artifact_rag_vector_weight=0.7,
                artifact_rag_lexical_weight=0.3,
                artifact_rag_candidate_multiplier=4,
            )
        )

        def do(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            captured_data["data"] = kwargs["data"]
            return {
                "chunks": [
                    {
                        "source_artifact_id": "source-1",
                        "chunk_index": 0,
                        "chunk_kind": "text",
                        "content_text": "compatibility risk analysis",
                        "coordinates": {"kind": "text", "byte_start": 0, "byte_end": 27},
                        "prep_strategy_id": "text_default",
                        "filename": "risk-analysis.pdf",
                    }
                ],
                "context_text": "[Source: risk-analysis.pdf] compatibility risk analysis",
            }

    out = RagContextProvider().apply(state, data=SimpleNamespace(), motet=_MotetStub(), logger=_LoggerStub())

    request_data = captured_data["data"]
    assert request_data.query_text == "what is the analysis?"
    assert request_data.scope == "conversation"
    assert out.context_info["artifact_rag_policy"]["should_retrieve"] is True
    assert out.context_info["artifact_rag_chunk_count"] == 1
    texts = [part.text for part in (out.messages[-1].content_parts or []) if getattr(part, "type", None) == "text"]
    assert texts[0].startswith("<artifact_rag_context>")
    assert "compatibility risk analysis" in texts[0]


def test_rag_context_provider_uses_analysis_signal_but_keeps_scope_conservative() -> None:
    message = Message(role="user", content="Search my documents for the refund policy.")
    state = ContextPipelineState(messages=[message])
    captured_data: dict[str, Any] = {}

    class _MotetStub:
        conversation_id = "conv-1"
        stack = SimpleNamespace(
            config=SimpleNamespace(
                artifact_rag_enabled=True,
                artifact_rag_top_k=5,
                artifact_rag_similarity_threshold=0.0,
                artifact_rag_token_budget=4000,
                artifact_rag_hybrid_enabled=True,
                artifact_rag_vector_weight=0.7,
                artifact_rag_lexical_weight=0.3,
                artifact_rag_candidate_multiplier=4,
            )
        )

        def do(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            captured_data["data"] = kwargs["data"]
            return {
                "chunks": [
                    {
                        "source_artifact_id": "source-1",
                        "chunk_index": 0,
                        "chunk_kind": "text",
                        "content_text": "refund policy",
                        "coordinates": {"kind": "text", "byte_start": 0, "byte_end": 13},
                        "prep_strategy_id": "text_default",
                    }
                ],
                "context_text": "[Source: policy.pdf] refund policy",
            }

    data = SimpleNamespace(
        analysis_metadata={
            "rag": {
                "needs_rag": True,
                "artifact_action": "question",
                "suggested_scope": "principal",
                "confidence": 0.9,
            }
        },
        context={
            "artifact_ids": ["source-1"],
            "artifact_tags": ["contracts"],
        },
    )

    out = RagContextProvider().apply(state, data=data, motet=_MotetStub(), logger=_LoggerStub())

    request_data = captured_data["data"]
    assert request_data.scope == "conversation"
    assert request_data.artifact_ids == ["source-1"]
    assert request_data.artifact_tags == ["contracts"]
    assert request_data.top_k == 3
    assert out.context_info["artifact_rag_chunk_count"] == 1


def test_rag_context_provider_keeps_video_transcript_when_only_video_scene_chunks() -> None:
    message = Message(
        role="user",
        content="what does this say?",
        content_parts=[
            TextPart(
                text=(
                    "<attachment artifact_id='transcript-1' source_artifact_id='video-1' "
                    "source_content_type='video/quicktime' filename='IMG_2178.MOV'>\n"
                    "[Use source_artifact_id for tools that need the original binary file; "
                    "artifact_id is the video transcript.]\n"
                    "[0-1200] Hello from the video\n</attachment>"
                )
            ),
            TextPart(text="what does this say?"),
        ],
    )
    state = ContextPipelineState(messages=[message])

    class _MotetStub:
        conversation_id = "conv-1"
        stack = SimpleNamespace(
            config=SimpleNamespace(
                artifact_rag_enabled=True,
                artifact_rag_top_k=3,
                artifact_rag_similarity_threshold=0.0,
                artifact_rag_token_budget=3000,
                artifact_rag_hybrid_enabled=True,
                artifact_rag_vector_weight=0.7,
                artifact_rag_lexical_weight=0.3,
                artifact_rag_candidate_multiplier=4,
            )
        )

        def do(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "chunks": [
                    {
                        "source_artifact_id": "video-1",
                        "derived_artifact_id": "kf-1",
                        "chunk_index": 2,
                        "chunk_kind": "video_scene",
                        "content_text": "Video scene at 1844ms from IMG_2178.MOV (keyframe kf-1)",
                        "filename": "IMG_2178.MOV",
                        "similarity": 0.16,
                    }
                ],
                "context_text": "[Source: IMG_2178.MOV] Video scene at 1844ms",
            }

    out = RagContextProvider().apply(state, data=SimpleNamespace(), motet=_MotetStub(), logger=_LoggerStub())

    texts = [part.text for part in (out.messages[0].content_parts or []) if getattr(part, "type", None) == "text"]
    assert out.context_info["rag_context_skipped"] == "full_text_in_budget"
    assert any("Hello from the video" in text for text in texts)
    assert any("<attachment " in text for text in texts)
    assert all(not text.startswith("<artifact_rag_context>") for text in texts)


def test_rag_context_provider_keeps_video_transcript_when_under_budget_and_segments_indexed() -> None:
    message = Message(
        role="user",
        content="what does this say?",
        content_parts=[
            TextPart(
                text=(
                    "<attachment artifact_id='transcript-1' source_artifact_id='video-1' "
                    "source_content_type='video/quicktime' filename='IMG_2178.MOV'>\n"
                    "artifact_id is the video transcript.\n"
                    "[0-1200] Hello from the video\n</attachment>"
                )
            ),
            TextPart(text="what does this say?"),
        ],
    )
    state = ContextPipelineState(messages=[message])

    class _MotetStub:
        conversation_id = "conv-1"
        stack = SimpleNamespace(
            config=SimpleNamespace(
                artifact_rag_enabled=True,
                artifact_rag_top_k=3,
                artifact_rag_similarity_threshold=0.0,
                artifact_rag_token_budget=3000,
                artifact_rag_hybrid_enabled=True,
                artifact_rag_vector_weight=0.7,
                artifact_rag_lexical_weight=0.3,
                artifact_rag_candidate_multiplier=4,
            )
        )

        def do(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "chunks": [
                    {
                        "source_artifact_id": "video-1",
                        "derived_artifact_id": "transcript-1",
                        "chunk_index": 11,
                        "chunk_kind": "transcript_segment",
                        "content_text": "Hello from the video",
                        "filename": "IMG_2178.MOV",
                        "similarity": 0.82,
                    }
                ],
                "context_text": "[Source: IMG_2178.MOV] Hello from the video",
            }

    out = RagContextProvider().apply(state, data=SimpleNamespace(), motet=_MotetStub(), logger=_LoggerStub())

    texts = [part.text for part in (out.messages[0].content_parts or []) if getattr(part, "type", None) == "text"]
    assert out.context_info["rag_context_skipped"] == "full_text_in_budget"
    assert any("<attachment " in text and "Hello from the video" in text for text in texts)
    assert all(not text.startswith("<artifact_rag_context>") for text in texts)


def test_rag_context_provider_skips_retrieval_for_signal_free_single_attachment() -> None:
    message = Message(
        role="user",
        content="what does this say?",
        attachments=[
            {
                "artifact_id": "video-1",
                "filename": "clip.mov",
                "content_type": "video/quicktime",
            }
        ],
    )
    state = ContextPipelineState(messages=[message])
    motet = SimpleNamespace(
        conversation_id="conv-1",
        stack=SimpleNamespace(
            config=SimpleNamespace(
                artifact_rag_enabled=True,
                artifact_rag_top_k=3,
                artifact_rag_token_budget=3000,
            )
        ),
    )

    out = RagContextProvider().apply(state, data=SimpleNamespace(), motet=motet, logger=_LoggerStub())

    assert out.context_info["rag_context_skipped"] == "signal_free_single_attachment"
    assert out.context_info["artifact_rag_policy"]["artifact_ids"] == ["video-1"]


def test_rag_context_provider_supersedes_only_when_inline_attachment_exceeds_budget() -> None:
    long_body = "word " * 4000
    message = Message(
        role="user",
        content="find the refund clause in this document",
        content_parts=[
            TextPart(
                text=(
                    "<attachment artifact_id='derived-1' source_artifact_id='source-1' "
                    "source_content_type='application/pdf' filename='big.pdf'>\n"
                    "[Use source_artifact_id for tools that need the original binary file; "
                    "artifact_id is the extracted text artifact.]\n"
                    f"{long_body}\n</attachment>"
                )
            ),
            TextPart(text="find the refund clause in this document"),
        ],
    )
    state = ContextPipelineState(messages=[message])

    class _MotetStub:
        conversation_id = "conv-1"
        stack = SimpleNamespace(
            config=SimpleNamespace(
                artifact_rag_enabled=True,
                artifact_rag_top_k=5,
                artifact_rag_similarity_threshold=0.0,
                artifact_rag_token_budget=500,
                artifact_rag_hybrid_enabled=True,
                artifact_rag_vector_weight=0.7,
                artifact_rag_lexical_weight=0.3,
                artifact_rag_candidate_multiplier=4,
            )
        )

        def do(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "chunks": [
                    {
                        "source_artifact_id": "source-1",
                        "derived_artifact_id": "derived-1",
                        "chunk_index": 0,
                        "chunk_kind": "text",
                        "content_text": "semantic hit",
                        "filename": "big.pdf",
                        "similarity": 0.91,
                    }
                ],
                "context_text": "[Source: big.pdf] semantic hit",
            }

    out = RagContextProvider().apply(state, data=SimpleNamespace(), motet=_MotetStub(), logger=_LoggerStub())

    texts = [part.text for part in (out.messages[0].content_parts or []) if getattr(part, "type", None) == "text"]
    assert out.context_info["artifact_rag_policy"]["position_ordered"] is True
    assert texts[0].startswith("<artifact_rag_context>")
    assert all("<attachment " not in text for text in texts)
