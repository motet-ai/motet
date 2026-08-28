"""
Motet - Artifacts API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    API endpoints for managing artifacts (uploads, downloads, listing).
    Implements (Tool Artifacts) and (User Uploads).
    preparation-aware chunk indexing status (GET /indexing-status)
    and reindex (POST /{id}/reindex).
    HTTP Range delivery, playback tokens (POST /{id}/playback-token),
    and token-authenticated inline streaming (GET /{id}/stream).

Dependencies:
    - fastapi: Web framework
    - motet.core.artifacts: Artifact store and types
    - motet.interfaces.api.shared.auth: Authentication
    - motet.core.rag: Valkey chunk counts for status

Usage:
    GET /api/v1/artifacts?kind=...&source_artifact_id=...
    POST /api/v1/artifacts
    GET /api/v1/artifacts/indexing-status
    GET /api/v1/artifacts/{id}/metadata
    GET /api/v1/artifacts/{id}/download
    GET /api/v1/artifacts/{id}/preview
    POST /api/v1/artifacts/{id}/playback-token
    GET /api/v1/artifacts/{id}/stream?token=...
    POST /api/v1/artifacts/{id}/reindex
    DELETE /api/v1/artifacts
    DELETE /api/v1/artifacts/{id}
"""

from typing import Any, Dict, List, Optional, Literal, cast
import asyncio
import uuid
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, UploadFile, File, Form, Response as FastAPIResponse
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ....core.artifacts import (
    get_artifact_store,
    ArtifactKind,
    ArtifactMetadata,
)
from ....core.artifacts.preparation import (
    ArtifactPayloadInfo,
    ArtifactPrepContext,
    ArtifactPrepHints,
    ArtifactPrepSelector,
    ArtifactPrepState,
    DerivedSetStatus,
)
from ....core.artifacts.protocol import ArtifactStoreProtocol
from ..shared.auth import get_current_principal, is_admin_principal
from ..shared.identity import get_principal_context
from ....core.types import Principal

logger = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/artifacts",
    tags=["artifacts"],
)

# --- Models ---

class ArtifactListResponse(BaseModel):
    """Response model for paginated artifact listing."""
    items: List[ArtifactMetadata] = Field(..., description="List of artifact metadata objects")
    total: Optional[int] = Field(default=None, description="Total number of matching artifacts (if known)")
    limit: int = Field(..., description="Maximum number of items returned")
    offset: int = Field(..., description="Number of items skipped")


class ArtifactBulkDeleteResponse(BaseModel):
    """Response model for scoped bulk artifact deletion."""

    status: Literal["deleted"] = Field(default="deleted", description="Bulk delete outcome")
    deleted_count: int = Field(..., ge=0, description="Number of artifacts successfully deleted")
    failed_count: int = Field(..., ge=0, description="Number of artifacts that could not be deleted")

class ArtifactUploadResponse(BaseModel):
    """Response model for a successful artifact upload."""
    artifact_id: str = Field(..., description="Unique identifier of the uploaded artifact")
    filename: str = Field(..., description="Original filename of the uploaded file")
    content_type: str = Field(..., description="MIME type of the uploaded file")
    bytes: int = Field(..., description="Size of the uploaded file in bytes")
    kind: ArtifactKind = Field(..., description="Classification of the artifact")


IndexingStatusSummary = Literal[
    "indexed",
    "ready_not_indexed",
    "awaiting_derivation",
    "prep_pending",
    "prep_running",
    "prep_partial",
    "prep_failed",
    "indexing_disabled",
    "index_unavailable",
    "unsupported_kind",
    "missing_source_link",
]


class ArtifactIndexingStatusItem(BaseModel):
    """Per-artifact preparation/indexing status for ops / management UI."""

    artifact_id: str = Field(..., description="Artifact this row describes")
    artifact_rag_globally_enabled: bool = Field(
        ...,
        description="Whether MOTET_ARTIFACT_RAG_ENABLED is on for this server (gates retrieval + indexing)",
    )
    source_artifact_id: Optional[str] = Field(default=None, description="Source artifact id used in chunk keys")
    derived_sets: List[DerivedSetStatus] = Field(default_factory=list, description="Prepared derived/indexed sets")
    indexing_enabled: bool = Field(True, description="Whether this artifact source is eligible for text chunk indexing")
    chunks_by_strategy: Dict[str, int] = Field(default_factory=dict, description="Indexed chunk counts by strategy")
    total_chunks_indexed: int = Field(0, ge=0, description="Total indexed chunk count for the source artifact")
    summary: IndexingStatusSummary = Field(..., description="Coarse status for display")
    detail: Optional[str] = Field(default=None, description="Optional human-readable explanation")


class ArtifactIndexingBulkStatusResponse(BaseModel):
    """Response for GET /api/v1/artifacts/indexing-status."""

    items: List[ArtifactIndexingStatusItem] = Field(
        default_factory=list,
        description="Status per requested id (404s omitted)",
    )


class ArtifactReindexResponse(BaseModel):
    """Response for POST /api/v1/artifacts/{artifact_id}/reindex."""

    command_type: str = Field(default="core.prepare_artifact_index", description="Command executed")
    task_id: str = Field(..., description="Task id for this execution")
    status: Literal["queued", "running", "success", "error"] = Field(..., description="Reindex task status")
    strategy_id: Optional[str] = Field(default=None, description="Preparation strategy selected or requested")
    strategy_version: Optional[str] = Field(default=None, description="Preparation strategy version")
    prep_decision_source: Optional[Literal["dispatch", "planner"]] = Field(default=None, description="Preparation decision source")
    cache_hit: Optional[bool] = Field(default=None, description="Whether reindex reused existing prepared chunks")
    derived_artifact_ids: List[str] = Field(default_factory=list, description="Derived/enrichment artifacts used or produced")
    result: Optional[Any] = Field(default=None, description="Command execution payload when complete")
    error: Optional[str] = Field(default=None, description="Error message when status is error")


class ArtifactReindexTaskStatusResponse(BaseModel):
    """Response for GET /api/v1/artifacts/reindex-tasks/{task_id}."""

    command_type: str = Field(default="core.prepare_artifact_index", description="Command executed")
    task_id: str = Field(..., description="Task id for this execution")
    artifact_id: str = Field(..., description="Requested artifact id")
    source_artifact_id: str = Field(..., description="Source artifact id")
    derived_artifact_ids: List[str] = Field(default_factory=list, description="Derived/enrichment artifacts used or produced")
    strategy_id: Optional[str] = Field(default=None, description="Preparation strategy selected or requested")
    strategy_version: Optional[str] = Field(default=None, description="Preparation strategy version")
    prep_decision_source: Optional[Literal["dispatch", "planner"]] = Field(default=None, description="Preparation decision source")
    cache_hit: Optional[bool] = Field(default=None, description="Whether reindex reused existing prepared chunks")
    status: Literal["queued", "running", "success", "error"] = Field(..., description="Task status")
    result: Optional[Any] = Field(default=None, description="Command execution payload when complete")
    error: Optional[str] = Field(default=None, description="Error message when status is error")


class ArtifactIndexingPolicyRequest(BaseModel):
    """Request for PATCH /api/v1/artifacts/{artifact_id}/indexing-policy."""

    indexing_enabled: bool = Field(..., description="Whether this artifact source is eligible for chunk indexing")
    disable_strategies: Optional[List[str]] = Field(default=None, description="Preparation strategy IDs to disable")


class ArtifactIndexingPolicyResponse(BaseModel):
    """Response for PATCH /api/v1/artifacts/{artifact_id}/indexing-policy."""

    artifact_id: str = Field(..., description="Requested artifact id")
    source_artifact_id: str = Field(..., description="Source artifact id whose policy was updated")
    indexing_enabled: bool = Field(..., description="Current text chunk indexing eligibility")
    disable_strategies: List[str] = Field(default_factory=list, description="Disabled preparation strategies")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Updated artifact metadata")


