"""
Motet - Artifact API Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    HTTP-level tests for artifacts API routes (uploads/listing and ADR-0110
    preparation-aware indexing status/reindex) using TestClient and mocked store/config.

Dependencies:
    - pytest, fastapi.testclient
    - motet.interfaces.http.create_app
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

from motet.core.commands.command_data_classes import PrepareArtifactIndexData

from motet.core.artifacts import ArtifactKind, ArtifactMetadata


def _artifact_meta(
    *,
    id_: str,
    kind: ArtifactKind,
    source_artifact_id: str | None = None,
    metadata: dict | None = None,
    content_type: str = "text/plain",
    created_at: float = 1234567890.0,
) -> ArtifactMetadata:
    return ArtifactMetadata(
        id=id_,
        kind=kind,
        content_type=content_type,
        bytes=10,
        checksum_sha256="abc",
        created_at=created_at,
        tenant_id="test-tenant",
        source_artifact_id=source_artifact_id,
        metadata=metadata or {},
    )


def _patch_artifact_rag_config(enabled: bool):
    cfg = MagicMock()
    cfg.artifact_rag_enabled = enabled
    return patch("motet.core.config.Config", return_value=cfg)


def _patch_chunk_repo_count(count: int):
    return patch(
        "motet.core.rag.repository.ArtifactChunkRepository",
        return_value=MagicMock(count_source_chunks_by_strategy=MagicMock(return_value={"text_default": count} if count else {})),
    )


def _patch_chunk_repo_error(error: Exception):
    return patch(
        "motet.core.rag.repository.ArtifactChunkRepository",
        return_value=MagicMock(count_source_chunks_by_strategy=MagicMock(side_effect=error)),
    )

MOCK_PRINCIPAL_HEADERS = {
    "X-Principal-Id": "test-principal",
    "X-Tenant-Id": "test-tenant",
    "X-Motet-Id": "test-motet"
}

MOCK_ADMIN_HEADERS = {
    **MOCK_PRINCIPAL_HEADERS,
    "X-Roles": "motet-admin",
}

@pytest.fixture
def art_client(monkeypatch):
    monkeypatch.setenv("MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS", "true")
    from motet.interfaces.http import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c

@pytest.fixture
def mock_store():
    with patch("motet.interfaces.api.v1.artifacts.get_artifact_store") as mock:
        store = MagicMock()
        mock.return_value = store
        yield store

def test_upload_artifact(art_client, mock_store):
    mock_store.put.return_value = "art-123"
    
    file_content = b"test content"
    files = {"file": ("test.txt", file_content, "text/plain")}
    
    with patch("motet.core.workers.global_invoker") as mock_invoker:
        mock_invoker.execute_command.return_value = {
            "status": "completed",
            "result": {
                "status": "success",
                "data": {
                    "artifact_id": "art-123",
                    "kind": "user_upload",
                    "content_type": "text/plain",
                    "bytes": len(file_content),
                }
            }
        }
        
        response = art_client.post(
            "/api/v1/artifacts",
            headers=MOCK_PRINCIPAL_HEADERS,
            files=files,
            params={"kind": "user_upload"}
        )
    
    assert response.status_code == 200
    data = response.json()
    assert data["artifact_id"] == "art-123"
    assert data["kind"] == "user_upload"

def test_list_artifacts(art_client, mock_store):
    from motet.core.artifacts import ArtifactMetadata
    
    mock_store.list.return_value = [
        ArtifactMetadata(
            id="art-1",
            kind=ArtifactKind.USER_UPLOAD,
            content_type="text/plain",
            bytes=100,
            checksum_sha256="abc",
            created_at=1234567890.0,
            tenant_id="test-tenant"
        )
    ]
    
    response = art_client.get(
        "/api/v1/artifacts",
        headers=MOCK_PRINCIPAL_HEADERS,
        params={"limit": 10}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == "art-1"
    
    mock_store.list.assert_called_once()
    assert mock_store.list.call_args.kwargs["tenant_id"] == "test-tenant"
    assert mock_store.list.call_args.kwargs["principal_id"] == "test-principal"
    assert mock_store.list.call_args.kwargs["motet_id"] == "test-motet"

def test_list_artifacts_admin_scope_override(art_client, mock_store):
    mock_store.list.return_value = []

    response = art_client.get(
        "/api/v1/artifacts",
        headers=MOCK_ADMIN_HEADERS,
        params={"tenant_id": "selected-tenant", "motet_id": "selected-motet"}
    )

    assert response.status_code == 200
    mock_store.list.assert_called_once()
    assert mock_store.list.call_args.kwargs["tenant_id"] == "selected-tenant"
    assert mock_store.list.call_args.kwargs["principal_id"] is None
    assert mock_store.list.call_args.kwargs["motet_id"] == "selected-motet"

def test_list_artifacts_scope_override_requires_admin(art_client, mock_store):
    response = art_client.get(
        "/api/v1/artifacts",
        headers=MOCK_PRINCIPAL_HEADERS,
        params={"tenant_id": "selected-tenant"}
    )

    assert response.status_code == 403
    mock_store.list.assert_not_called()


def test_delete_all_artifacts(art_client, mock_store):
    mock_store.list.side_effect = [
        [
            _artifact_meta(id_="art-1", kind=ArtifactKind.USER_UPLOAD),
            _artifact_meta(id_="art-2", kind=ArtifactKind.DERIVED_TEXT),
        ],
        [],
    ]
    mock_store.delete.return_value = True

    response = art_client.delete(
        "/api/v1/artifacts",
        headers=MOCK_PRINCIPAL_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "deleted", "deleted_count": 2, "failed_count": 0}
    assert mock_store.delete.call_count == 2
    mock_store.delete.assert_any_call(
        artifact_id="art-1",
        tenant_id="test-tenant",
        principal_id="test-principal",
        motet_id="test-motet",
    )
    mock_store.delete.assert_any_call(
        artifact_id="art-2",
        tenant_id="test-tenant",
        principal_id="test-principal",
        motet_id="test-motet",
    )


def test_delete_all_artifacts_admin_scope_override(art_client, mock_store):
    mock_store.list.side_effect = [
        [_artifact_meta(id_="art-1", kind=ArtifactKind.USER_UPLOAD)],
        [],
    ]
    mock_store.delete.return_value = True

    response = art_client.delete(
        "/api/v1/artifacts",
        headers=MOCK_ADMIN_HEADERS,
        params={"tenant_id": "selected-tenant", "motet_id": "selected-motet"},
    )

    assert response.status_code == 200
    mock_store.delete.assert_called_once_with(
        artifact_id="art-1",
        tenant_id="selected-tenant",
        principal_id=None,
        motet_id="selected-motet",
    )


def test_delete_all_artifacts_scope_override_requires_admin(art_client, mock_store):
    response = art_client.delete(
        "/api/v1/artifacts",
        headers=MOCK_PRINCIPAL_HEADERS,
        params={"tenant_id": "selected-tenant"},
    )

    assert response.status_code == 403
    mock_store.list.assert_not_called()
    mock_store.delete.assert_not_called()


def test_list_artifacts_filters_by_source_artifact_id_and_kind(art_client, mock_store):
    mock_store.list.return_value = [
        _artifact_meta(
            id_="poster-1",
            kind=ArtifactKind.DERIVED_VIDEO_POSTER,
            source_artifact_id="video-source-1",
            content_type="image/jpeg",
        )
    ]

    response = art_client.get(
        "/api/v1/artifacts",
        headers=MOCK_PRINCIPAL_HEADERS,
        params={
            "source_artifact_id": "video-source-1",
            "kind": ArtifactKind.DERIVED_VIDEO_POSTER.value,
            "limit": 1,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == "poster-1"
    assert data["items"][0]["source_artifact_id"] == "video-source-1"

    mock_store.list.assert_called_once()
    assert mock_store.list.call_args.kwargs["source_artifact_id"] == "video-source-1"
    assert mock_store.list.call_args.kwargs["kind"] == ArtifactKind.DERIVED_VIDEO_POSTER


def test_list_artifacts_drops_mismatched_source_rows(art_client, mock_store):
    mock_store.list.return_value = [
        _artifact_meta(
            id_="poster-wrong",
            kind=ArtifactKind.DERIVED_VIDEO_POSTER,
            source_artifact_id="other-video",
            content_type="image/jpeg",
        ),
        _artifact_meta(
            id_="poster-1",
            kind=ArtifactKind.DERIVED_VIDEO_POSTER,
            source_artifact_id="video-source-1",
            content_type="image/jpeg",
        ),
    ]

    response = art_client.get(
        "/api/v1/artifacts",
        headers=MOCK_PRINCIPAL_HEADERS,
        params={
            "source_artifact_id": "video-source-1",
            "kind": ArtifactKind.DERIVED_VIDEO_POSTER.value,
            "limit": 5,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert [item["id"] for item in data["items"]] == ["poster-1"]


def test_artifact_metadata_admin_scope_override(art_client, mock_store):
    from motet.core.artifacts import ArtifactMetadata

    mock_store.get_metadata.return_value = ArtifactMetadata(
        id="art-1",
        kind=ArtifactKind.USER_UPLOAD,
        content_type="text/plain",
        bytes=100,
        checksum_sha256="abc",
        created_at=1234567890.0,
        tenant_id="selected-tenant",
        principal_id="service-account:memo-local-dev",
        motet_id="selected-motet",
    )

    response = art_client.get(
        "/api/v1/artifacts/art-1/metadata",
        headers=MOCK_ADMIN_HEADERS,
        params={"tenant_id": "selected-tenant", "motet_id": "selected-motet"}
    )

    assert response.status_code == 200
    assert response.json()["principal_id"] == "service-account:memo-local-dev"
    mock_store.get_metadata.assert_called_once()
    assert mock_store.get_metadata.call_args.kwargs["tenant_id"] == "selected-tenant"
    assert mock_store.get_metadata.call_args.kwargs["principal_id"] is None
    assert mock_store.get_metadata.call_args.kwargs["motet_id"] == "selected-motet"


def test_update_artifact_metadata_merges_artifact_tags(art_client, mock_store):
    existing = _artifact_meta(
        id_="art-1",
        kind=ArtifactKind.USER_UPLOAD,
        metadata={"artifact_tags": ["signed", "jersey"], "filename": "image.png"},
    )
    updated = _artifact_meta(
        id_="art-1",
        kind=ArtifactKind.USER_UPLOAD,
        metadata={
            "artifact_tags": ["signed", "jersey", "game-used"],
            "filename": "image.png",
            "memo_asset_id": "draft_123",
        },
    )
    mock_store.get_metadata.return_value = existing
    mock_store.update_metadata.return_value = updated

    response = art_client.patch(
        "/api/v1/artifacts/art-1/metadata",
        headers=MOCK_PRINCIPAL_HEADERS,
        json={
            "metadata": {"memo_asset_id": "draft_123"},
            "artifact_tags": ["game-used", "jersey", ""],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["metadata"]["artifact_tags"] == ["signed", "jersey", "game-used"]
    assert body["metadata"]["memo_asset_id"] == "draft_123"
    mock_store.get_metadata.assert_called_once()
    assert mock_store.get_metadata.call_args.kwargs["tenant_id"] == "test-tenant"
    assert mock_store.get_metadata.call_args.kwargs["principal_id"] == "test-principal"
    assert mock_store.get_metadata.call_args.kwargs["motet_id"] == "test-motet"
    mock_store.update_metadata.assert_called_once()
    assert mock_store.update_metadata.call_args.args[0] == "art-1"
    assert mock_store.update_metadata.call_args.args[1] == {
        "memo_asset_id": "draft_123",
        "artifact_tags": ["signed", "jersey", "game-used"],
    }


def test_update_artifact_metadata_can_replace_artifact_tags(art_client, mock_store):
    existing = _artifact_meta(
        id_="art-1",
        kind=ArtifactKind.USER_UPLOAD,
        metadata={"artifact_tags": ["signed", "jersey"]},
    )
    updated = _artifact_meta(
        id_="art-1",
        kind=ArtifactKind.USER_UPLOAD,
        metadata={"artifact_tags": ["photo"]},
    )
    mock_store.get_metadata.return_value = existing
    mock_store.update_metadata.return_value = updated

    response = art_client.patch(
        "/api/v1/artifacts/art-1/metadata",
        headers=MOCK_PRINCIPAL_HEADERS,
        json={"artifact_tags": ["photo"], "merge_artifact_tags": False},
    )

    assert response.status_code == 200
    assert response.json()["metadata"]["artifact_tags"] == ["photo"]
    assert mock_store.update_metadata.call_args.args[1] == {"artifact_tags": ["photo"]}


def test_update_artifact_metadata_returns_404_when_missing(art_client, mock_store):
    mock_store.get_metadata.return_value = None

    response = art_client.patch(
        "/api/v1/artifacts/missing/metadata",
        headers=MOCK_PRINCIPAL_HEADERS,
        json={"artifact_tags": ["jersey"]},
    )

    assert response.status_code == 404
    mock_store.update_metadata.assert_not_called()


def test_download_artifact(art_client, mock_store):
    from motet.core.artifacts import ArtifactMetadata
    
    mock_store.get_metadata.return_value = ArtifactMetadata(
        id="art-1",
        kind=ArtifactKind.USER_UPLOAD,
        content_type="text/plain",
        bytes=5,
        checksum_sha256="abc",
        created_at=1234567890.0,
        metadata={"filename": "test.txt"}
    )
    mock_store.get.return_value = b"hello"
    
    response = art_client.get(
        "/api/v1/artifacts/art-1/download",
        headers=MOCK_PRINCIPAL_HEADERS
    )
    
    assert response.status_code == 200
    assert response.content == b"hello"
    assert response.headers["content-disposition"] == 'attachment; filename="test.txt"'
    assert response.headers.get("accept-ranges") == "bytes"


def test_download_artifact_range_returns_206(art_client, mock_store):
    mock_store.get_metadata.return_value = ArtifactMetadata(
        id="art-1",
        kind=ArtifactKind.USER_UPLOAD,
        content_type="video/mp4",
        bytes=10,
        checksum_sha256="abc",
        created_at=1234567890.0,
        metadata={"filename": "clip.mp4"},
    )
    mock_store.get_range.return_value = b"2345"

    response = art_client.get(
        "/api/v1/artifacts/art-1/download",
        headers={**MOCK_PRINCIPAL_HEADERS, "Range": "bytes=2-5"},
    )

    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers.get("accept-ranges") == "bytes"
    assert response.headers.get("content-range") == "bytes 2-5/10"
    mock_store.get_range.assert_called_once()


def _video_meta(artifact_id: str = "art-1") -> ArtifactMetadata:
    return ArtifactMetadata(
        id=artifact_id,
        kind=ArtifactKind.USER_UPLOAD,
        content_type="video/mp4",
        bytes=10,
        checksum_sha256="abc",
        created_at=1234567890.0,
        tenant_id="test-tenant",
        metadata={"filename": "clip.mp4"},
    )


def test_playback_token_mint_and_stream(art_client, mock_store):
    """ADR-0118 Phase A.2: mint a playback token, then stream with it (no auth headers)."""
    mock_store.get_metadata.return_value = _video_meta()
    mock_store.get.return_value = b"0123456789"

    mint = art_client.post(
        "/api/v1/artifacts/art-1/playback-token",
        headers=MOCK_PRINCIPAL_HEADERS,
    )
    assert mint.status_code == 200
    body = mint.json()
    assert body["artifact_id"] == "art-1"
    assert body["expires_in"] > 0
    assert body["stream_url"].startswith("/api/v1/artifacts/art-1/stream?token=")

    # Stream WITHOUT principal headers — the token is the credential.
    full = art_client.get(body["stream_url"])
    assert full.status_code == 200
    assert full.content == b"0123456789"
    assert full.headers["content-disposition"] == 'inline; filename="clip.mp4"'
    assert full.headers.get("accept-ranges") == "bytes"


def test_playback_token_stream_honors_range(art_client, mock_store):
    mock_store.get_metadata.return_value = _video_meta()
    mock_store.get_range.return_value = b"2345"

    mint = art_client.post(
        "/api/v1/artifacts/art-1/playback-token",
        headers=MOCK_PRINCIPAL_HEADERS,
    )
    assert mint.status_code == 200

    response = art_client.get(
        mint.json()["stream_url"],
        headers={"Range": "bytes=2-5"},
    )
    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers.get("content-range") == "bytes 2-5/10"
    mock_store.get_range.assert_called_once()


def test_playback_token_requires_accessible_artifact(art_client, mock_store):
    mock_store.get_metadata.return_value = None
    response = art_client.post(
        "/api/v1/artifacts/missing/playback-token",
        headers=MOCK_PRINCIPAL_HEADERS,
    )
    assert response.status_code == 404


def test_stream_rejects_invalid_token(art_client, mock_store):
    response = art_client.get("/api/v1/artifacts/art-1/stream", params={"token": "garbage"})
    assert response.status_code == 401
    mock_store.get.assert_not_called()


def test_stream_rejects_token_for_other_artifact(art_client, mock_store):
    mock_store.get_metadata.return_value = _video_meta()
    mint = art_client.post(
        "/api/v1/artifacts/art-1/playback-token",
        headers=MOCK_PRINCIPAL_HEADERS,
    )
    token = mint.json()["token"]

    response = art_client.get("/api/v1/artifacts/art-2/stream", params={"token": token})
    assert response.status_code == 401
    mock_store.get.assert_not_called()


def test_upload_video_rejects_oversized_payload(art_client, mock_store):
    oversized = b"x" * 100
    files = {"file": ("clip.mp4", oversized, "video/mp4")}

    with patch("motet.core.config.Config") as mock_config:
        cfg = MagicMock()
        cfg.artifact_max_video_bytes = 50
        mock_config.return_value = cfg
        response = art_client.post(
            "/api/v1/artifacts",
            headers=MOCK_PRINCIPAL_HEADERS,
            files=files,
            params={"kind": "user_upload"},
        )

    assert response.status_code == 413
    mock_store.put.assert_not_called()


def test_indexing_status_rejects_more_than_eighty_ids(art_client, mock_store):
    params = [("artifact_id", str(i)) for i in range(81)]
    response = art_client.get(
        "/api/v1/artifacts/indexing-status",
        headers=MOCK_PRINCIPAL_HEADERS,
        params=params,
    )
    assert response.status_code == 400
    assert "80" in response.json()["detail"]
    mock_store.get_metadata.assert_not_called()


def test_indexing_status_requires_admin_for_scope_override(art_client, mock_store):
    response = art_client.get(
        "/api/v1/artifacts/indexing-status",
        headers=MOCK_PRINCIPAL_HEADERS,
        params={"artifact_id": "a1", "tenant_id": "other-tenant"},
    )
    assert response.status_code == 403
    mock_store.get_metadata.assert_not_called()


def test_indexing_status_empty_query_returns_empty_items(art_client, mock_store):
    with _patch_artifact_rag_config(True):
        response = art_client.get(
            "/api/v1/artifacts/indexing-status",
            headers=MOCK_PRINCIPAL_HEADERS,
        )
    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_indexing_status_skips_unknown_artifact_ids(art_client, mock_store):
    mock_store.get_metadata.return_value = None
    with _patch_artifact_rag_config(True):
        response = art_client.get(
            "/api/v1/artifacts/indexing-status",
            headers=MOCK_PRINCIPAL_HEADERS,
            params={"artifact_id": "missing"},
        )
    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_indexing_status_deduplicates_ids(art_client, mock_store):
    src = _artifact_meta(id_="src-1", kind=ArtifactKind.USER_UPLOAD)
    derived = _artifact_meta(
        id_="der-1",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id="src-1",
    )

    def _gm(aid, **kwargs):
        if aid == "src-1":
            return src
        return None

    mock_store.get_metadata.side_effect = _gm
    mock_store.list.return_value = [derived]

    with _patch_artifact_rag_config(True), _patch_chunk_repo_count(2):
        response = art_client.get(
            "/api/v1/artifacts/indexing-status",
            headers=MOCK_PRINCIPAL_HEADERS,
            params=[("artifact_id", "src-1"), ("artifact_id", "src-1")],
        )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["artifact_id"] == "src-1"
    assert items[0]["summary"] == "indexed"
    assert items[0]["total_chunks_indexed"] == 2


def test_indexing_status_chooses_newest_derived_text_deterministically(art_client, mock_store):
    src = _artifact_meta(id_="src-1", kind=ArtifactKind.USER_UPLOAD)
    old_derived = _artifact_meta(
        id_="der-old",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id="src-1",
        created_at=100.0,
    )
    new_derived = _artifact_meta(
        id_="der-new",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id="src-1",
        created_at=200.0,
    )
    mock_store.get_metadata.return_value = src
    mock_store.list.return_value = [old_derived, new_derived]

    with _patch_artifact_rag_config(True), _patch_chunk_repo_count(0):
        response = art_client.get(
            "/api/v1/artifacts/indexing-status",
            headers=MOCK_PRINCIPAL_HEADERS,
            params={"artifact_id": "src-1"},
        )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["derived_sets"] == []
    assert item["summary"] == "ready_not_indexed"


def test_indexing_status_prefers_explicit_derived_text_metadata(art_client, mock_store):
    src = _artifact_meta(
        id_="src-1",
        kind=ArtifactKind.USER_UPLOAD,
        metadata={"derived_artifact_ids": {"derived_text": "der-preferred"}},
    )
    preferred = _artifact_meta(
        id_="der-preferred",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id="src-1",
        created_at=100.0,
    )
    newer = _artifact_meta(
        id_="der-newer",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id="src-1",
        created_at=200.0,
    )
    mock_store.get_metadata.return_value = src
    mock_store.list.return_value = [newer, preferred]

    with _patch_artifact_rag_config(True), _patch_chunk_repo_count(1):
        response = art_client.get(
            "/api/v1/artifacts/indexing-status",
            headers=MOCK_PRINCIPAL_HEADERS,
            params={"artifact_id": "src-1"},
        )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["derived_sets"][0]["derived_artifact_ids"] == ["der-preferred"]
    assert item["summary"] == "indexed"


def test_indexing_status_reports_index_unavailable_on_chunk_count_error(art_client, mock_store):
    src = _artifact_meta(id_="src-1", kind=ArtifactKind.USER_UPLOAD)
    derived = _artifact_meta(
        id_="der-1",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id="src-1",
    )
    mock_store.get_metadata.return_value = src
    mock_store.list.return_value = [derived]

    with _patch_artifact_rag_config(True), _patch_chunk_repo_error(RuntimeError("scan failed")):
        response = art_client.get(
            "/api/v1/artifacts/indexing-status",
            headers=MOCK_PRINCIPAL_HEADERS,
            params={"artifact_id": "src-1"},
        )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["summary"] == "index_unavailable"
    assert item["total_chunks_indexed"] == 0
    assert "scan failed" in item["detail"]


def test_indexing_status_user_upload_ready_when_not_indexed(art_client, mock_store):
    src = _artifact_meta(id_="src-1", kind=ArtifactKind.USER_UPLOAD)
    mock_store.get_metadata.return_value = src
    mock_store.list.return_value = []

    with _patch_artifact_rag_config(True), _patch_chunk_repo_count(0):
        response = art_client.get(
            "/api/v1/artifacts/indexing-status",
            headers=MOCK_PRINCIPAL_HEADERS,
            params={"artifact_id": "src-1"},
        )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["summary"] == "ready_not_indexed"


def test_indexing_status_reports_prep_running_from_metadata(art_client, mock_store):
    src = _artifact_meta(
        id_="src-1",
        kind=ArtifactKind.USER_UPLOAD,
        metadata={
            "prep_strategy_versions": {"text_default": "1.0.0"},
            "prep_state_by_strategy": {"text_default": "prep_running"},
        },
    )
    mock_store.get_metadata.return_value = src
    mock_store.list.return_value = []

    with _patch_artifact_rag_config(True), _patch_chunk_repo_count(0):
        response = art_client.get(
            "/api/v1/artifacts/indexing-status",
            headers=MOCK_PRINCIPAL_HEADERS,
            params={"artifact_id": "src-1"},
        )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["summary"] == "prep_running"
    assert item["derived_sets"][0]["strategy_id"] == "text_default"
    assert item["derived_sets"][0]["chunks_indexed"] == 0


def test_reindex_returns_503_when_feature_disabled(art_client, mock_store):
    with _patch_artifact_rag_config(False):
        response = art_client.post(
            "/api/v1/artifacts/src-1/reindex",
            headers=MOCK_PRINCIPAL_HEADERS,
        )
    assert response.status_code == 503
    mock_store.get_metadata.assert_not_called()


def test_reindex_returns_404_when_missing(art_client, mock_store):
    mock_store.get_metadata.return_value = None
    with _patch_artifact_rag_config(True):
        response = art_client.post(
            "/api/v1/artifacts/nope/reindex",
            headers=MOCK_PRINCIPAL_HEADERS,
        )
    assert response.status_code == 404


def test_reindex_returns_400_for_unsupported_kind(art_client, mock_store):
    meta = _artifact_meta(id_="ocr-1", kind=ArtifactKind.DERIVED_OCR)
    mock_store.get_metadata.return_value = meta
    with _patch_artifact_rag_config(True):
        response = art_client.post(
            "/api/v1/artifacts/ocr-1/reindex",
            headers=MOCK_PRINCIPAL_HEADERS,
        )
    assert response.status_code == 400
    assert "cannot be prepared/indexed" in response.json()["detail"].lower()


def test_reindex_queues_source_when_no_derived_artifact(art_client, mock_store):
    src = _artifact_meta(id_="src-1", kind=ArtifactKind.USER_UPLOAD)
    mock_store.get_metadata.return_value = src
    mock_store.list.return_value = []

    fake_reg = MagicMock()
    fake_reg.data_class = PrepareArtifactIndexData
    fake_reg.implementation = lambda **kwargs: MagicMock(name="constructed_command")

    with (
        _patch_artifact_rag_config(True),
        patch(
            "motet.core.commands.command_type_registry.command_type_registry.get",
            return_value=fake_reg,
        ),
        patch("motet.interfaces.api.v1.artifacts.asyncio.create_task") as create_task,
    ):
        create_task.side_effect = lambda coro: coro.close()
        response = art_client.post(
            "/api/v1/artifacts/src-1/reindex",
            headers=MOCK_PRINCIPAL_HEADERS,
        )
    assert response.status_code == 200
    assert response.json()["derived_artifact_ids"] == []


def test_reindex_returns_500_when_command_missing(art_client, mock_store):
    src = _artifact_meta(id_="src-1", kind=ArtifactKind.USER_UPLOAD)
    derived = _artifact_meta(
        id_="der-1",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id="src-1",
    )
    mock_store.get_metadata.return_value = src
    mock_store.list.return_value = [derived]

    fake_reg = MagicMock()
    fake_reg.data_class = None
    fake_reg.implementation = MagicMock()

    with (
        _patch_artifact_rag_config(True),
        patch(
            "motet.core.commands.command_type_registry.command_type_registry.get",
            return_value=fake_reg,
        ),
    ):
        response = art_client.post(
            "/api/v1/artifacts/src-1/reindex",
            headers=MOCK_PRINCIPAL_HEADERS,
        )
    assert response.status_code == 500


def test_reindex_success(art_client, mock_store):
    src = _artifact_meta(
        id_="src-1",
        kind=ArtifactKind.USER_UPLOAD,
        metadata={"conversation_id": "conv-9"},
    )
    derived = _artifact_meta(
        id_="der-1",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id="src-1",
    )

    mock_store.get_metadata.return_value = src
    mock_store.list.return_value = [derived]

    fake_reg = MagicMock()
    fake_reg.data_class = PrepareArtifactIndexData
    fake_reg.implementation = lambda **kwargs: MagicMock(name="constructed_command")

    exec_result = {"status": "success", "data": {"total_chunks_indexed": 3, "cache_hit": True}}

    with (
        _patch_artifact_rag_config(True),
        patch(
            "motet.core.commands.command_type_registry.command_type_registry.get",
            return_value=fake_reg,
        ),
        patch(
            "motet.core.workers.global_invoker.execute_command",
            return_value=exec_result,
        ),
    ):
        response = art_client.post(
            "/api/v1/artifacts/src-1/reindex",
            headers=MOCK_PRINCIPAL_HEADERS,
            params={"wait": "true"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["command_type"] == "core.prepare_artifact_index"
    assert body["status"] == "success"
    assert body["cache_hit"] is True
    assert body["result"] == exec_result
    assert "task_id" in body and body["task_id"]


def test_reindex_uses_newest_derived_text(art_client, mock_store):
    src = _artifact_meta(id_="src-1", kind=ArtifactKind.USER_UPLOAD)
    old_derived = _artifact_meta(
        id_="der-old",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id="src-1",
        created_at=100.0,
    )
    new_derived = _artifact_meta(
        id_="der-new",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id="src-1",
        created_at=200.0,
    )
    mock_store.get_metadata.return_value = src
    mock_store.list.return_value = [old_derived, new_derived]

    captured_data = []

    def _fake_impl(**kwargs):
        captured_data.append(kwargs["data"])
        return MagicMock(name="constructed_command")

    fake_reg = MagicMock()
    fake_reg.data_class = PrepareArtifactIndexData
    fake_reg.implementation = _fake_impl

    with (
        _patch_artifact_rag_config(True),
        patch(
            "motet.core.commands.command_type_registry.command_type_registry.get",
            return_value=fake_reg,
        ),
        patch(
            "motet.core.workers.global_invoker.execute_command",
            return_value={"status": "success", "data": {"total_chunks_indexed": 1}},
        ),
    ):
        response = art_client.post(
            "/api/v1/artifacts/src-1/reindex",
            headers=MOCK_PRINCIPAL_HEADERS,
            params={"wait": "true"},
        )

    assert response.status_code == 200
    assert captured_data[0].derived_artifact_id == "der-new"


def test_reindex_docx_passes_resolved_derived_to_prepare_command(art_client, mock_store):
    """Reindex resolves newest derived text id; prepare_artifact_index routes to source DOCX."""

    src = _artifact_meta(
        id_="src-1",
        kind=ArtifactKind.USER_UPLOAD,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        metadata={"filename": "structured-plan.docx"},
    )
    derived = _artifact_meta(
        id_="der-new",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id="src-1",
        created_at=200.0,
    )
    mock_store.get_metadata.return_value = src
    mock_store.list.return_value = [derived]

    captured_data = []

    def _fake_impl(**kwargs):
        captured_data.append(kwargs["data"])
        return MagicMock(name="constructed_command")

    fake_reg = MagicMock()
    fake_reg.data_class = PrepareArtifactIndexData
    fake_reg.implementation = _fake_impl

    with (
        _patch_artifact_rag_config(True),
        patch(
            "motet.core.commands.command_type_registry.command_type_registry.get",
            return_value=fake_reg,
        ),
        patch(
            "motet.core.workers.global_invoker.execute_command",
            return_value={"status": "success", "data": {"total_chunks_indexed": 1}},
        ),
    ):
        response = art_client.post(
            "/api/v1/artifacts/src-1/reindex",
            headers=MOCK_PRINCIPAL_HEADERS,
            params={"wait": "true"},
        )

    assert response.status_code == 200
    assert captured_data[0].source_artifact_id == "src-1"
    assert captured_data[0].derived_artifact_id == "der-new"


def test_reindex_defaults_to_queued_background_job(art_client, mock_store):
    src = _artifact_meta(id_="src-1", kind=ArtifactKind.USER_UPLOAD)
    derived = _artifact_meta(
        id_="der-1",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id="src-1",
    )
    mock_store.get_metadata.return_value = src
    mock_store.list.return_value = [derived]

    fake_reg = MagicMock()
    fake_reg.data_class = PrepareArtifactIndexData
    fake_reg.implementation = lambda **kwargs: MagicMock(name="constructed_command")

    with (
        _patch_artifact_rag_config(True),
        patch(
            "motet.core.commands.command_type_registry.command_type_registry.get",
            return_value=fake_reg,
        ),
        patch("motet.interfaces.api.v1.artifacts.asyncio.create_task") as create_task,
    ):
        create_task.side_effect = lambda coro: coro.close()
        response = art_client.post(
            "/api/v1/artifacts/src-1/reindex",
            headers=MOCK_PRINCIPAL_HEADERS,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["result"] is None
    create_task.assert_called_once()


def test_reindex_returns_409_when_indexing_disabled(art_client, mock_store):
    src = _artifact_meta(
        id_="src-1",
        kind=ArtifactKind.USER_UPLOAD,
        metadata={"artifact_indexing_enabled": False},
    )
    derived = _artifact_meta(
        id_="der-1",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id="src-1",
    )
    mock_store.get_metadata.return_value = src
    mock_store.list.return_value = [derived]

    with _patch_artifact_rag_config(True):
        response = art_client.post(
            "/api/v1/artifacts/src-1/reindex",
            headers=MOCK_PRINCIPAL_HEADERS,
        )

    assert response.status_code == 409


def test_update_artifact_indexing_policy_updates_source_metadata(art_client, mock_store):
    src = _artifact_meta(id_="src-1", kind=ArtifactKind.USER_UPLOAD)
    updated = _artifact_meta(
        id_="src-1",
        kind=ArtifactKind.USER_UPLOAD,
        metadata={"artifact_indexing_enabled": False, "indexing_enabled": False},
    )
    mock_store.get_metadata.return_value = src
    mock_store.list.return_value = []
    mock_store.update_metadata.return_value = updated

    with _patch_chunk_repo_count(0) as repo_cls:
        response = art_client.patch(
            "/api/v1/artifacts/src-1/indexing-policy",
            headers=MOCK_PRINCIPAL_HEADERS,
            json={"indexing_enabled": False},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["source_artifact_id"] == "src-1"
    assert body["indexing_enabled"] is False
    mock_store.update_metadata.assert_called_once()
    assert mock_store.update_metadata.call_args.args[0] == "src-1"
    assert mock_store.update_metadata.call_args.args[1]["artifact_indexing_enabled"] is False
    repo_cls.return_value.delete_source_chunks.assert_called_once()


def test_indexing_status_reports_disabled_policy(art_client, mock_store):
    src = _artifact_meta(
        id_="src-1",
        kind=ArtifactKind.USER_UPLOAD,
        metadata={"artifact_indexing_enabled": False},
    )
    derived = _artifact_meta(
        id_="der-1",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id="src-1",
    )
    mock_store.get_metadata.return_value = src
    mock_store.list.return_value = [derived]

    with _patch_artifact_rag_config(True), _patch_chunk_repo_count(2):
        response = art_client.get(
            "/api/v1/artifacts/indexing-status",
            headers=MOCK_PRINCIPAL_HEADERS,
            params={"artifact_id": "src-1"},
        )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["indexing_enabled"] is False
    assert item["summary"] == "indexing_disabled"
    assert item["total_chunks_indexed"] == 2
