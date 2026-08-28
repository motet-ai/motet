"""
Motet - Video Artifact End-to-End Integration Test (ADR-0118)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-11

Description:
    Exercises the ADR-0118/ADR-0119 video artifact flow: upload →
    derive_video_visuals (poster/keyframes) + derive_video_transcript
    (parallel tracks) → video_default preparation → Valkey RAG indexing and
    retrieval. Also verifies MEDIA_PROCESSING capability gating on both
    derivation commands and worker advertisement when ffmpeg is present.

Dependencies:
    - pytest for Docker-backed integration execution
    - ffmpeg/ffprobe for fixture video generation and derivation
    - motet.core.artifacts for Redis artifact storage
    - motet.core.embedding for sibling embedding service calls
    - motet.core.commands.builtin for create/derive/index/retrieve

Usage:
    docker compose -f tests/docker-compose.test.yml --profile workers up -d embedding-server
    docker compose -f tests/docker-compose.test.yml run --rm \
      -e MOTET_EMBEDDING_TOPOLOGY=sibling \
      -e MOTET_EMBEDDING_ENDPOINT=http://embedding-server:8091 \
      test-runner python -m pytest tests/integration/test_video_artifact_e2e.py -v

Notes:
    - Skips when ffmpeg, Redis, or the embedding endpoint are unavailable.
    - Derivation and indexing run in-process (same pattern as test_artifact_rag_e2e).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from motet.core.artifacts import ArtifactKind, RedisArtifactStore, ScopedArtifactStore
from motet.core.embedding import create_embedding_service
from motet.core.commands.builtin import artifacts as artifact_commands
from motet.core.commands.builtin import derivation as derivation_commands
from motet.core.commands.builtin import rag as rag_commands
from motet.core.commands.command_data_classes import (
    CreateArtifactData,
    DeriveVideoTranscriptData,
    DeriveVideoVisualsData,
    PrepareArtifactIndexData,
    RagRetrieveContextData,
)
from motet.core.commands.capabilities import WorkerCapability
from motet.core.rag import ArtifactChunkRepository


def _wrapped(command_fn: Any) -> Any:
    return getattr(command_fn, "__wrapped__", command_fn)


def _require_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe required for ADR-0118 video integration test")


def _require_embedding_endpoint() -> str:
    endpoint = os.getenv("MOTET_EMBEDDING_ENDPOINT", "").strip().rstrip("/")
    if not endpoint:
        pytest.skip("MOTET_EMBEDDING_ENDPOINT is required for video artifact RAG E2E")
    try:
        urllib.request.urlopen(f"{endpoint}/healthz", timeout=5).read()
    except Exception as exc:
        pytest.skip(f"Embedding endpoint is not reachable for video artifact RAG E2E: {exc}")
    return endpoint


def _generate_fixture_video_bytes() -> bytes:
    """Build a tiny MP4 via ffmpeg testsrc (no external fixture files)."""

    _require_ffmpeg()
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        out_path = tmp.name
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=2:size=320x240:rate=5",
                "-pix_fmt",
                "yuv420p",
                "-t",
                "2",
                out_path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"ffmpeg fixture generation failed: {result.stderr.strip()}")
        with open(out_path, "rb") as handle:
            payload = handle.read()
        if len(payload) < 100:
            pytest.skip("ffmpeg fixture generation produced empty output")
        return payload
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def test_derive_video_commands_declare_media_processing_capability() -> None:
    visuals_cmd = derivation_commands.derive_video_visuals(
        task_id="task-cap",
        conversation_id="",
        tenant_id="tenant-cap",
        principal_id="principal-cap",
        motet_id="motet-cap",
        data=DeriveVideoVisualsData(source_artifact_id="video-src"),
    )
    assert WorkerCapability.MEDIA_PROCESSING in visuals_cmd.get_required_capabilities()

    transcript_cmd = derivation_commands.derive_video_transcript(
        task_id="task-cap",
        conversation_id="",
        tenant_id="tenant-cap",
        principal_id="principal-cap",
        motet_id="motet-cap",
        data=DeriveVideoTranscriptData(source_artifact_id="video-src"),
    )
    assert WorkerCapability.MEDIA_PROCESSING in transcript_cmd.get_required_capabilities()


def test_worker_advertises_media_processing_when_ffmpeg_present() -> None:
    _require_ffmpeg()
    from motet.core.workers.tasks import _detect_worker_capabilities

    caps = _detect_worker_capabilities(
        SimpleNamespace(config=SimpleNamespace(worker_media_processing=False)),
        [],
    )
    assert WorkerCapability.MEDIA_PROCESSING.value in caps


@pytest.mark.integration
@pytest.mark.e2e
@pytest.mark.requires_redis
def test_video_upload_derive_index_and_retrieve(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_ffmpeg()
    _require_embedding_endpoint()

    run_id = uuid.uuid4().hex
    tenant_id = f"tenant-video-e2e-{run_id}"
    principal_id = f"principal-video-e2e-{run_id}"
    motet_id = "motet-video-e2e"
    conversation_id = f"conversation-video-e2e-{run_id}"
    service_name = f"artifact_store_video_e2e_{run_id}"

    raw_store = RedisArtifactStore(service_name=service_name)
    scoped_store = ScopedArtifactStore(
        raw_store,
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
    )
    embedding_service = create_embedding_service(topology="sibling")
    repository = ArtifactChunkRepository(embedding_dim=embedding_service.get_embedding_dimension())

    cfg = SimpleNamespace(
        artifact_rag_enabled=True,
        artifact_rag_index_on_derivation=True,
        artifact_rag_top_k=5,
        artifact_rag_similarity_threshold=0.0,
        artifact_rag_chunk_size=3200,
        artifact_rag_chunk_overlap=400,
        artifact_rag_token_budget=1200,
        video_transcription_enabled=False,
        video_transcription_backend="none",
    )

    derived_artifact_ids: list[str] = []

    class _MotetStub:
        def __init__(self) -> None:
            self.tenant_id = tenant_id
            self.principal_id = principal_id
            self.motet_id = motet_id
            self.conversation_id = conversation_id
            self.task_id = f"task-{run_id}"
            self.command_id = f"command-{run_id}"
            self.stack = SimpleNamespace(config=cfg)
            self.artifact_store = scoped_store
            self._worker_context = {"embedding_service": embedding_service}

        def resolve_conversation_id(self, requested: str | None = None) -> str:
            return requested or self.conversation_id

        def log_fields(self, **extra: Any) -> dict[str, Any]:
            return {
                "tenant_id": self.tenant_id,
                "principal_id": self.principal_id,
                "motet_id": self.motet_id,
                "conversation_id": self.conversation_id,
                **extra,
            }

        def dispatch(self, commands: list[Any]) -> list[str]:
            task_ids: list[str] = []
            for child in commands:
                command_type = child.get_command_type()
                if command_type.endswith("derive_video_visuals"):
                    result = _wrapped(derivation_commands.derive_video_visuals)(child.data)
                    derivations = (result.get("derivations") or {}) if isinstance(result, dict) else {}
                    poster = (derivations.get("poster") or {}).get("id")
                    if poster:
                        derived_artifact_ids.append(str(poster))
                    for item in derivations.get("keyframes") or []:
                        kid = (item or {}).get("id")
                        if kid:
                            derived_artifact_ids.append(str(kid))
                elif command_type.endswith("derive_video_transcript"):
                    result = _wrapped(derivation_commands.derive_video_transcript)(child.data)
                    derivations = (result.get("derivations") or {}) if isinstance(result, dict) else {}
                    transcript = (derivations.get("transcript") or {}) or {}
                    if transcript.get("id"):
                        derived_artifact_ids.append(str(transcript["id"]))
                elif command_type.endswith("prepare_artifact_index"):
                    _wrapped(rag_commands.prepare_artifact_index)(child.data)
                task_ids.append(f"task-{command_type}-{len(task_ids)}")
            return task_ids

    motet = _MotetStub()
    monkeypatch.setattr(artifact_commands, "get_motet_context", lambda: motet)
    monkeypatch.setattr(derivation_commands, "get_motet_context", lambda: motet)
    monkeypatch.setattr(rag_commands, "get_motet_context", lambda: motet)
    monkeypatch.setattr("motet.core.media.video_processing.get_artifact_store", lambda: raw_store)

    source_id = ""
    video_bytes = _generate_fixture_video_bytes()
    try:
        create_result = _wrapped(artifact_commands.create_artifact)(
            CreateArtifactData(
                payload=video_bytes,
                content_type="video/mp4",
                kind=str(ArtifactKind.USER_UPLOAD.value),
                filename="fixture-walkthrough.mp4",
                conversation_id=conversation_id,
                trigger_derivations=True,
            )
        )
        source_id = str(create_result.get("artifact_id") or "")
        assert source_id

        posters = scoped_store.list(
            kind=ArtifactKind.DERIVED_VIDEO_POSTER,
            source_artifact_id=source_id,
            limit=5,
        )
        keyframes = scoped_store.list(
            kind=ArtifactKind.DERIVED_VIDEO_KEYFRAME,
            source_artifact_id=source_id,
            limit=60,
        )
        assert posters, "expected DERIVED_VIDEO_POSTER from derive_video_visuals"
        assert keyframes, "expected DERIVED_VIDEO_KEYFRAME artifacts from derive_video_visuals"

        index_result = _wrapped(rag_commands.prepare_artifact_index)(
            PrepareArtifactIndexData(source_artifact_id=source_id, force_reindex=True)
        )
        assert index_result.get("strategy_id") == "video_default"
        assert int(index_result.get("chunks_indexed") or 0) >= 1

        retrieve_result = _wrapped(rag_commands.rag_retrieve_context)(
            RagRetrieveContextData(
                query_text="video scene keyframe",
                scope="conversation",
                conversation_id=conversation_id,
                role="user",
                top_k=5,
                token_budget=1200,
            )
        )
        assert int(retrieve_result.get("chunk_count") or 0) >= 1
        assert retrieve_result["chunks"][0]["source_artifact_id"] == source_id
        assert "keyframe" in retrieve_result["context_text"].lower()
    finally:
        for artifact_id in reversed(derived_artifact_ids):
            scoped_store.delete(artifact_id)
        if source_id:
            for kind in (
                ArtifactKind.DERIVED_VIDEO_POSTER,
                ArtifactKind.DERIVED_VIDEO_KEYFRAME,
                ArtifactKind.DERIVED_VIDEO_TRANSCRIPT,
            ):
                for meta in scoped_store.list(kind=kind, source_artifact_id=source_id, limit=60):
                    scoped_store.delete(meta.id)
            scoped_store.delete(source_id)
            repository.delete_source_chunks(tenant_id=tenant_id, source_artifact_id=source_id)
        try:
            repository._redis.execute_command("FT.DROPINDEX", repository.index_name(tenant_id), "DD")
        except Exception:
            pass
        time.sleep(0.01)