class ArtifactMetadataPatchRequest(BaseModel):
    """Request for PATCH /api/v1/artifacts/{artifact_id}/metadata."""

    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary artifact metadata fields to merge into the existing metadata bag.",
        json_schema_extra={"example": {"memo_asset_id": "draft_123", "source": "memo"}},
    )
    artifact_tags: Optional[List[str]] = Field(
        default=None,
        description=(
            "Optional artifact tags to store under metadata.artifact_tags. "
            "Use for retrieval labels, AI observations, and other filterable concepts."
        ),
        json_schema_extra={"example": ["jersey", "signed", "game-used"]},
    )
    merge_artifact_tags: bool = Field(
        default=True,
        description="When true, append artifact_tags to existing tags instead of replacing them.",
        json_schema_extra={"example": True},
    )


class ArtifactPrepStrategyItem(BaseModel):
    """Registered preparation strategy visible to artifact management clients."""

    strategy_id: str = Field(..., description="Preparation strategy ID")
    strategy_version: str = Field(..., description="Preparation strategy version")
    manifest: Dict[str, Any] = Field(default_factory=dict, description="Strategy manifest")
    required_capabilities: List[str] = Field(default_factory=list, description="Worker capabilities required by the tool")


class ArtifactPrepStrategiesResponse(BaseModel):
    """Response for GET /api/v1/artifacts/preparation/strategies."""

    items: List[ArtifactPrepStrategyItem] = Field(default_factory=list, description="Registered preparation strategies")


class ArtifactPrepPlanRequest(BaseModel):
    """Dry-run preparation planning request."""

    content_type: str = Field(default="application/octet-stream", description="Artifact MIME type")
    extension: Optional[str] = Field(default=None, description="Optional extension including leading dot")
    filename: Optional[str] = Field(default=None, description="Optional filename")
    kind: ArtifactKind = Field(default=ArtifactKind.USER_UPLOAD, description="Artifact kind")
    bytes: int = Field(default=0, ge=0, description="Payload size in bytes")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Artifact metadata sample")
    prep_hints: ArtifactPrepHints = Field(default_factory=ArtifactPrepHints, description="Preparation hints")
    include_planner: bool = Field(default=False, description="Reserved for future cold-path planner calls")


class ArtifactPrepPlanResponse(BaseModel):
    """Dry-run preparation plan response."""

    prep_decision_source: Literal["dispatch", "planner"] = Field(..., description="Decision source")
    plan: Dict[str, Any] = Field(default_factory=dict, description="Canonical ArtifactPrepPlan")
    diagnostics: List[str] = Field(default_factory=list, description="Selection diagnostics")


# --- Derived-text chunk indexing status helpers (ADR-0063) ---


_REINDEX_TASK_TTL_SECONDS = 24 * 60 * 60


def _artifact_indexing_enabled(meta: ArtifactMetadata) -> bool:
    """Return durable per-artifact indexing eligibility; defaults to enabled."""

    md = meta.metadata or {}
    for key in ("artifact_indexing_enabled", "indexing_enabled", "artifact_rag_enabled", "rag_eligible"):
        if key in md:
            return bool(md.get(key))
    return True


def _metadata_derived_text_artifact_id(meta: ArtifactMetadata) -> Optional[str]:
    """Return an explicit/current derived-text id from source metadata when present."""

    md = meta.metadata or {}
    for key in (
        "active_derived_text_artifact_id",
        "derived_text_artifact_id",
        "extracted_text_artifact_id",
    ):
        value = md.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    derived_ids = md.get("derived_artifact_ids")
    if isinstance(derived_ids, dict):
        for key in ("derived_text", "extracted_text", "text"):
            value = derived_ids.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _select_current_derived_text(
    rows: List[ArtifactMetadata],
    *,
    preferred_id: Optional[str] = None,
) -> Optional[ArtifactMetadata]:
    """Select the current derived-text artifact deterministically."""

    candidates = [row for row in rows if row.kind == ArtifactKind.DERIVED_TEXT]
    if not candidates:
        return None
    if preferred_id:
        for row in candidates:
            if row.id == preferred_id:
                return row
    return sorted(candidates, key=lambda row: (float(row.created_at or 0.0), row.id), reverse=True)[0]


def _resolve_rag_source_derived(
    store: ArtifactStoreProtocol,
    meta: ArtifactMetadata,
    *,
    tenant_id: str,
    principal_id: Optional[str],
    motet_id: Optional[str],
) -> tuple[Optional[str], Optional[str], Literal["source", "derived_text", "unsupported"]]:
    """Map an artifact row to (source_artifact_id, derived_text_artifact_id, role)."""

    if meta.kind == ArtifactKind.DERIVED_TEXT:
        if not meta.source_artifact_id:
            return None, None, "unsupported"
        return meta.source_artifact_id, meta.id, "derived_text"
    if meta.kind in (ArtifactKind.USER_UPLOAD, ArtifactKind.TOOL_ARTIFACT):
        preferred_id = _metadata_derived_text_artifact_id(meta)
        if preferred_id:
            preferred = store.get_metadata(
                preferred_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
                motet_id=motet_id,
            )
            if (
                preferred
                and preferred.kind == ArtifactKind.DERIVED_TEXT
                and preferred.source_artifact_id == meta.id
            ):
                return meta.id, preferred.id, "source"
        rows = store.list(
            kind=ArtifactKind.DERIVED_TEXT,
            source_artifact_id=meta.id,
            limit=20,
            offset=0,
            tenant_id=tenant_id,
            principal_id=principal_id,
            motet_id=motet_id,
        )
        derived = _select_current_derived_text(rows, preferred_id=preferred_id)
        derived_id = derived.id if derived else None
        return meta.id, derived_id, "source"
    return None, None, "unsupported"


