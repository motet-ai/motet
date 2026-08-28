"""
Motet - Video Artifact Derivation

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Derives poster frames, keyframe images, and optional transcripts from
    uploaded video artifacts using ffmpeg subprocess pipelines. Derived
    keyframes are stored as ordinary JPEG image artifacts so vision-capable
    models consume them without protocol changes.

    Per the visual and transcript derivations are independent
    entry points (derive_video_visual_artifacts / derive_video_transcript_artifact)
    so the derive_video_visuals and derive_video_transcript commands can run
    in parallel on different workers. Transcription backends are pluggable:
    "whisper_cli" (in-worker Whisper, zero egress) and "openai_api" (hosted
    /v1/audio/transcriptions; sends tenant audio to OpenAI). The openai_api
    backend supports speaker diarization when video_transcription_model is a
    diarize-family model (e.g. gpt-4o-transcribe-diarize): segments gain
    speaker labels rendered inline in the transcript artifact. Labels are
    per-request, so multi-chunk uploads log a structured consistency warning.

Dependencies:
    - ffmpeg/ffprobe CLI (WorkerCapability.MEDIA_PROCESSING)
    - motet.core.artifacts for storage and lineage
    - motet.core.config for transcription backend selection
    - requests (openai_api backend only)

Usage:
    visuals = derive_video_visual_artifacts(
        source_artifact_id="...",
        tenant_id="tenant",
        keyframe_strategy=KeyframeStrategy.SCENE,
        max_keyframes=12,
    )
    transcript = derive_video_transcript_artifact(
        source_artifact_id="...",
        tenant_id="tenant",
    )
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

import structlog

from ..artifacts import get_artifact_store
from ..artifacts.types import ArtifactKind
from ..config import Config
from .exceptions import DerivationError

logger = structlog.get_logger(__name__)

_FFMPEG_TIMEOUT_SECONDS = 600
_FFPROBE_TIMEOUT_SECONDS = 60

# Poster/keyframe JPEGs are LLM prompt images (ADR-0120 Phase 3); cap the longest
# side so a native 1080p/4K frame does not become a multi-megabyte prompt part.
# Only downscales (`min(...)` keeps smaller frames untouched); -2 preserves aspect
# ratio with an even height as required by the JPEG encoder.
_FRAME_DOWNSCALE_FILTER = "scale='min(1024,iw)':-2"

# OpenAI /v1/audio/transcriptions rejects uploads over 25 MB. Our extracted
# WAVs are 16 kHz mono s16 (32 kB/s), so 600s chunks are ~19.2 MB — safely
# under the limit while keeping per-request latency reasonable (ADR-0119).
_OPENAI_API_MAX_UPLOAD_BYTES = 24 * 1024 * 1024
_OPENAI_API_CHUNK_SECONDS = 600
_OPENAI_API_TIMEOUT_SECONDS = 300

# Local Whisper model names; when the deployment switches the backend to
# openai_api without updating video_transcription_model, fall back to the
# hosted default instead of sending an invalid model name.
_LOCAL_WHISPER_MODEL_NAMES = {"tiny", "base", "small", "medium", "large", "turbo"}
_OPENAI_API_DEFAULT_MODEL = "whisper-1"


class KeyframeStrategy(str, Enum):
    """Keyframe extraction policy for video derivation."""

    SCENE = "scene"
    INTERVAL = "interval"


@dataclass(frozen=True)
class VideoProbe:
    """ffprobe summary for a video file."""

    duration_ms: int
    width: Optional[int]
    height: Optional[int]
    has_audio: bool


@dataclass(frozen=True)
class ExtractedFrame:
    """A keyframe extracted from video with timestamp metadata."""

    data: bytes
    t_ms: int
    index: int


@dataclass(frozen=True)
class TranscriptSegment:
    """A timestamped transcript segment with optional speaker label."""

    start_ms: int
    end_ms: int
    text: str
    speaker: Optional[str] = None


def _require_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise DerivationError(
            "ffmpeg/ffprobe not available on this worker; "
            "video derivation requires WorkerCapability.MEDIA_PROCESSING"
        )


def _run_subprocess(cmd: List[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DerivationError(f"ffmpeg command timed out after {timeout}s: {' '.join(cmd)}") from exc


def ffprobe_json(path: str) -> VideoProbe:
    """Probe video duration, dimensions, and audio presence."""

    _require_ffmpeg()
    result = _run_subprocess(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            path,
        ],
        timeout=_FFPROBE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise DerivationError(f"ffprobe failed: {result.stderr.strip() or result.stdout.strip()}")

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise DerivationError("ffprobe returned invalid JSON") from exc

    duration_sec = 0.0
    fmt = payload.get("format") or {}
    try:
        duration_sec = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration_sec = 0.0

    width: Optional[int] = None
    height: Optional[int] = None
    has_audio = False
    for stream in payload.get("streams") or []:
        codec_type = str(stream.get("codec_type") or "")
        if codec_type == "video" and width is None:
            try:
                width = int(stream.get("width") or 0) or None
                height = int(stream.get("height") or 0) or None
            except (TypeError, ValueError):
                width = None
                height = None
        if codec_type == "audio":
            has_audio = True

    return VideoProbe(
        duration_ms=max(0, int(duration_sec * 1000)),
        width=width,
        height=height,
        has_audio=has_audio,
    )


def extract_poster(path: str, *, duration_ms: int) -> bytes:
    """Extract a single representative JPEG poster frame."""

    _require_ffmpeg()
    seek_sec = max(0.0, (duration_ms / 1000.0) * 0.1)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        out_path = tmp.name
    try:
        result = _run_subprocess(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{seek_sec:.3f}",
                "-i",
                path,
                "-vf",
                _FRAME_DOWNSCALE_FILTER,
                "-vframes",
                "1",
                "-q:v",
                "2",
                out_path,
            ],
            timeout=_FFMPEG_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise DerivationError(f"poster extraction failed: {result.stderr.strip()}")
        with open(out_path, "rb") as handle:
            data = handle.read()
        if not data:
            raise DerivationError("poster extraction produced empty output")
        return data
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _parse_showinfo_timestamps(stderr: str) -> List[int]:
    """Parse ``pts_time`` values from ffmpeg showinfo filter stderr."""

    times_ms: List[int] = []
    for match in re.finditer(r"pts_time:([0-9.]+)", stderr):
        try:
            times_ms.append(int(float(match.group(1)) * 1000))
        except (TypeError, ValueError):
            continue
    return times_ms


def _extract_keyframes_with_strategy(
    path: str,
    *,
    strategy: KeyframeStrategy,
    max_keyframes: int,
    duration_ms: int,
    scene_threshold: float = 0.3,
) -> List[ExtractedFrame]:
    """Run one ffmpeg keyframe extraction pass; returns empty list on failure."""

    max_keyframes = max(1, min(int(max_keyframes), 60))

    with tempfile.TemporaryDirectory(prefix="motet-keyframes-") as tmpdir:
        pattern = os.path.join(tmpdir, "frame_%03d.jpg")
        if strategy == KeyframeStrategy.SCENE:
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                path,
                "-vf",
                f"select='gt(scene,{scene_threshold})',showinfo,{_FRAME_DOWNSCALE_FILTER}",
                "-fps_mode",
                "vfr",
                "-frames:v",
                str(max_keyframes),
                "-q:v",
                "2",
                pattern,
            ]
        else:
            duration_sec = max(0.1, duration_ms / 1000.0) if duration_ms > 0 else 5.0
            interval_sec = max(0.25, duration_sec / max_keyframes)
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                path,
                "-vf",
                f"fps=1/{interval_sec:.3f},{_FRAME_DOWNSCALE_FILTER}",
                "-frames:v",
                str(max_keyframes),
                "-q:v",
                "2",
                pattern,
            ]

        result = _run_subprocess(cmd, timeout=_FFMPEG_TIMEOUT_SECONDS)
        if result.returncode != 0:
            logger.warning(
                "keyframe_extraction_pass_failed",
                strategy=strategy.value,
                stderr=result.stderr.strip()[:500],
            )
            return []

        timestamps = _parse_showinfo_timestamps(result.stderr) if strategy == KeyframeStrategy.SCENE else []
        frames: List[ExtractedFrame] = []
        files = sorted(
            name for name in os.listdir(tmpdir) if name.startswith("frame_") and name.endswith(".jpg")
        )
        for index, name in enumerate(files[:max_keyframes]):
            file_path = os.path.join(tmpdir, name)
            with open(file_path, "rb") as handle:
                data = handle.read()
            if not data:
                continue
            t_ms = timestamps[index] if index < len(timestamps) else int((duration_ms / max(len(files), 1)) * index)
            frames.append(ExtractedFrame(data=data, t_ms=t_ms, index=index))
        return frames


def extract_keyframes(
    path: str,
    *,
    strategy: KeyframeStrategy,
    max_keyframes: int,
    duration_ms: int,
    scene_threshold: float = 0.3,
) -> List[ExtractedFrame]:
    """Extract scene-change or interval keyframes as JPEG bytes."""

    _require_ffmpeg()
    frames = _extract_keyframes_with_strategy(
        path,
        strategy=strategy,
        max_keyframes=max_keyframes,
        duration_ms=duration_ms,
        scene_threshold=scene_threshold,
    )
    if not frames and strategy == KeyframeStrategy.SCENE:
        logger.info("keyframe_scene_fallback_to_interval", duration_ms=duration_ms)
        frames = _extract_keyframes_with_strategy(
            path,
            strategy=KeyframeStrategy.INTERVAL,
            max_keyframes=max_keyframes,
            duration_ms=duration_ms,
        )
    if not frames:
        raise DerivationError("keyframe extraction produced no frames")
    return frames


def extract_audio_wav(path: str) -> str:
    """Extract mono 16 kHz WAV audio; returns temp file path (caller must unlink)."""

    _require_ffmpeg()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        out_path = tmp.name
    result = _run_subprocess(
        [
            "ffmpeg",
            "-y",
            "-i",
            path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            out_path,
        ],
        timeout=_FFMPEG_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        raise DerivationError(f"audio extraction failed: {result.stderr.strip()}")
    return out_path


def _split_wav_for_api(wav_path: str) -> List[str]:
    """Split a WAV into sequential chunks under the hosted API upload limit.

    Returns [wav_path] unchanged when the file is already small enough.
    Caller owns cleanup of any returned chunk paths (other than wav_path).
    """

    if os.path.getsize(wav_path) <= _OPENAI_API_MAX_UPLOAD_BYTES:
        return [wav_path]

    out_dir = os.path.dirname(wav_path) or "."
    base = os.path.splitext(os.path.basename(wav_path))[0]
    pattern = os.path.join(out_dir, f"{base}_chunk%04d.wav")
    result = _run_subprocess(
        [
            "ffmpeg",
            "-y",
            "-i",
            wav_path,
            "-f",
            "segment",
            "-segment_time",
            str(_OPENAI_API_CHUNK_SECONDS),
            "-c",
            "copy",
            pattern,
        ],
        timeout=_FFMPEG_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise DerivationError(f"audio chunking failed: {result.stderr.strip()}")
    chunks = sorted(
        os.path.join(out_dir, name)
        for name in os.listdir(out_dir)
        if name.startswith(f"{base}_chunk") and name.endswith(".wav")
    )
    if not chunks:
        raise DerivationError("audio chunking produced no output files")
    return chunks


def _transcribe_openai_api(wav_path: str, config: Config) -> List[TranscriptSegment]:
    """Transcribe via OpenAI /v1/audio/transcriptions (ADR-0119 openai_api backend).

    Sends tenant audio to OpenAI — choosing this backend is the deployment's
    explicit egress authorization. Missing API key and HTTP failures degrade
    to an empty transcript with a structured log, never a derivation failure.
    """

    import requests

    api_key = str(getattr(config, "openai_api_key", "") or "") or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        logger.warning("video_transcription_skipped", reason="openai_api_key_missing")
        return []

    model = str(getattr(config, "video_transcription_model", "") or "").strip()
    if not model or model.lower() in _LOCAL_WHISPER_MODEL_NAMES:
        model = _OPENAI_API_DEFAULT_MODEL
    language = str(getattr(config, "video_transcription_language", "") or "").strip()
    base_url = str(
        getattr(config, "video_transcription_api_base", "") or "https://api.openai.com/v1"
    ).rstrip("/")

    # whisper-1 returns per-segment timestamps via verbose_json; the
    # gpt-4o-transcribe family only supports json/text (no segments); the
    # diarize family returns speaker-labeled segments via diarized_json and
    # requires chunking_strategy for audio longer than 30 seconds (ADR-0119).
    diarize = "diarize" in model.lower()
    verbose = not diarize and not model.startswith("gpt-4o")

    chunks = _split_wav_for_api(wav_path)
    if diarize and len(chunks) > 1:
        # Speaker labels are assigned per request; without known-speaker
        # reference carry-forward, "Speaker A" in one chunk is not guaranteed
        # to be "Speaker A" in the next (ADR-0119 §Future patterns).
        logger.warning(
            "video_transcription_diarization_chunked",
            chunk_count=len(chunks),
            reason="speaker_labels_not_consistent_across_chunks",
        )
    segments: List[TranscriptSegment] = []
    try:
        for chunk_index, chunk_path in enumerate(chunks):
            offset_ms = chunk_index * _OPENAI_API_CHUNK_SECONDS * 1000
            form: Dict[str, Any] = {"model": model}
            if language:
                form["language"] = language
            if diarize:
                form["response_format"] = "diarized_json"
                form["chunking_strategy"] = "auto"
            else:
                form["response_format"] = "verbose_json" if verbose else "json"
            try:
                with open(chunk_path, "rb") as handle:
                    response = requests.post(
                        f"{base_url}/audio/transcriptions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        data=form,
                        files={"file": (os.path.basename(chunk_path), handle, "audio/wav")},
                        timeout=_OPENAI_API_TIMEOUT_SECONDS,
                    )
            except requests.RequestException as exc:
                logger.warning(
                    "video_transcription_failed",
                    backend="openai_api",
                    chunk_index=chunk_index,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                return []
            if response.status_code != 200:
                logger.warning(
                    "video_transcription_failed",
                    backend="openai_api",
                    chunk_index=chunk_index,
                    status_code=response.status_code,
                    body=response.text[:500],
                )
                return []
            try:
                payload = response.json()
            except ValueError:
                logger.warning(
                    "video_transcription_failed",
                    backend="openai_api",
                    chunk_index=chunk_index,
                    reason="invalid_json_response",
                )
                return []

            api_segments = payload.get("segments") if isinstance(payload, dict) else None
            if api_segments:
                for item in api_segments:
                    try:
                        segments.append(
                            TranscriptSegment(
                                start_ms=offset_ms + int(float(item.get("start") or 0.0) * 1000),
                                end_ms=offset_ms + int(float(item.get("end") or 0.0) * 1000),
                                text=str(item.get("text") or "").strip(),
                                speaker=str(item.get("speaker") or "").strip() or None,
                            )
                        )
                    except (TypeError, ValueError):
                        continue
            else:
                text = str((payload or {}).get("text") or "").strip()
                if text:
                    duration_ms = int(float((payload or {}).get("duration") or 0.0) * 1000)
                    segments.append(
                        TranscriptSegment(
                            start_ms=offset_ms,
                            end_ms=offset_ms + duration_ms,
                            text=text,
                        )
                    )
    finally:
        for chunk_path in chunks:
            if chunk_path != wav_path:
                try:
                    os.unlink(chunk_path)
                except OSError:
                    pass

    return [seg for seg in segments if seg.text]


def _transcribe_wav(wav_path: str, config: Config) -> List[TranscriptSegment]:
    """Transcribe audio using the worker-configured backend."""

    if not bool(getattr(config, "video_transcription_enabled", True)):
        return []

    backend = str(getattr(config, "video_transcription_backend", "none") or "none").strip().lower()
    if backend == "none":
        logger.info("video_transcription_skipped", reason="backend_disabled")
        return []

    if backend == "openai_api":
        return _transcribe_openai_api(wav_path, config)

    if backend == "whisper_cli":
        if not shutil.which("whisper"):
            logger.warning("video_transcription_skipped", reason="whisper_cli_not_on_path")
            return []
        model = str(getattr(config, "video_transcription_model", "base") or "base")
        language = str(getattr(config, "video_transcription_language", "") or "").strip()
        out_dir = os.path.dirname(wav_path) or "."
        cmd = [
            "whisper",
            wav_path,
            "--model",
            model,
            "--output_format",
            "json",
            # whisper writes outputs to CWD by default; pin to the wav's directory
            "--output_dir",
            out_dir,
        ]
        if language:
            cmd.extend(["--language", language])
        result = _run_subprocess(cmd, timeout=_FFMPEG_TIMEOUT_SECONDS)
        if result.returncode != 0:
            logger.warning("video_transcription_failed", stderr=result.stderr.strip())
            return []
        base_name = os.path.splitext(os.path.basename(wav_path))[0]
        json_path = os.path.join(out_dir, base_name + ".json")
        if not os.path.exists(json_path):
            logger.warning("video_transcription_failed", backend="whisper_cli", reason="output_json_missing")
            return []
        try:
            with open(json_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "video_transcription_failed",
                backend="whisper_cli",
                reason="output_json_unreadable",
                error=str(exc),
            )
            return []
        segments: List[TranscriptSegment] = []
        for item in payload.get("segments") or []:
            try:
                segments.append(
                    TranscriptSegment(
                        start_ms=int(float(item.get("start") or 0.0) * 1000),
                        end_ms=int(float(item.get("end") or 0.0) * 1000),
                        text=str(item.get("text") or "").strip(),
                    )
                )
            except (TypeError, ValueError):
                continue
        return [seg for seg in segments if seg.text]

    logger.warning("video_transcription_skipped", reason="unknown_backend", backend=backend)
    return []


def render_transcript(segments: List[TranscriptSegment]) -> str:
    """Render transcript segments as plain text with lightweight timestamps.

    Speaker labels (diarized backends, ADR-0119) are rendered inline so
    downstream consumers (RAG chunks, prepare_context injection) carry
    who-said-what with no further changes.
    """

    lines = []
    for seg in segments:
        if seg.speaker:
            lines.append(f"[{seg.start_ms}-{seg.end_ms}] {seg.speaker}: {seg.text}")
        else:
            lines.append(f"[{seg.start_ms}-{seg.end_ms}] {seg.text}")
    return "\n".join(lines).strip()


def _materialize_source_video(
    store: Any,
    *,
    source_artifact_id: str,
    tenant_id: str,
    principal_id: Optional[str],
    motet_id: Optional[str],
) -> tuple[str, Any, Optional[int]]:
    """Fetch and validate the source video, writing it to a temp file.

    Returns (src_path, source_meta, ttl_seconds). Caller owns src_path cleanup.
    """

    source_meta = store.get_metadata(
        source_artifact_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
    )
    if not source_meta:
        raise DerivationError(f"Source artifact {source_artifact_id} not found")
    if not str(source_meta.content_type or "").startswith("video/"):
        raise DerivationError(f"Source artifact is not a video: {source_meta.content_type}")

    source_bytes = store.get(
        source_artifact_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
    )
    if not source_bytes or not isinstance(source_bytes, (bytes, bytearray)):
        raise DerivationError("Source video payload missing or not binary")

    ttl_seconds = (
        int(source_meta.expires_at - source_meta.created_at)
        if source_meta.expires_at
        else None
    )

    suffix = ".mp4"
    filename = str(source_meta.metadata.get("filename") or "")
    if "." in filename:
        # Filename is user-controlled; only accept a safe alphanumeric extension.
        ext = filename.rsplit(".", 1)[-1]
        if re.fullmatch(r"[A-Za-z0-9]{1,8}", ext):
            suffix = "." + ext.lower()

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        src_path = tmp.name
        tmp.write(bytes(source_bytes))

    return src_path, source_meta, ttl_seconds


def derive_video_visual_artifacts(
    *,
    source_artifact_id: str,
    tenant_id: str,
    principal_id: Optional[str] = None,
    motet_id: Optional[str] = None,
    keyframe_strategy: KeyframeStrategy = KeyframeStrategy.SCENE,
    max_keyframes: int = 12,
    force_regenerate: bool = False,
) -> Dict[str, Any]:
    """
    Derive poster and keyframe artifacts from a video upload (ADR-0119 visual track).
    """

    store = get_artifact_store()
    config = Config()

    if not force_regenerate:
        existing_poster = store.list(
            kind=ArtifactKind.DERIVED_VIDEO_POSTER,
            source_artifact_id=source_artifact_id,
            limit=1,
            tenant_id=tenant_id,
            principal_id=principal_id,
            motet_id=motet_id,
        )
        existing_keyframes = store.list(
            kind=ArtifactKind.DERIVED_VIDEO_KEYFRAME,
            source_artifact_id=source_artifact_id,
            limit=max_keyframes,
            tenant_id=tenant_id,
            principal_id=principal_id,
            motet_id=motet_id,
        )
        if existing_poster and existing_keyframes:
            return {
                "status": "success",
                "source_artifact_id": source_artifact_id,
                "reused": True,
                "derivations": {
                    "poster": {"id": existing_poster[0].id},
                    "keyframes": [
                        {
                            "id": meta.id,
                            "t_ms": meta.metadata.get("t_ms"),
                            "index": meta.metadata.get("index"),
                        }
                        for meta in existing_keyframes
                    ],
                },
            }

    src_path, source_meta, ttl_seconds = _materialize_source_video(
        store,
        source_artifact_id=source_artifact_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
    )
    try:
        probe = ffprobe_json(src_path)
        poster_bytes = extract_poster(src_path, duration_ms=probe.duration_ms)
        try:
            scene_threshold = float(getattr(config, "video_scene_threshold", 0.3) or 0.3)
        except (TypeError, ValueError):
            scene_threshold = 0.3
        frames = extract_keyframes(
            src_path,
            strategy=keyframe_strategy,
            max_keyframes=max_keyframes,
            duration_ms=probe.duration_ms,
            scene_threshold=scene_threshold,
        )

        poster_id = store.put(
            payload=poster_bytes,
            content_type="image/jpeg",
            kind=ArtifactKind.DERIVED_VIDEO_POSTER,
            source_artifact_id=source_artifact_id,
            metadata={
                "source_filename": source_meta.metadata.get("filename"),
                "derivation_method": "ffmpeg_poster_v1",
                "t_ms": 0,
            },
            tenant_id=tenant_id,
            principal_id=principal_id,
            motet_id=motet_id,
            ttl_seconds=ttl_seconds,
        )

        keyframe_results: List[Dict[str, Any]] = []
        for frame in frames:
            kid = store.put(
                payload=frame.data,
                content_type="image/jpeg",
                kind=ArtifactKind.DERIVED_VIDEO_KEYFRAME,
                source_artifact_id=source_artifact_id,
                metadata={
                    "source_filename": source_meta.metadata.get("filename"),
                    "derivation_method": "ffmpeg_keyframe_v1",
                    "t_ms": frame.t_ms,
                    "index": frame.index,
                    "keyframe_strategy": keyframe_strategy.value,
                },
                tenant_id=tenant_id,
                principal_id=principal_id,
                motet_id=motet_id,
                ttl_seconds=ttl_seconds,
            )
            keyframe_results.append({"id": kid, "t_ms": frame.t_ms, "index": frame.index})

        return {
            "status": "success",
            "source_artifact_id": source_artifact_id,
            "probe": {
                "duration_ms": probe.duration_ms,
                "width": probe.width,
                "height": probe.height,
                "has_audio": probe.has_audio,
            },
            "derivations": {
                "poster": {"id": poster_id},
                "keyframes": keyframe_results,
            },
        }
    finally:
        try:
            os.unlink(src_path)
        except OSError:
            pass


def derive_video_transcript_artifact(
    *,
    source_artifact_id: str,
    tenant_id: str,
    principal_id: Optional[str] = None,
    motet_id: Optional[str] = None,
    force_regenerate: bool = False,
) -> Dict[str, Any]:
    """
    Derive a transcript artifact from a video upload (ADR-0119 transcript track).

    Skips cheaply (before fetching the payload) when transcription is disabled
    or no backend is configured; skips after probing when the video has no
    audio stream. Backend failures degrade to a skip, never a command failure.
    """

    store = get_artifact_store()
    config = Config()

    backend = str(getattr(config, "video_transcription_backend", "none") or "none").strip().lower()
    if not bool(getattr(config, "video_transcription_enabled", True)) or backend == "none":
        logger.info(
            "video_transcription_skipped",
            reason="backend_disabled",
            source_artifact_id=source_artifact_id,
        )
        return {
            "status": "skipped",
            "reason": "backend_disabled",
            "source_artifact_id": source_artifact_id,
            "derivations": {"transcript": None},
        }

    if not force_regenerate:
        existing_transcript = store.list(
            kind=ArtifactKind.DERIVED_VIDEO_TRANSCRIPT,
            source_artifact_id=source_artifact_id,
            limit=1,
            tenant_id=tenant_id,
            principal_id=principal_id,
            motet_id=motet_id,
        )
        if existing_transcript:
            return {
                "status": "success",
                "source_artifact_id": source_artifact_id,
                "reused": True,
                "derivations": {"transcript": {"id": existing_transcript[0].id}},
            }

    src_path, source_meta, ttl_seconds = _materialize_source_video(
        store,
        source_artifact_id=source_artifact_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
    )
    wav_path: Optional[str] = None
    try:
        probe = ffprobe_json(src_path)
        if not probe.has_audio:
            logger.info(
                "video_transcription_skipped",
                reason="no_audio_stream",
                source_artifact_id=source_artifact_id,
            )
            return {
                "status": "skipped",
                "reason": "no_audio_stream",
                "source_artifact_id": source_artifact_id,
                "derivations": {"transcript": None},
            }

        wav_path = extract_audio_wav(src_path)
        segments = _transcribe_wav(wav_path, config)
        if not segments:
            return {
                "status": "skipped",
                "reason": "no_transcript_produced",
                "source_artifact_id": source_artifact_id,
                "derivations": {"transcript": None},
            }

        transcript_id = store.put(
            payload=render_transcript(segments),
            content_type="text/plain",
            kind=ArtifactKind.DERIVED_VIDEO_TRANSCRIPT,
            source_artifact_id=source_artifact_id,
            metadata={
                "source_filename": source_meta.metadata.get("filename"),
                "derivation_method": "transcription_v1",
                "transcription_backend": backend,
                "segments": [
                    {
                        "start_ms": s.start_ms,
                        "end_ms": s.end_ms,
                        "text": s.text,
                        **({"speaker": s.speaker} if s.speaker else {}),
                    }
                    for s in segments
                ],
            },
            tenant_id=tenant_id,
            principal_id=principal_id,
            motet_id=motet_id,
            ttl_seconds=ttl_seconds,
        )

        return {
            "status": "success",
            "source_artifact_id": source_artifact_id,
            "segment_count": len(segments),
            "derivations": {"transcript": {"id": transcript_id}},
        }
    finally:
        try:
            os.unlink(src_path)
        except OSError:
            pass
        if wav_path:
            try:
                os.unlink(wav_path)
            except OSError:
                pass
