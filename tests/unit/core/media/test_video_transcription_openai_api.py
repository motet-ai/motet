"""
Motet - openai_api video transcription backend tests (ADR-0119)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-11

Description:
    Unit tests for the hosted OpenAI /v1/audio/transcriptions backend:
    backend selection, model name mapping, segment timestamp mapping,
    chunk offset accounting, speaker diarization (diarized_json request
    shape, speaker labels in segments and rendered transcripts), and
    structured degradation (missing key, HTTP errors, network failures)
    that must never raise.

Dependencies:
    - pytest with monkeypatch for requests stubbing

Usage:
    pytest tests/unit/core/media/test_video_transcription_openai_api.py -v
"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from motet.core.media import video_processing as vp


@pytest.fixture
def wav_file() -> Any:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(b"RIFF0000WAVE")
        path = tmp.name
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _config(**overrides: Any) -> SimpleNamespace:
    base = {
        "video_transcription_enabled": True,
        "video_transcription_backend": "openai_api",
        "video_transcription_model": "base",
        "video_transcription_language": "",
        "video_transcription_api_base": "https://api.openai.com/v1",
        "openai_api_key": "sk-test",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_missing_api_key_skips_without_http_call(monkeypatch: pytest.MonkeyPatch, wav_file: str) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls: list[Any] = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(a) or _FakeResponse())

    segments = vp._transcribe_openai_api(wav_file, _config(openai_api_key=None))

    assert segments == []
    assert calls == []


def test_verbose_json_segments_are_mapped(monkeypatch: pytest.MonkeyPatch, wav_file: str) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        captured["url"] = url
        captured["data"] = kwargs.get("data")
        captured["headers"] = kwargs.get("headers")
        return _FakeResponse(
            payload={
                "text": "hello world",
                "segments": [
                    {"start": 0.0, "end": 1.5, "text": " hello "},
                    {"start": 1.5, "end": 3.0, "text": "world"},
                    {"start": 3.0, "end": 4.0, "text": "   "},
                ],
            }
        )

    monkeypatch.setattr(requests, "post", fake_post)

    segments = vp._transcribe_openai_api(wav_file, _config())

    assert captured["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    # Local whisper model name "base" must be mapped to the hosted default.
    assert captured["data"]["model"] == "whisper-1"
    assert captured["data"]["response_format"] == "verbose_json"
    assert [(s.start_ms, s.end_ms, s.text) for s in segments] == [
        (0, 1500, "hello"),
        (1500, 3000, "world"),
    ]


def test_explicit_hosted_model_and_language_pass_through(monkeypatch: pytest.MonkeyPatch, wav_file: str) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        captured["data"] = kwargs.get("data")
        return _FakeResponse(payload={"text": "bonjour", "duration": 2.0})

    monkeypatch.setattr(requests, "post", fake_post)

    config = _config(video_transcription_model="gpt-4o-transcribe", video_transcription_language="fr")
    segments = vp._transcribe_openai_api(wav_file, config)

    assert captured["data"]["model"] == "gpt-4o-transcribe"
    assert captured["data"]["language"] == "fr"
    # gpt-4o family does not support verbose_json; whole text becomes one segment.
    assert captured["data"]["response_format"] == "json"
    assert [(s.start_ms, s.end_ms, s.text) for s in segments] == [(0, 2000, "bonjour")]


def test_http_error_degrades_to_empty(monkeypatch: pytest.MonkeyPatch, wav_file: str) -> None:
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse(status_code=429, text="rate limited"))
    assert vp._transcribe_openai_api(wav_file, _config()) == []


def test_network_failure_degrades_to_empty(monkeypatch: pytest.MonkeyPatch, wav_file: str) -> None:
    def fake_post(*args: Any, **kwargs: Any) -> _FakeResponse:
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(requests, "post", fake_post)
    assert vp._transcribe_openai_api(wav_file, _config()) == []


def test_chunk_offsets_shift_segment_timestamps(monkeypatch: pytest.MonkeyPatch, wav_file: str) -> None:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(b"RIFF0000WAVE")
        second_chunk = tmp.name

    monkeypatch.setattr(vp, "_split_wav_for_api", lambda path: [wav_file, second_chunk])

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(payload={"segments": [{"start": 1.0, "end": 2.0, "text": "chunk"}]})

    monkeypatch.setattr(requests, "post", fake_post)

    segments = vp._transcribe_openai_api(wav_file, _config())

    offset_ms = vp._OPENAI_API_CHUNK_SECONDS * 1000
    assert [(s.start_ms, s.end_ms) for s in segments] == [
        (1000, 2000),
        (offset_ms + 1000, offset_ms + 2000),
    ]
    # Extra chunk files are cleaned up; the original wav is the caller's.
    assert not os.path.exists(second_chunk)
    assert os.path.exists(wav_file)


def test_diarize_model_requests_diarized_json_and_maps_speakers(
    monkeypatch: pytest.MonkeyPatch, wav_file: str
) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        captured["data"] = kwargs.get("data")
        return _FakeResponse(
            payload={
                "segments": [
                    {"start": 0.0, "end": 2.0, "text": "hello there", "speaker": "A"},
                    {"start": 2.0, "end": 4.5, "text": "hi back", "speaker": "B"},
                    {"start": 4.5, "end": 5.0, "text": "unlabeled"},
                ]
            }
        )

    monkeypatch.setattr(requests, "post", fake_post)

    config = _config(video_transcription_model="gpt-4o-transcribe-diarize")
    segments = vp._transcribe_openai_api(wav_file, config)

    assert captured["data"]["model"] == "gpt-4o-transcribe-diarize"
    assert captured["data"]["response_format"] == "diarized_json"
    assert captured["data"]["chunking_strategy"] == "auto"
    assert [(s.start_ms, s.end_ms, s.text, s.speaker) for s in segments] == [
        (0, 2000, "hello there", "A"),
        (2000, 4500, "hi back", "B"),
        (4500, 5000, "unlabeled", None),
    ]


def test_diarize_multi_chunk_logs_consistency_warning(
    monkeypatch: pytest.MonkeyPatch, wav_file: str
) -> None:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(b"RIFF0000WAVE")
        second_chunk = tmp.name

    monkeypatch.setattr(vp, "_split_wav_for_api", lambda path: [wav_file, second_chunk])

    warnings: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        vp.logger, "warning", lambda event, **kw: warnings.append((event, kw))
    )
    monkeypatch.setattr(
        requests,
        "post",
        lambda url, **kwargs: _FakeResponse(
            payload={"segments": [{"start": 0.0, "end": 1.0, "text": "x", "speaker": "A"}]}
        ),
    )

    segments = vp._transcribe_openai_api(
        wav_file, _config(video_transcription_model="gpt-4o-transcribe-diarize")
    )

    assert len(segments) == 2
    assert ("video_transcription_diarization_chunked", {"chunk_count": 2, "reason": "speaker_labels_not_consistent_across_chunks"}) in warnings


def test_render_transcript_includes_speaker_labels() -> None:
    segments = [
        vp.TranscriptSegment(start_ms=0, end_ms=2000, text="hello", speaker="A"),
        vp.TranscriptSegment(start_ms=2000, end_ms=4000, text="world"),
    ]
    assert vp.render_transcript(segments) == "[0-2000] A: hello\n[2000-4000] world"


def test_transcribe_wav_routes_openai_api_backend(monkeypatch: pytest.MonkeyPatch, wav_file: str) -> None:
    sentinel = [vp.TranscriptSegment(start_ms=0, end_ms=1000, text="routed")]
    monkeypatch.setattr(vp, "_transcribe_openai_api", lambda path, config: sentinel)

    assert vp._transcribe_wav(wav_file, _config()) == sentinel


def test_transcribe_wav_backend_none_skips(wav_file: str) -> None:
    assert vp._transcribe_wav(wav_file, _config(video_transcription_backend="none")) == []