def _compute_indexing_status_item(
    *,
    artifact_id: str,
    meta: ArtifactMetadata,
    store: ArtifactStoreProtocol,
    artifact_rag_enabled: bool,
    tenant_id: str,
    principal_id: Optional[str],
    motet_id: Optional[str],
) -> ArtifactIndexingStatusItem:
    source_id, derived_id, role = _resolve_rag_source_derived(
        store,
        meta,
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
    )
    policy_meta = meta
    if source_id and source_id != meta.id:
        source_meta = store.get_metadata(
            source_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            motet_id=motet_id,
        )
        if source_meta:
            policy_meta = source_meta
    indexing_enabled = _artifact_indexing_enabled(policy_meta)
    chunks_by_strategy: Dict[str, int] = {}
    chunk_count_error: Optional[str] = None
    if source_id and tenant_id:
        try:
            from ....core.config import Config
            from ....core.rag.repository import ArtifactChunkRepository

            cfg = Config()
            repo = ArtifactChunkRepository(
                native_text_mode=str(getattr(cfg, "artifact_rag_native_text_mode", "auto")),
            )
            chunks_by_strategy = repo.count_source_chunks_by_strategy(tenant_id=tenant_id, source_artifact_id=source_id)
        except Exception as e:
            chunk_count_error = str(e)
            logger.warning(
                "artifact_indexing_status_chunk_count_failed",
                error=str(e),
                source_artifact_id=source_id,
                exc_info=True,
            )

    total_chunks = sum(chunks_by_strategy.values())
    md = policy_meta.metadata or {}
    versions = md.get("prep_strategy_versions") or {}
    states = md.get("prep_state_by_strategy") or {}
    derived_sets: List[DerivedSetStatus] = []
    strategy_ids = set(chunks_by_strategy.keys()) | {str(k) for k in versions.keys()} | {str(k) for k in states.keys()}
    for strategy_id in sorted(strategy_ids):
        sid = str(strategy_id)
        count = int(chunks_by_strategy.get(sid, 0))
        prep_state = str(states.get(sid) or "prep_complete")
        confidence = 0.6 if prep_state == "prep_partial" else 1.0
        derived_sets.append(
            DerivedSetStatus(
                strategy_id=sid,
                strategy_version=str(versions.get(sid, "")) or "unknown",
                derived_artifact_ids=[derived_id] if derived_id else [],
                chunks_indexed=count,
                prep_state=cast(ArtifactPrepState, prep_state),
                confidence=confidence,
            )
        )
    detail: Optional[str] = None
    if role == "unsupported":
        if meta.kind == ArtifactKind.DERIVED_TEXT and not meta.source_artifact_id:
            summary: IndexingStatusSummary = "missing_source_link"
            detail = "Derived text artifact has no source_artifact_id"
        else:
            summary = "unsupported_kind"
            detail = f"Kind {meta.kind} is not used as a text chunk index source"
        return ArtifactIndexingStatusItem(
            artifact_id=artifact_id,
            artifact_rag_globally_enabled=artifact_rag_enabled,
            source_artifact_id=source_id,
            derived_sets=derived_sets,
            indexing_enabled=indexing_enabled,
            chunks_by_strategy=chunks_by_strategy,
            total_chunks_indexed=total_chunks,
            summary=summary,
            detail=detail,
        )

    if not indexing_enabled:
        return ArtifactIndexingStatusItem(
            artifact_id=artifact_id,
            artifact_rag_globally_enabled=artifact_rag_enabled,
            source_artifact_id=source_id,
            derived_sets=derived_sets,
            indexing_enabled=False,
            chunks_by_strategy=chunks_by_strategy,
            total_chunks_indexed=total_chunks,
            summary="indexing_disabled",
            detail="Chunk indexing is disabled for this artifact",
        )

    if chunk_count_error:
        return ArtifactIndexingStatusItem(
            artifact_id=artifact_id,
            artifact_rag_globally_enabled=artifact_rag_enabled,
            source_artifact_id=source_id,
            derived_sets=derived_sets,
            indexing_enabled=indexing_enabled,
            chunks_by_strategy={},
            total_chunks_indexed=0,
            summary="index_unavailable",
            detail=f"Could not read chunk index status: {chunk_count_error}",
        )

    prep_states = {item.prep_state for item in derived_sets}
    if "prep_running" in prep_states:
        return ArtifactIndexingStatusItem(
            artifact_id=artifact_id,
            artifact_rag_globally_enabled=artifact_rag_enabled,
            source_artifact_id=source_id,
            derived_sets=derived_sets,
            indexing_enabled=indexing_enabled,
            chunks_by_strategy=chunks_by_strategy,
            total_chunks_indexed=total_chunks,
            summary="prep_running",
            detail="Artifact preparation is currently running",
        )

    if "prep_partial" in prep_states and total_chunks > 0:
        return ArtifactIndexingStatusItem(
            artifact_id=artifact_id,
            artifact_rag_globally_enabled=artifact_rag_enabled,
            source_artifact_id=source_id,
            derived_sets=derived_sets,
            indexing_enabled=indexing_enabled,
            chunks_by_strategy=chunks_by_strategy,
            total_chunks_indexed=total_chunks,
            summary="prep_partial",
            detail="Artifact was indexed through a fallback or partial preparation path",
        )

    if "prep_failed" in prep_states and total_chunks == 0:
        return ArtifactIndexingStatusItem(
            artifact_id=artifact_id,
            artifact_rag_globally_enabled=artifact_rag_enabled,
            source_artifact_id=source_id,
            derived_sets=derived_sets,
            indexing_enabled=indexing_enabled,
            chunks_by_strategy=chunks_by_strategy,
            total_chunks_indexed=0,
            summary="prep_failed",
            detail="Artifact preparation failed before chunks were indexed",
        )

    if "prep_pending" in prep_states and total_chunks == 0:
        return ArtifactIndexingStatusItem(
            artifact_id=artifact_id,
            artifact_rag_globally_enabled=artifact_rag_enabled,
            source_artifact_id=source_id,
            derived_sets=derived_sets,
            indexing_enabled=indexing_enabled,
            chunks_by_strategy=chunks_by_strategy,
            total_chunks_indexed=0,
            summary="prep_pending",
            detail="Artifact preparation is pending",
        )

    if total_chunks > 0:
        return ArtifactIndexingStatusItem(
            artifact_id=artifact_id,
            artifact_rag_globally_enabled=artifact_rag_enabled,
            source_artifact_id=source_id,
            derived_sets=derived_sets,
            indexing_enabled=indexing_enabled,
            chunks_by_strategy=chunks_by_strategy,
            total_chunks_indexed=total_chunks,
            summary="indexed",
            detail=None,
        )

    if not artifact_rag_enabled:
        detail = "Chunks not indexed; enable MOTET_ARTIFACT_RAG_ENABLED or use Reindex after enabling"
    elif role == "derived_text" and not source_id:
        detail = "Derived text artifact has no source linkage"
    elif not derived_id and meta.kind == ArtifactKind.DERIVED_TEXT:
        detail = "Derived artifact is not linked to a source artifact"
    else:
        detail = "Artifact is ready but not indexed yet; use Reindex or wait for preparation hook"

    return ArtifactIndexingStatusItem(
        artifact_id=artifact_id,
        artifact_rag_globally_enabled=artifact_rag_enabled,
        source_artifact_id=source_id,
        derived_sets=derived_sets,
        indexing_enabled=indexing_enabled,
        chunks_by_strategy=chunks_by_strategy,
        total_chunks_indexed=0,
        summary="ready_not_indexed",
        detail=detail,
    )


def _store_reindex_task_state(task_id: str, state: Dict[str, Any]) -> None:
    """Persist lightweight reindex task state for Manage UI polling."""

    try:
        from ....core.distributed.redis_manager import get_sync_redis_client

        client = get_sync_redis_client("artifact_reindex_tasks")
        import json

        client.setex(
            f"artifact_reindex_task:{task_id}",
            _REINDEX_TASK_TTL_SECONDS,
            json.dumps(state, ensure_ascii=False, default=str),
        )
    except Exception as e:
        logger.warning("artifact_reindex_task_state_store_failed", task_id=task_id, error=str(e), exc_info=True)


def _load_reindex_task_state(task_id: str) -> Optional[Dict[str, Any]]:
    """Load lightweight reindex task state."""

    try:
        from ....core.distributed.redis_manager import get_sync_redis_client

        client = get_sync_redis_client("artifact_reindex_tasks")
        raw = client.get(f"artifact_reindex_task:{task_id}")
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        import json

        data = json.loads(str(raw))
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("artifact_reindex_task_state_load_failed", task_id=task_id, error=str(e), exc_info=True)
        return None


async def _run_reindex_job(task_id: str, command: Any, state: Dict[str, Any]) -> None:
    """Execute a reindex command in the background and update task state."""

    from ....core.workers import global_invoker

    running = {**state, "status": "running", "error": None, "result": None}
    _store_reindex_task_state(task_id, running)
    try:
        result = await asyncio.to_thread(global_invoker.execute_command, command)
        result_data = result.get("data") if isinstance(result, dict) else None
        metadata = {}
        if isinstance(result_data, dict):
            metadata = {
                "strategy_id": result_data.get("strategy_id") or running.get("strategy_id"),
                "strategy_version": result_data.get("strategy_version") or running.get("strategy_version"),
                "prep_decision_source": result_data.get("prep_decision_source") or running.get("prep_decision_source"),
                "cache_hit": result_data.get("cache_hit"),
                "derived_artifact_ids": result_data.get("derived_artifact_ids") or running.get("derived_artifact_ids") or [],
            }
        _store_reindex_task_state(task_id, {**running, **metadata, "status": "success", "result": result})
    except Exception as e:
        logger.error("artifact_reindex_job_failed", task_id=task_id, error=str(e), exc_info=True)
        _store_reindex_task_state(task_id, {**running, "status": "error", "error": str(e)})


def _conversation_id_for_reindex(
    store: ArtifactStoreProtocol,
    meta: ArtifactMetadata,
    source_id: Optional[str],
    *,
    tenant_id: str,
    principal_id: Optional[str],
    motet_id: Optional[str],
) -> str:
    md = meta.metadata or {}
    conv = md.get("conversation_id")
    if isinstance(conv, str) and conv.strip():
        return conv
    if source_id and source_id != meta.id:
        src = store.get_metadata(
            source_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            motet_id=motet_id,
        )
        if src and src.metadata:
            c2 = src.metadata.get("conversation_id")
            if isinstance(c2, str) and c2.strip():
                return c2
    return ""

# --- Endpoints ---

def _resolve_artifact_scope(
    principal: Principal,
    *,
    tenant_id: Optional[str] = None,
    motet_id: Optional[str] = None,
) -> tuple[str, str, Optional[str]]:
    """
    Resolve artifact access scope.

    Normal users are always scoped to their own principal. Admin users may pass
    tenant_id/motet_id query params from the Manage UI scope selector; when they
    do, principal_id is intentionally omitted so tenant-scoped artifacts from
    service accounts and other principals are visible.
    """
    principal_motet_id, principal_tenant_id, principal_id = get_principal_context(principal)
    has_scope_override = bool(tenant_id or motet_id)
    if has_scope_override and not is_admin_principal(principal):
        raise HTTPException(status_code=403, detail="Admin role required for artifact scope override")

    return (
        motet_id or principal_motet_id,
        tenant_id or principal_tenant_id,
        None if has_scope_override and is_admin_principal(principal) else principal_id,
    )


def _normalize_artifact_tags(values: Any) -> List[str]:
    """Return stable, non-empty artifact tag strings for metadata storage."""

    if isinstance(values, str):
        values = values.split(",")
    if not isinstance(values, list):
        values = []
    return list(dict.fromkeys(str(value).strip() for value in values or [] if str(value).strip()))


@router.get("", response_model=ArtifactListResponse)
async def list_artifacts(
    kind: Optional[ArtifactKind] = Query(None, description="Filter by artifact kind"),
    conversation_id: Optional[str] = Query(None, description="Filter by conversation ID"),
    source_artifact_id: Optional[str] = Query(
        None,
        description="Filter to derived artifacts for a given source artifact ID",
    ),
    tenant_id: Optional[str] = Query(None, description="Admin-only tenant scope override"),
    motet_id: Optional[str] = Query(None, description="Admin-only motet/environment scope override"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(get_current_principal),
) -> ArtifactListResponse:
    """
    List artifacts visible to the current principal.

    Combine ``kind`` and ``source_artifact_id`` to discover derived artifacts
    (for example ``kind=derived_video_poster`` for a video upload's poster frame).
    """
    effective_motet_id, effective_tenant_id, effective_principal_id = _resolve_artifact_scope(
        principal,
        tenant_id=tenant_id,
        motet_id=motet_id,
    )
    store = get_artifact_store()
    items = store.list(
        kind=kind,
        conversation_id=conversation_id,
        source_artifact_id=source_artifact_id,
        limit=limit,
        offset=offset,
        tenant_id=effective_tenant_id,
        principal_id=effective_principal_id,
        motet_id=effective_motet_id,
    )
    if source_artifact_id:
        expected = source_artifact_id.strip()
        items = [item for item in items if (item.source_artifact_id or "").strip() == expected]
    
    return ArtifactListResponse(
        items=items,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/indexing-status",
    response_model=ArtifactIndexingBulkStatusResponse,
    summary="Bulk derived-text chunk indexing status",
    description=(
        "returns Valkey chunk index health for up to 80 artifacts. "
        "Unknown or inaccessible ids are omitted."
    ),
)
async def bulk_artifact_indexing_status(
    artifact_id: List[str] = Query(
        default_factory=list,
        description="Repeat query parameter once per artifact id (max 80)",
    ),
    tenant_id: Optional[str] = Query(None, description="Admin-only tenant scope override"),
    motet_id: Optional[str] = Query(None, description="Admin-only motet scope override"),
    principal: Principal = Depends(get_current_principal),
) -> ArtifactIndexingBulkStatusResponse:
    if len(artifact_id) > 80:
        raise HTTPException(
            status_code=400,
            detail="At most 80 artifact_id query parameters allowed",
        )
    effective_motet_id, effective_tenant_id, effective_principal_id = _resolve_artifact_scope(
        principal,
        tenant_id=tenant_id,
        motet_id=motet_id,
    )
    from ....core.config import Config

    cfg = Config()
    rag_enabled = bool(getattr(cfg, "artifact_rag_enabled", False))
    store = get_artifact_store()
    items: List[ArtifactIndexingStatusItem] = []
    seen: set[str] = set()
    for raw_id in artifact_id:
        aid = (raw_id or "").strip()
        if not aid or aid in seen:
            continue
        seen.add(aid)
        meta = store.get_metadata(
            aid,
            tenant_id=effective_tenant_id,
            principal_id=effective_principal_id,
            motet_id=effective_motet_id,
        )
        if not meta:
            continue
        items.append(
            _compute_indexing_status_item(
                artifact_id=aid,
                meta=meta,
                store=store,
                artifact_rag_enabled=rag_enabled,
                tenant_id=effective_tenant_id,
                principal_id=effective_principal_id,
                motet_id=effective_motet_id,
            )
        )
    return ArtifactIndexingBulkStatusResponse(items=items)


@router.get(
    "/preparation/strategies",
    response_model=ArtifactPrepStrategiesResponse,
    summary="List artifact preparation strategies",
)
async def list_preparation_strategies(
    principal: Principal = Depends(get_current_principal),
) -> ArtifactPrepStrategiesResponse:
    if not is_admin_principal(principal):
        raise HTTPException(status_code=403, detail="Admin role required to list preparation strategies")
    from ....core.artifacts.preparation.strategies import ensure_builtin_prep_tools_registered
    from ....core.tools.registry import registry as tool_registry

    ensure_builtin_prep_tools_registered()
    items: List[ArtifactPrepStrategyItem] = []
    seen: set[str] = set()
    for tool_name, tool in sorted(tool_registry.list_items().items()):
        manifest = getattr(tool, "prep_manifest", None)
        if manifest is None:
            continue
        manifest_data = manifest.model_dump(mode="json") if hasattr(manifest, "model_dump") else dict(manifest)
        sid = str(manifest_data.get("strategy_id") or tool_name)
        if sid in seen:
            continue
        seen.add(sid)
        items.append(
            ArtifactPrepStrategyItem(
                strategy_id=sid,
                strategy_version=str(manifest_data.get("strategy_version") or ""),
                manifest=manifest_data,
                required_capabilities=list(getattr(tool, "required_capabilities", []) or []),
            )
        )
    return ArtifactPrepStrategiesResponse(items=items)


@router.post(
    "/preparation/plan",
    response_model=ArtifactPrepPlanResponse,
    summary="Dry-run artifact preparation strategy selection",
)
async def plan_artifact_preparation(
    req: ArtifactPrepPlanRequest,
    principal: Principal = Depends(get_current_principal),
) -> ArtifactPrepPlanResponse:
    effective_motet_id, effective_tenant_id, effective_principal_id = get_principal_context(principal)
    import time

    meta = ArtifactMetadata(
        id="dry-run",
        kind=req.kind,
        content_type=req.content_type,
        bytes=req.bytes,
        checksum_sha256="",
        created_at=time.time(),
        tenant_id=effective_tenant_id,
        principal_id=effective_principal_id,
        motet_id=effective_motet_id,
        metadata={**req.metadata, **({"filename": req.filename} if req.filename else {})},
    )
    context = ArtifactPrepContext(
        artifact=meta,
        payload=b"",
        payload_info=ArtifactPayloadInfo(
            content_type=req.content_type,
            extension=req.extension,
            bytes=req.bytes,
            filename=req.filename,
        ),
        source_artifact_id=str(meta.id),
        artifact_tags=[],
        tenant_id=effective_tenant_id,
        principal_id=effective_principal_id,
        motet_id=effective_motet_id,
        conversation_id="",
        role="user",
        hints=req.prep_hints,
        config={},
    )
    if req.include_planner:
        logger.info("artifact_prep_plan_include_planner_ignored", reason="planner_not_enabled")
    try:
        selection = ArtifactPrepSelector().select(context)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
    return ArtifactPrepPlanResponse(
        prep_decision_source=selection.plan.prep_decision_source,
        plan=selection.plan.model_dump(mode="json"),
        diagnostics=selection.plan.diagnostics,
    )


@router.post(
    "/{artifact_id}/reindex",
    response_model=ArtifactReindexResponse,
    summary="Prepare and reindex chunks for an artifact",
    description=(
        "Runs core.prepare_artifact_index for the resolved source artifact and optional preparation strategy. "
        "Requires MOTET_ARTIFACT_RAG_ENABLED and workers with embedding capabilities."
    ),
)
async def reindex_artifact(
    artifact_id: str,
    wait: bool = Query(False, description="If true, block until indexing completes; default queues background work"),
    strategy_id: Optional[str] = Query(None, description="Optional preparation strategy ID override"),
    tenant_id: Optional[str] = Query(None, description="Admin-only tenant scope override"),
    motet_id: Optional[str] = Query(None, description="Admin-only motet scope override"),
    principal: Principal = Depends(get_current_principal),
) -> ArtifactReindexResponse:
    from ....core.config import Config
    from motet.core.commands.command_type_registry import command_type_registry

    cfg = Config()
    if not bool(getattr(cfg, "artifact_rag_enabled", False)):
        raise HTTPException(
            status_code=503,
            detail="Artifact chunk indexing is disabled (MOTET_ARTIFACT_RAG_ENABLED=false)",
        )

    effective_motet_id, effective_tenant_id, effective_principal_id = _resolve_artifact_scope(
        principal,
        tenant_id=tenant_id,
        motet_id=motet_id,
    )
    store = get_artifact_store()
    meta = store.get_metadata(
        artifact_id,
        tenant_id=effective_tenant_id,
        principal_id=effective_principal_id,
        motet_id=effective_motet_id,
    )
    if not meta:
        raise HTTPException(status_code=404, detail="Artifact not found")

    source_id, derived_id, role = _resolve_rag_source_derived(
        store,
        meta,
        tenant_id=effective_tenant_id,
        principal_id=effective_principal_id,
        motet_id=effective_motet_id,
    )
    if role == "unsupported":
        raise HTTPException(
            status_code=400,
            detail="This artifact cannot be prepared/indexed",
        )
    if not source_id:
        raise HTTPException(status_code=400, detail="Could not resolve source artifact for indexing")
    policy_meta = meta
    if source_id != meta.id:
        source_meta = store.get_metadata(
            source_id,
            tenant_id=effective_tenant_id,
            principal_id=effective_principal_id,
            motet_id=effective_motet_id,
        )
        if source_meta:
            policy_meta = source_meta
    if not _artifact_indexing_enabled(policy_meta):
        raise HTTPException(status_code=409, detail="Chunk indexing is disabled for this artifact")

    conv = _conversation_id_for_reindex(
        store,
        meta,
        source_id,
        tenant_id=effective_tenant_id,
        principal_id=effective_principal_id,
        motet_id=effective_motet_id,
    )

    reg = command_type_registry.get("core.prepare_artifact_index")
    if not reg or reg.data_class is None:
        raise HTTPException(status_code=500, detail="core.prepare_artifact_index is not registered")

    try:
        data_instance = reg.data_class.model_validate(
            {
                "source_artifact_id": source_id,
                "derived_artifact_id": derived_id,
                "strategy_id": strategy_id,
                "force_reindex": True,
            }
        )
    except Exception as ve:
        raise HTTPException(status_code=422, detail=f"Invalid index payload: {ve}")

    tenant_id_str = effective_tenant_id or (principal.tenant_id or "")
    principal_id_str = principal.id or ""
    motet_id_str = (effective_motet_id or principal.motet_id or "") or "default"
    task_id = str(uuid.uuid4())
    impl = reg.implementation
    try:
        command = impl(
            task_id=task_id,
            conversation_id=conv,
            tenant_id=tenant_id_str,
            principal_id=principal_id_str,
            motet_id=motet_id_str,
            data=data_instance,
        )
    except TypeError:
        try:
            command = impl(task_id=task_id, conversation_id=conv, data=data_instance)
        except Exception as ce:
            raise HTTPException(status_code=500, detail=f"Failed to construct command: {ce}")

    state = {
        "command_type": "core.prepare_artifact_index",
        "task_id": task_id,
        "artifact_id": artifact_id,
        "source_artifact_id": source_id,
        "derived_artifact_ids": [derived_id] if derived_id else [],
        "strategy_id": strategy_id,
        "strategy_version": None,
        "prep_decision_source": None,
        "cache_hit": None,
        "tenant_id": effective_tenant_id,
        "motet_id": effective_motet_id,
        "principal_id": effective_principal_id,
        "status": "queued",
        "result": None,
        "error": None,
    }
    _store_reindex_task_state(task_id, state)

    if wait:
        try:
            await asyncio.wait_for(_run_reindex_job(task_id, command, state), timeout=300.0)
        except asyncio.TimeoutError:
            _store_reindex_task_state(task_id, {**state, "status": "error", "error": "Reindex timed out after 300s"})
            raise HTTPException(status_code=408, detail="Reindex timed out after 300s")
        latest = _load_reindex_task_state(task_id) or state
        result_payload = latest.get("result")
        result_data = result_payload.get("data") if isinstance(result_payload, dict) else None
        if isinstance(result_data, dict):
            latest = {
                **latest,
                "strategy_id": result_data.get("strategy_id") or latest.get("strategy_id"),
                "strategy_version": result_data.get("strategy_version") or latest.get("strategy_version"),
                "prep_decision_source": result_data.get("prep_decision_source") or latest.get("prep_decision_source"),
                "cache_hit": result_data.get("cache_hit"),
                "derived_artifact_ids": result_data.get("derived_artifact_ids") or latest.get("derived_artifact_ids") or [],
            }
            _store_reindex_task_state(task_id, latest)
        latest_status = str(latest.get("status") or "error")
        latest_strategy_id = latest.get("strategy_id")
        latest_strategy_version = latest.get("strategy_version")
        latest_decision_source = latest.get("prep_decision_source")
        latest_cache_hit = latest.get("cache_hit")
        latest_derived_ids = latest.get("derived_artifact_ids") or []
        latest_error = latest.get("error")
        return ArtifactReindexResponse(
            task_id=task_id,
            status=cast(Literal["queued", "running", "success", "error"], latest_status),
            strategy_id=str(latest_strategy_id) if latest_strategy_id is not None else None,
            strategy_version=str(latest_strategy_version) if latest_strategy_version is not None else None,
            prep_decision_source=cast(Optional[Literal["dispatch", "planner"]], latest_decision_source),
            cache_hit=latest_cache_hit if isinstance(latest_cache_hit, bool) else None,
            derived_artifact_ids=[str(item) for item in latest_derived_ids] if isinstance(latest_derived_ids, list) else [],
            result=latest.get("result"),
            error=str(latest_error) if latest_error is not None else None,
        )

    asyncio.create_task(_run_reindex_job(task_id, command, state))
    return ArtifactReindexResponse(
        task_id=task_id,
        status="queued",
        strategy_id=strategy_id,
        cache_hit=None,
        derived_artifact_ids=[derived_id] if derived_id else [],
    )


@router.get(
    "/reindex-tasks/{task_id}",
    response_model=ArtifactReindexTaskStatusResponse,
    summary="Get artifact text reindex task status",
)
async def get_reindex_task_status(
    task_id: str,
    principal: Principal = Depends(get_current_principal),
) -> ArtifactReindexTaskStatusResponse:
    state = _load_reindex_task_state(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="Reindex task not found")
    _principal_motet_id, _principal_tenant_id, principal_id = get_principal_context(principal)
    state_principal_id = state.get("principal_id")
    if state_principal_id and state_principal_id != principal_id and not is_admin_principal(principal):
        raise HTTPException(status_code=404, detail="Reindex task not found")
    store = get_artifact_store()
    meta = store.get_metadata(
        str(state.get("source_artifact_id") or ""),
        tenant_id=str(state.get("tenant_id") or ""),
        principal_id=None if is_admin_principal(principal) else principal_id,
        motet_id=str(state.get("motet_id") or "") or None,
    )
    if not meta:
        raise HTTPException(status_code=404, detail="Reindex task not found")
    return ArtifactReindexTaskStatusResponse.model_validate(state)


@router.patch(
    "/{artifact_id}/indexing-policy",
    response_model=ArtifactIndexingPolicyResponse,
    summary="Update artifact text indexing eligibility",
    responses={
        200: {"description": "Artifact indexing policy updated"},
        400: {"description": "Artifact cannot be configured for text chunk indexing"},
        403: {"description": "Admin role required for scope override"},
        404: {"description": "Artifact not found"},
        500: {"description": "Artifact metadata update failed"},
    },
)
async def update_artifact_indexing_policy(
    artifact_id: str,
    req: ArtifactIndexingPolicyRequest,
    tenant_id: Optional[str] = Query(None, description="Admin-only tenant scope override"),
    motet_id: Optional[str] = Query(None, description="Admin-only motet scope override"),
    principal: Principal = Depends(get_current_principal),
) -> ArtifactIndexingPolicyResponse:
    effective_motet_id, effective_tenant_id, effective_principal_id = _resolve_artifact_scope(
        principal,
        tenant_id=tenant_id,
        motet_id=motet_id,
    )
    store = get_artifact_store()
    meta = store.get_metadata(
        artifact_id,
        tenant_id=effective_tenant_id,
        principal_id=effective_principal_id,
        motet_id=effective_motet_id,
    )
    if not meta:
        raise HTTPException(status_code=404, detail="Artifact not found")
    source_id, _derived_id, role = _resolve_rag_source_derived(
        store,
        meta,
        tenant_id=effective_tenant_id,
        principal_id=effective_principal_id,
        motet_id=effective_motet_id,
    )
    if role == "unsupported" or not source_id:
        raise HTTPException(status_code=400, detail="This artifact cannot be configured for text chunk indexing")

    patch_data: Dict[str, Any] = {
        "artifact_indexing_enabled": bool(req.indexing_enabled),
        "indexing_enabled": bool(req.indexing_enabled),
    }
    if req.disable_strategies is not None:
        patch_data["disable_strategies"] = list(dict.fromkeys(req.disable_strategies))
    updated = store.update_metadata(
        source_id,
        patch_data,
        tenant_id=effective_tenant_id,
        principal_id=effective_principal_id,
        motet_id=effective_motet_id,
    )
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update artifact indexing policy")
    if not req.indexing_enabled:
        try:
            from ....core.config import Config
            from ....core.rag.repository import ArtifactChunkRepository

            cfg = Config()
            repo = ArtifactChunkRepository(
                native_text_mode=str(getattr(cfg, "artifact_rag_native_text_mode", "auto")),
            )
            repo.delete_source_chunks(tenant_id=effective_tenant_id, source_artifact_id=source_id)
        except Exception as e:
            logger.warning(
                "artifact_indexing_policy_chunk_delete_failed",
                source_artifact_id=source_id,
                error=str(e),
                exc_info=True,
            )
    return ArtifactIndexingPolicyResponse(
        artifact_id=artifact_id,
        source_artifact_id=source_id,
        indexing_enabled=_artifact_indexing_enabled(updated),
        disable_strategies=list((updated.metadata or {}).get("disable_strategies") or []),
        metadata=updated.metadata or {},
    )


@router.patch(
    "/{artifact_id}/metadata",
    response_model=ArtifactMetadata,
    summary="Merge artifact metadata and tags",
    responses={
        200: {"description": "Artifact metadata updated"},
        403: {"description": "Admin role required for scope override"},
        404: {"description": "Artifact not found"},
        422: {"description": "Invalid metadata patch"},
        500: {"description": "Artifact metadata update failed"},
    },
)
async def update_artifact_metadata(
    artifact_id: str,
    req: ArtifactMetadataPatchRequest,
    tenant_id: Optional[str] = Query(None, description="Admin-only tenant scope override"),
    motet_id: Optional[str] = Query(None, description="Admin-only motet/environment scope override"),
    principal: Principal = Depends(get_current_principal),
) -> ArtifactMetadata:
    """Merge custom metadata and artifact_tags into an existing artifact."""
    effective_motet_id, effective_tenant_id, effective_principal_id = _resolve_artifact_scope(
        principal,
        tenant_id=tenant_id,
        motet_id=motet_id,
    )
    store = get_artifact_store()
    meta = store.get_metadata(
        artifact_id=artifact_id,
        tenant_id=effective_tenant_id,
        principal_id=effective_principal_id,
        motet_id=effective_motet_id,
    )
    if not meta:
        raise HTTPException(status_code=404, detail="Artifact not found")

    patch_data: Dict[str, Any] = dict(req.metadata or {})
    if "artifact_tags" in patch_data:
        patch_data["artifact_tags"] = _normalize_artifact_tags(patch_data.get("artifact_tags"))
    if req.artifact_tags is not None:
        requested_tags = _normalize_artifact_tags(req.artifact_tags)
        if req.merge_artifact_tags:
            existing_tags = _normalize_artifact_tags((meta.metadata or {}).get("artifact_tags"))
            requested_tags = _normalize_artifact_tags([*existing_tags, *requested_tags])
        patch_data["artifact_tags"] = requested_tags

    if not patch_data:
        return meta

    updated = store.update_metadata(
        artifact_id,
        patch_data,
        tenant_id=effective_tenant_id,
        principal_id=effective_principal_id,
        motet_id=effective_motet_id,
    )
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update artifact metadata")
    return updated


@router.post("", response_model=ArtifactUploadResponse)
async def upload_artifact(
    file: UploadFile = File(...),
    kind: ArtifactKind = Query(ArtifactKind.USER_UPLOAD, description="Kind of artifact"),
    conversation_id: Optional[str] = Query(None, description="Conversation ID to associate with this upload"),
    prep_hints: Optional[str] = Form(
        None,
        description="Optional JSON-encoded ArtifactPrepHints object for preparation strategy selection/enrichment.",
    ),
    principal: Principal = Depends(get_current_principal),
) -> ArtifactUploadResponse:
    """
    Upload a new artifact (multipart/form-data).
    
    Optionally associates the artifact with a conversation_id for context tracking.
    """
    motet_id, tenant_id, principal_id = get_principal_context(principal)
    upload_content_type = file.content_type or "application/octet-stream"
    max_video_bytes: Optional[int] = None
    if upload_content_type.startswith("video/"):
        from ....core.config import Config

        cfg = Config()
        max_video_bytes = int(getattr(cfg, "artifact_max_video_bytes", 536_870_912) or 536_870_912)
        # Cheap precheck from multipart metadata before buffering the body.
        declared_size = getattr(file, "size", None)
        if declared_size is not None and int(declared_size) > max_video_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Video upload exceeds maximum size of {max_video_bytes} bytes",
            )
    content = await file.read()
    if max_video_bytes is not None and len(content) > max_video_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Video upload exceeds maximum size of {max_video_bytes} bytes",
        )
    parsed_prep_hints: ArtifactPrepHints | None = None
    if prep_hints:
        try:
            import json

            parsed = json.loads(prep_hints)
            parsed_prep_hints = ArtifactPrepHints.model_validate(parsed)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Invalid prep_hints JSON: {e}")

    # Centralized artifact creation + derivation trigger (ADR-0062)
    from motet.core.commands.builtin.artifacts import create_artifact
    from motet.core.commands.command_data_classes import CreateArtifactData
    from ....core.workers import global_invoker

    # Generate a task_id for the upload operation (create + optional derivation dispatch)
    task_id = str(uuid.uuid4())
    # Ensure conversation_id is a string (not None) for DistributedCommandContext
    # But pass the actual conversation_id (or None) to CreateArtifactData so it can be stored in metadata
    conv_id = conversation_id or ""

    logger.info(
        "upload_artifact_received",
        filename=file.filename,
        content_type=file.content_type,
        bytes=len(content),
        conversation_id=conversation_id,
        conv_id_for_command_context=conv_id,
        kind=str(kind.value),
        prep_hints=parsed_prep_hints.model_dump(exclude_none=True) if parsed_prep_hints else {},
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
    )

    create_cmd = create_artifact(
        task_id=task_id,
        conversation_id=conv_id,  # Must be string, not None for command context
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
        data=CreateArtifactData(
            payload=content,
            content_type=upload_content_type,
            kind=str(kind.value),
            filename=file.filename,
            conversation_id=conversation_id,  # Pass the actual conversation_id (can be None) for metadata storage
            prep_hints=parsed_prep_hints,
            trigger_derivations=True,
            image_derivation_names=["thumb", "base", "detail"],
        ),
    )

    logger.info(
        "upload_artifact_create_command_built",
        command_id=create_cmd.command_id,
        task_id=task_id,
        conversation_id=conversation_id,
        conv_id_for_command_context=conv_id,
        command_context_conversation_id=create_cmd.distributed_context.conversation_id,
    )

    result = await asyncio.to_thread(global_invoker.execute_command, create_cmd)
    
    # Extract artifact_id from ADR-0029 response
    from ....core.media.utils import extract_artifact_id_from_result
    try:
        artifact_id = extract_artifact_id_from_result(result)
    except ValueError as e:
        logger.error("artifact_upload_failed", error=str(e), result=result)
        raise HTTPException(status_code=500, detail="Artifact creation failed")

    return ArtifactUploadResponse(
        artifact_id=artifact_id,
        filename=file.filename or "unknown",
        content_type=file.content_type or "application/octet-stream",
        bytes=len(content),
        kind=kind,
    )

@router.get("/{artifact_id}/metadata", response_model=ArtifactMetadata)
async def get_artifact_metadata(
    artifact_id: str,
    tenant_id: Optional[str] = Query(None, description="Admin-only tenant scope override"),
    motet_id: Optional[str] = Query(None, description="Admin-only motet/environment scope override"),
    principal: Principal = Depends(get_current_principal),
) -> ArtifactMetadata:
    """
    Get metadata for a specific artifact.
    """
    effective_motet_id, effective_tenant_id, effective_principal_id = _resolve_artifact_scope(
        principal,
        tenant_id=tenant_id,
        motet_id=motet_id,
    )
    store = get_artifact_store()
    meta = store.get_metadata(
        artifact_id=artifact_id,
        tenant_id=effective_tenant_id,
        principal_id=effective_principal_id,
        motet_id=effective_motet_id,
    )
    
    if not meta:
        raise HTTPException(status_code=404, detail="Artifact not found")
        
    return meta


def _load_artifact_payload_bytes(
    store: Any,
    *,
    artifact_id: str,
    tenant_id: Optional[str],
    principal_id: Optional[str],
    motet_id: Optional[str],
    range_header: Optional[str],
) -> tuple[bytes, ArtifactMetadata, int, dict[str, str]]:
    """Load artifact bytes, honoring HTTP Range when requested (ADR-0118)."""

    from ....core.artifacts.range_utils import ByteRangeError, parse_byte_range

    meta = store.get_metadata(
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
    )
    if not meta:
        raise HTTPException(status_code=404, detail="Artifact not found")

    total_size = int(meta.bytes or 0)
    extra_headers: dict[str, str] = {"Accept-Ranges": "bytes"}

    if range_header:
        try:
            start, end = parse_byte_range(range_header, total_size)
        except ByteRangeError as exc:
            raise HTTPException(
                status_code=416,
                detail=str(exc),
                headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{total_size}"},
            ) from exc
        chunk = store.get_range(
            artifact_id=artifact_id,
            start=start,
            end=end,
            tenant_id=tenant_id,
            principal_id=principal_id,
            motet_id=motet_id,
        )
        if chunk is None:
            raise HTTPException(status_code=404, detail="Artifact payload missing")
        extra_headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
        return chunk, meta, 206, extra_headers

    payload = store.get(
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="Artifact payload missing")

    from ....core.artifacts.range_utils import artifact_payload_to_bytes

    try:
        data = artifact_payload_to_bytes(payload)
    except TypeError as exc:
        raise HTTPException(status_code=500, detail="Unsupported artifact payload type") from exc
    return data, meta, 200, extra_headers


# Full downloads of raw-format payloads stream in ranged chunks of this size so
# large videos never sit fully in API memory (ADR-0118).
_RAW_STREAM_CHUNK_BYTES = 8 * 1024 * 1024


def _artifact_payload_response(
    store: Any,
    *,
    artifact_id: str,
    tenant_id: Optional[str],
    principal_id: Optional[str],
    motet_id: Optional[str],
    range_header: Optional[str],
    disposition: Literal["attachment", "inline"],
) -> StreamingResponse:
    """Serve an artifact payload with Range support and raw-format chunked streaming.

    Shared by download (attachment) and stream (inline playback) endpoints
    (ADR-0118). Callers are responsible for authentication/scope resolution.
    """
    if not range_header:
        meta = store.get_metadata(
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            motet_id=motet_id,
        )
        if not meta:
            raise HTTPException(status_code=404, detail="Artifact not found")
        total_size = int(meta.bytes or 0)
        if getattr(meta, "payload_format", "envelope") == "raw" and total_size > _RAW_STREAM_CHUNK_BYTES:
            filename = meta.metadata.get("filename", f"{artifact_id}.dat")

            def iter_ranged_chunks():
                offset = 0
                while offset < total_size:
                    chunk_end = min(offset + _RAW_STREAM_CHUNK_BYTES, total_size) - 1
                    chunk = store.get_range(
                        artifact_id=artifact_id,
                        start=offset,
                        end=chunk_end,
                        tenant_id=tenant_id,
                        principal_id=principal_id,
                        motet_id=motet_id,
                    )
                    if not chunk:
                        break
                    yield chunk
                    offset += len(chunk)

            return StreamingResponse(
                iter_ranged_chunks(),
                media_type=meta.content_type,
                headers={
                    "Content-Disposition": f'{disposition}; filename="{filename}"',
                    "Content-Length": str(total_size),
                    "Accept-Ranges": "bytes",
                },
            )

    payload, meta, status_code, extra_headers = _load_artifact_payload_bytes(
        store,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
        range_header=range_header,
    )

    filename = meta.metadata.get("filename", f"{artifact_id}.dat")
    headers = {
        "Content-Disposition": f'{disposition}; filename="{filename}"',
        **extra_headers,
    }

    def iterfile():
        yield payload

    return StreamingResponse(
        iterfile(),
        status_code=status_code,
        media_type=meta.content_type,
        headers=headers,
    )


@router.get("/{artifact_id}/download")
async def download_artifact(
    artifact_id: str,
    tenant_id: Optional[str] = Query(None, description="Admin-only tenant scope override"),
    motet_id: Optional[str] = Query(None, description="Admin-only motet/environment scope override"),
    range_header: Optional[str] = Header(default=None, alias="Range"),
    principal: Principal = Depends(get_current_principal),
):
    """
    Download the raw artifact payload.
    """
    effective_motet_id, effective_tenant_id, effective_principal_id = _resolve_artifact_scope(
        principal,
        tenant_id=tenant_id,
        motet_id=motet_id,
    )
    store = get_artifact_store()
    return _artifact_payload_response(
        store,
        artifact_id=artifact_id,
        tenant_id=effective_tenant_id,
        principal_id=effective_principal_id,
        motet_id=effective_motet_id,
        range_header=range_header,
        disposition="attachment",
    )


class PlaybackTokenResponse(BaseModel):
    """Response for POST /api/v1/artifacts/{artifact_id}/playback-token."""

    artifact_id: str = Field(..., description="Artifact this token grants streaming access to")
    token: str = Field(..., description="Short-lived signed playback token")
    expires_in: int = Field(..., description="Token lifetime in seconds", json_schema_extra={"example": 300})
    stream_url: str = Field(
        ...,
        description="Relative stream URL usable directly as a media element src",
        json_schema_extra={"example": "/api/v1/artifacts/art-1/stream?token=..."},
    )


@router.post(
    "/{artifact_id}/playback-token",
    response_model=PlaybackTokenResponse,
    responses={
        404: {"description": "Artifact not found or not accessible in the caller's scope"},
    },
)
async def create_playback_token(
    artifact_id: str,
    tenant_id: Optional[str] = Query(None, description="Admin-only tenant scope override"),
    motet_id: Optional[str] = Query(None, description="Admin-only motet/environment scope override"),
    principal: Principal = Depends(get_current_principal),
) -> PlaybackTokenResponse:
    """
    Mint a short-lived playback token for streaming this artifact.

    Browser media elements cannot attach Authorization headers, so authenticated
    clients exchange their credentials for a token bound to one artifact and the
    caller's resolved access scope, then point the media element at `stream_url`.
    """
    from ....core.artifacts.playback_tokens import mint_playback_token
    from ....core.config import Config

    effective_motet_id, effective_tenant_id, effective_principal_id = _resolve_artifact_scope(
        principal,
        tenant_id=tenant_id,
        motet_id=motet_id,
    )
    store = get_artifact_store()
    meta = store.get_metadata(
        artifact_id=artifact_id,
        tenant_id=effective_tenant_id,
        principal_id=effective_principal_id,
        motet_id=effective_motet_id,
    )
    if not meta:
        raise HTTPException(status_code=404, detail="Artifact not found")

    ttl_seconds = int(getattr(Config(), "artifact_playback_token_ttl_seconds", 300))
    token = mint_playback_token(
        artifact_id=artifact_id,
        tenant_id=effective_tenant_id,
        principal_id=effective_principal_id,
        motet_id=effective_motet_id,
        ttl_seconds=ttl_seconds,
    )
    return PlaybackTokenResponse(
        artifact_id=artifact_id,
        token=token,
        expires_in=ttl_seconds,
        stream_url=f"/api/v1/artifacts/{artifact_id}/stream?token={token}",
    )


@router.get(
    "/{artifact_id}/stream",
    responses={
        206: {"description": "Partial content for Range requests"},
        401: {"description": "Missing, invalid, or expired playback token"},
        404: {"description": "Artifact not found in the token's scope"},
    },
)
async def stream_artifact(
    artifact_id: str,
    token: str = Query(..., description="Playback token from POST /{artifact_id}/playback-token"),
    range_header: Optional[str] = Header(default=None, alias="Range"),
):
    """
    Stream an artifact inline using a playback token.

    Token-authenticated (no Authorization header) so it can be used directly as
    a `<video src>`. Access scope comes from the verified token claims; honors
    HTTP Range for seek/scrub like the download endpoint.
    """
    from ....core.artifacts.playback_tokens import PlaybackTokenError, verify_playback_token

    try:
        claims = verify_playback_token(token, artifact_id=artifact_id)
    except PlaybackTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    store = get_artifact_store()
    return _artifact_payload_response(
        store,
        artifact_id=artifact_id,
        tenant_id=claims.tenant_id,
        principal_id=claims.principal_id,
        motet_id=claims.motet_id,
        range_header=range_header,
        disposition="inline",
    )


@router.get("/{artifact_id}/preview")
async def preview_artifact(
    artifact_id: str,
    tenant_id: Optional[str] = Query(None, description="Admin-only tenant scope override"),
    motet_id: Optional[str] = Query(None, description="Admin-only motet/environment scope override"),
    range_header: Optional[str] = Header(default=None, alias="Range"),
    principal: Principal = Depends(get_current_principal),
):
    """
    Preview artifact (inline content) for images/text/video.
    """
    effective_motet_id, effective_tenant_id, effective_principal_id = _resolve_artifact_scope(
        principal,
        tenant_id=tenant_id,
        motet_id=motet_id,
    )
    store = get_artifact_store()
    payload, meta, status_code, extra_headers = _load_artifact_payload_bytes(
        store,
        artifact_id=artifact_id,
        tenant_id=effective_tenant_id,
        principal_id=effective_principal_id,
        motet_id=effective_motet_id,
        range_header=range_header,
    )

    return FastAPIResponse(
        content=payload,
        status_code=status_code,
        media_type=meta.content_type,
        headers=extra_headers,
    )

_ARTIFACT_BULK_DELETE_BATCH_SIZE = 100


@router.delete("", response_model=ArtifactBulkDeleteResponse)
async def delete_all_artifacts(
    tenant_id: Optional[str] = Query(None, description="Admin-only tenant scope override"),
    motet_id: Optional[str] = Query(None, description="Admin-only motet/environment scope override"),
    principal: Principal = Depends(get_current_principal),
) -> ArtifactBulkDeleteResponse:
    """
    Delete all artifacts visible in the resolved scope.

    Normal users delete only their own artifacts. Admin scope overrides may delete
    all artifacts for the selected tenant/motet.
    """
    effective_motet_id, effective_tenant_id, effective_principal_id = _resolve_artifact_scope(
        principal,
        tenant_id=tenant_id,
        motet_id=motet_id,
    )
    store = get_artifact_store()
    deleted_count = 0
    failed_count = 0

    while True:
        items = store.list(
            limit=_ARTIFACT_BULK_DELETE_BATCH_SIZE,
            offset=0,
            tenant_id=effective_tenant_id,
            principal_id=effective_principal_id,
            motet_id=effective_motet_id,
        )
        if not items:
            break

        for item in items:
            deleted = store.delete(
                artifact_id=item.id,
                tenant_id=effective_tenant_id,
                principal_id=effective_principal_id,
                motet_id=effective_motet_id,
            )
            if deleted:
                deleted_count += 1
            else:
                failed_count += 1

    logger.info(
        "artifacts_bulk_deleted",
        deleted_count=deleted_count,
        failed_count=failed_count,
        tenant_id=effective_tenant_id,
        principal_id=effective_principal_id,
        motet_id=effective_motet_id,
    )
    return ArtifactBulkDeleteResponse(
        deleted_count=deleted_count,
        failed_count=failed_count,
    )


@router.delete("/{artifact_id}")
async def delete_artifact(
    artifact_id: str,
    tenant_id: Optional[str] = Query(None, description="Admin-only tenant scope override"),
    motet_id: Optional[str] = Query(None, description="Admin-only motet/environment scope override"),
    principal: Principal = Depends(get_current_principal),
):
    """
    Delete an artifact.
    """
    effective_motet_id, effective_tenant_id, effective_principal_id = _resolve_artifact_scope(
        principal,
        tenant_id=tenant_id,
        motet_id=motet_id,
    )
    store = get_artifact_store()
    deleted = store.delete(
        artifact_id=artifact_id,
        tenant_id=effective_tenant_id,
        principal_id=effective_principal_id,
        motet_id=effective_motet_id,
    )
    
    if not deleted:
        raise HTTPException(status_code=404, detail="Artifact not found or access denied")
        
    return {"status": "deleted", "artifact_id": artifact_id}

