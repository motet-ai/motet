"""
Motet - Artifact RAG Commands

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Distributed commands for artifact RAG. These commands prepare artifacts via
    strategies, index prepared chunks into Valkey Search, and retrieve scoped,
    citation-ready artifact chunks for context preparation.

Dependencies:
    - motet.core.artifacts.preparation for strategy selection and chunk preparation
    - motet.core.rag for Valkey storage and retrieval formatting
    - motet.core.commands.decorator for distributed command registration
    - motet.core.commands.command_data_classes for command payloads
    - motet.core.artifacts for artifact metadata and kind checks

Usage:
    result = motet.do(prepare_artifact_index, data=PrepareArtifactIndexData(...))
    context = motet.do(rag_retrieve_context, data=RagRetrieveContextData(query_text="..."))

Notes:
    - Workers must provide the centralized embedding service created during
      bootstrap; this avoids loading SentenceTransformer in worker processes.
    - Retrieval is fail-closed for missing isolation fields.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import structlog

from motet import motet
from motet.core.commands.command_data_classes import PrepareArtifactIndexData, RagRetrieveContextData
from motet.core.commands.decorator import get_motet_context
from motet.core.commands.capabilities import WorkerCapability

logger = structlog.get_logger(__name__)


def _worker_embedding_service(motet: Any) -> Any:
    worker_context = getattr(motet, "_worker_context", {}) or {}
    embedding_service = worker_context.get("embedding_service")
    if embedding_service is None:
        raise RuntimeError("Artifact RAG requires embedding_service in worker context")
    return embedding_service


def _metadata_tags(*metadata_bags: Dict[str, Any]) -> list[str]:
    """Collect artifact tags from source/derived metadata bags."""

    tags: list[str] = []
    for metadata in metadata_bags:
        raw_tags = metadata.get("tags") or metadata.get("artifact_tags") or []
        if isinstance(raw_tags, str):
            raw_tags = [part.strip() for part in raw_tags.split(",")]
        if isinstance(raw_tags, list):
            tags.extend(str(tag).strip() for tag in raw_tags if str(tag).strip())
    return list(dict.fromkeys(tags))


def _artifact_indexing_enabled(metadata: Dict[str, Any]) -> bool:
    """Return per-artifact indexing eligibility; defaults to enabled."""

    for key in ("artifact_indexing_enabled", "indexing_enabled", "artifact_rag_enabled", "rag_eligible"):
        if key in metadata:
            return bool(metadata.get(key))
    return True


def _extension_from_filename(filename: Any) -> str | None:
    name = str(filename or "").strip()
    if not name or "." not in name:
        return None
    suf = Path(name).suffix.lower()
    return suf if suf else None


def _build_prep_context_for_index(
    *,
    motet: Any,
    prepare_meta: Any,
    source_meta: Any,
    data: PrepareArtifactIndexData,
    payload: Any,
    artifact_tags: list[str],
    cfg: Any,
) -> Any:
    from motet.core.artifacts.preparation import ArtifactPayloadInfo, ArtifactPrepContext, ArtifactPrepHints
    from motet.core.artifacts.preparation.hashing import effective_source_content_hash
    from motet.core.media.utils import normalize_to_bytes

    source_md = getattr(source_meta, "metadata", {}) or {}
    prepare_md = getattr(prepare_meta, "metadata", {}) or {}
    payload_bytes = normalize_to_bytes(payload)
    conversation_id = str(
        source_md.get("conversation_id") or prepare_md.get("conversation_id") or motet.conversation_id or ""
    )
    role = str(source_md.get("role") or prepare_md.get("role") or "user")
    filename = (
        source_md.get("filename")
        or source_md.get("original_filename")
        or prepare_md.get("source_filename")
        or prepare_md.get("filename")
    )
    declared = str(getattr(prepare_meta, "checksum_sha256", "") or getattr(source_meta, "checksum_sha256", "") or "").strip()
    effective_hash = effective_source_content_hash(declared_hash=declared or None, payload_bytes=payload_bytes)
    raw_hints: dict[str, Any] = {}
    if isinstance(source_md.get("prep_hints"), dict):
        raw_hints.update(source_md["prep_hints"])
    if data.strategy_id:
        raw_hints["prep_strategy_id"] = data.strategy_id
    disable = source_md.get("disable_strategies") or raw_hints.get("disable_strategies") or []
    raw_hints["disable_strategies"] = list(disable)
    hints = ArtifactPrepHints.model_validate(raw_hints)

    chunk_size = int(getattr(cfg, "artifact_rag_chunk_size", 3200) if cfg is not None else 3200)
    chunk_overlap = int(getattr(cfg, "artifact_rag_chunk_overlap", 400) if cfg is not None else 400)

    ext = _extension_from_filename(filename)
    content_type = str(prepare_meta.content_type or source_meta.content_type or "application/octet-stream")
    if not ext and content_type == "application/json":
        ext = ".json"

    return ArtifactPrepContext(
        artifact=prepare_meta,
        payload=payload,
        payload_info=ArtifactPayloadInfo(
            content_type=content_type,
            extension=ext,
            bytes=int(getattr(prepare_meta, "bytes", 0) or len(payload_bytes)),
            content_hash=effective_hash,
            filename=str(filename) if filename else None,
        ),
        source_artifact_id=str(data.source_artifact_id),
        artifact_tags=list(artifact_tags),
        tenant_id=str(source_meta.tenant_id or motet.tenant_id),
        principal_id=str(source_meta.principal_id or motet.principal_id),
        motet_id=str(source_meta.motet_id or motet.motet_id),
        conversation_id=conversation_id,
        role=role,
        hints=hints,
        config={
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "json_max_depth": int(getattr(cfg, "artifact_prep_json_max_depth", 3) if cfg is not None else 3),
        },
    )


def _update_source_prep_metadata(
    *,
    motet: Any,
    source_meta: Any,
    source_artifact_id: str,
    strategy_id: str,
    strategy_version: str,
    prep_state: str,
) -> None:
    """Best-effort durable prep state update for indexing status."""

    metadata = getattr(source_meta, "metadata", {}) or {}
    versions = dict(metadata.get("prep_strategy_versions") or {})
    versions[strategy_id] = strategy_version
    states = dict(metadata.get("prep_state_by_strategy") or {})
    states[strategy_id] = prep_state
    motet.artifact_store.update_metadata(
        source_artifact_id,
        {"prep_strategy_versions": versions, "prep_state_by_strategy": states},
    )
    if isinstance(getattr(source_meta, "metadata", None), dict):
        source_meta.metadata.update({"prep_strategy_versions": versions, "prep_state_by_strategy": states})


@motet.command(
    description="Chunk and index an artifact or derived artifact into artifact RAG so it can be retrieved later.",
    timeout_seconds=300,
    required_capabilities=[
        WorkerCapability.FILE_OPERATIONS,
        WorkerCapability.VECTOR_OPERATIONS,
        WorkerCapability.EMBEDDINGS,
    ],
)
def prepare_artifact_index(data: PrepareArtifactIndexData) -> Dict[str, Any]:
    """Prepare and index one artifact or derived artifact for artifact RAG retrieval."""

    motet = get_motet_context()
    cfg = getattr(getattr(motet, "stack", None), "config", None)
    if cfg is not None and not bool(getattr(cfg, "artifact_rag_enabled", False)):
        return {
            "skipped": True,
            "reason": "artifact_rag_disabled",
            "source_artifact_id": data.source_artifact_id,
            "derived_artifact_id": data.derived_artifact_id,
            "chunks_indexed": 0,
        }

    from motet.core.artifacts import ArtifactKind
    from motet.core.artifacts.preparation import ArtifactPrepExecutor, ArtifactPrepSelector
    from motet.core.artifacts.preparation.hashing import chunk_cache_key
    from motet.core.artifacts.preparation.routing import should_prepare_source_instead_of_derived
    from motet.core.rag import ArtifactChunkRepository

    source_meta = motet.artifact_store.get_metadata(data.source_artifact_id)
    if not source_meta:
        raise ValueError(f"Source artifact not found: {data.source_artifact_id}")
    source_metadata = getattr(source_meta, "metadata", {}) or {}
    if not _artifact_indexing_enabled(source_metadata):
        repository = ArtifactChunkRepository()
        repository.delete_source_chunks(
            tenant_id=str(source_meta.tenant_id or motet.tenant_id),
            source_artifact_id=data.source_artifact_id,
        )
        return {
            "skipped": True,
            "reason": "artifact_indexing_disabled",
            "source_artifact_id": data.source_artifact_id,
            "derived_artifact_id": data.derived_artifact_id,
            "derived_artifact_ids": [data.derived_artifact_id] if data.derived_artifact_id else [],
            "chunks_indexed": 0,
        }

    prepare_meta = source_meta
    effective_derived_id: str | None = data.derived_artifact_id
    derived_meta: Any = None
    selected_context: Any = None
    if data.derived_artifact_id:
        derived_meta = motet.artifact_store.get_metadata(data.derived_artifact_id)
        if not derived_meta:
            raise ValueError(f"Derived artifact not found: {data.derived_artifact_id}")
        if derived_meta.source_artifact_id != data.source_artifact_id:
            raise ValueError(
                f"Derived artifact {data.derived_artifact_id} is not linked to source {data.source_artifact_id}"
            )
        source_payload = motet.artifact_store.get(source_meta.id)
        derived_payload = motet.artifact_store.get(data.derived_artifact_id)
        if derived_payload is None or derived_payload == b"" or derived_payload == "":
            return {
                "skipped": True,
                "reason": "empty_payload",
                "source_artifact_id": data.source_artifact_id,
                "derived_artifact_id": data.derived_artifact_id,
                "derived_artifact_ids": [data.derived_artifact_id] if data.derived_artifact_id else [],
                "chunks_indexed": 0,
            }
        if source_payload is None or source_payload == b"" or source_payload == "":
            prepare_meta = derived_meta
            payload = derived_payload
        else:
            derived_md = getattr(derived_meta, "metadata", {}) or {}
            artifact_tags_probe = _metadata_tags(source_metadata, derived_md)
            selector = ArtifactPrepSelector()
            source_ctx = _build_prep_context_for_index(
                motet=motet,
                prepare_meta=source_meta,
                source_meta=source_meta,
                data=data,
                payload=source_payload,
                artifact_tags=artifact_tags_probe,
                cfg=cfg,
            )
            derived_ctx = _build_prep_context_for_index(
                motet=motet,
                prepare_meta=derived_meta,
                source_meta=source_meta,
                data=data,
                payload=derived_payload,
                artifact_tags=artifact_tags_probe,
                cfg=cfg,
            )
            if should_prepare_source_instead_of_derived(selector, source_context=source_ctx, derived_context=derived_ctx):
                prepare_meta = source_meta
                payload = source_payload
                effective_derived_id = None
                selected_context = source_ctx
            else:
                prepare_meta = derived_meta
                payload = derived_payload
                selected_context = derived_ctx
    elif source_meta.kind == ArtifactKind.DERIVED_TEXT:
        prepare_meta = source_meta
        effective_derived_id = None
        payload = motet.artifact_store.get(prepare_meta.id)
    else:
        payload = motet.artifact_store.get(prepare_meta.id)

    if payload is None or payload == b"" or payload == "":
        return {
            "skipped": True,
            "reason": "empty_payload",
            "source_artifact_id": data.source_artifact_id,
            "derived_artifact_id": effective_derived_id,
            "derived_artifact_ids": [effective_derived_id] if effective_derived_id else [],
            "chunks_indexed": 0,
        }

    derived_metadata = getattr(prepare_meta, "metadata", {}) or {}
    artifact_tags = _metadata_tags(source_metadata, derived_metadata)

    context = selected_context or _build_prep_context_for_index(
        motet=motet,
        prepare_meta=prepare_meta,
        source_meta=source_meta,
        data=data,
        payload=payload,
        artifact_tags=artifact_tags,
        cfg=cfg,
    )
    selection = ArtifactPrepSelector().select(context)
    plan = selection.plan

    if not data.force_reindex:
        cache_key = chunk_cache_key(
            source_content_hash=context.payload_info.content_hash,
            strategy_id=plan.strategy_id,
            strategy_version=plan.strategy_version,
            canonical_config_hash=plan.canonical_config_hash,
            planner_decision_hash=plan.planner_decision_hash,
        )
        probe_repo = ArtifactChunkRepository(
            native_text_mode=str(getattr(cfg, "artifact_rag_native_text_mode", "auto") if cfg is not None else "auto"),
        )
        cached = probe_repo.count_chunks_matching_cache_key(
            tenant_id=context.tenant_id,
            source_artifact_id=data.source_artifact_id,
            prep_strategy_id=plan.strategy_id,
            chunk_cache_key=cache_key,
        )
        if cached > 0:
            logger.info(
                "artifact_rag_index_cache_hit",
                **motet.log_fields(
                    source_artifact_id=data.source_artifact_id,
                    derived_artifact_id=effective_derived_id,
                    strategy_id=plan.strategy_id,
                    chunk_cache_key=cache_key,
                    cached_chunks=cached,
                ),
            )
            return {
                "source_artifact_id": data.source_artifact_id,
                "derived_artifact_id": effective_derived_id,
                "derived_artifact_ids": [effective_derived_id] if effective_derived_id else [],
                "strategy_id": plan.strategy_id,
                "strategy_version": plan.strategy_version,
                "prep_state": "prep_complete",
                "prep_decision_source": plan.prep_decision_source,
                "chunks_indexed": 0,
                "cache_hit": True,
            }

    try:
        _update_source_prep_metadata(
            motet=motet,
            source_meta=source_meta,
            source_artifact_id=data.source_artifact_id,
            strategy_id=plan.strategy_id,
            strategy_version=plan.strategy_version,
            prep_state="prep_running",
        )
    except Exception as e:
        logger.warning(
            "artifact_rag_prep_metadata_update_failed",
            **motet.log_fields(
                source_artifact_id=data.source_artifact_id,
                strategy_id=plan.strategy_id,
                prep_state="prep_running",
                error=str(e),
                error_type=type(e).__name__,
            ),
            exc_info=True,
        )

    try:
        prep_result = ArtifactPrepExecutor().execute(strategy=selection.strategy, plan=selection.plan, context=context)
    except Exception:
        try:
            _update_source_prep_metadata(
                motet=motet,
                source_meta=source_meta,
                source_artifact_id=data.source_artifact_id,
                strategy_id=plan.strategy_id,
                strategy_version=plan.strategy_version,
                prep_state="prep_failed",
            )
        except Exception as e:
            logger.warning(
                "artifact_rag_prep_metadata_update_failed",
                **motet.log_fields(
                    source_artifact_id=data.source_artifact_id,
                    strategy_id=plan.strategy_id,
                    prep_state="prep_failed",
                    error=str(e),
                    error_type=type(e).__name__,
                ),
                exc_info=True,
            )
        raise
    chunks = prep_result.chunks
    if not chunks:
        try:
            _update_source_prep_metadata(
                motet=motet,
                source_meta=source_meta,
                source_artifact_id=data.source_artifact_id,
                strategy_id=prep_result.plan.strategy_id,
                strategy_version=prep_result.plan.strategy_version,
                prep_state=prep_result.prep_state,
            )
        except Exception as e:
            logger.warning(
                "artifact_rag_prep_metadata_update_failed",
                **motet.log_fields(
                    source_artifact_id=data.source_artifact_id,
                    strategy_id=prep_result.plan.strategy_id,
                    prep_state=prep_result.prep_state,
                    error=str(e),
                    error_type=type(e).__name__,
                ),
                exc_info=True,
            )
        return {
            "skipped": True,
            "reason": "no_prepared_chunks",
            "source_artifact_id": data.source_artifact_id,
            "derived_artifact_id": effective_derived_id,
            "derived_artifact_ids": prep_result.derived_artifact_ids,
            "strategy_id": prep_result.plan.strategy_id,
            "strategy_version": prep_result.plan.strategy_version,
            "prep_state": prep_result.prep_state,
            "prep_decision_source": prep_result.plan.prep_decision_source,
            "chunks_indexed": 0,
        }

    embedding_service = _worker_embedding_service(motet)
    embeddings = embedding_service.embed_batch([chunk.content_text for chunk in chunks])
    repository = ArtifactChunkRepository(
        embedding_dim=embedding_service.get_embedding_dimension(),
        native_text_mode=str(getattr(cfg, "artifact_rag_native_text_mode", "auto") if cfg is not None else "auto"),
    )
    written = repository.upsert_chunks(
        chunks,
        embeddings,
        replace_source=True,
    )

    try:
        _update_source_prep_metadata(
            motet=motet,
            source_meta=source_meta,
            source_artifact_id=data.source_artifact_id,
            strategy_id=prep_result.plan.strategy_id,
            strategy_version=prep_result.plan.strategy_version,
            prep_state=prep_result.prep_state,
        )
    except Exception as e:
        logger.warning(
            "artifact_rag_prep_metadata_update_failed",
            **motet.log_fields(
                source_artifact_id=data.source_artifact_id,
                strategy_id=prep_result.plan.strategy_id,
                prep_state=prep_result.prep_state,
                error=str(e),
                error_type=type(e).__name__,
            ),
            exc_info=True,
        )

    logger.info(
        "artifact_rag_indexed_text",
        **motet.log_fields(
            source_artifact_id=data.source_artifact_id,
            derived_artifact_id=effective_derived_id,
            strategy_id=prep_result.plan.strategy_id,
            chunks_indexed=written,
        ),
    )
    return {
        "source_artifact_id": data.source_artifact_id,
        "derived_artifact_id": effective_derived_id,
        "derived_artifact_ids": prep_result.derived_artifact_ids,
        "strategy_id": prep_result.plan.strategy_id,
        "strategy_version": prep_result.plan.strategy_version,
        "prep_state": prep_result.prep_state,
        "prep_decision_source": prep_result.plan.prep_decision_source,
        "chunks_indexed": written,
        "cache_hit": False,
    }


@motet.command(
    description="Retrieve scoped artifact chunks as citation-ready context text for answering questions about uploads.",
    timeout_seconds=45,
    required_capabilities=[WorkerCapability.VECTOR_OPERATIONS, WorkerCapability.EMBEDDINGS],
)
def rag_retrieve_context(data: RagRetrieveContextData) -> Dict[str, Any]:
    """Retrieve scoped artifact chunks and return citation-ready context text."""

    motet = get_motet_context()
    cfg = getattr(getattr(motet, "stack", None), "config", None)
    if cfg is not None and not bool(getattr(cfg, "artifact_rag_enabled", False)):
        return {
            "skipped": True,
            "reason": "artifact_rag_disabled",
            "chunks": [],
            "context_text": "",
        }

    from motet.core.rag import ArtifactChunkRepository, ArtifactRagRetriever, ArtifactRetrievalScope

    embedding_service = _worker_embedding_service(motet)
    repository = ArtifactChunkRepository(
        embedding_dim=embedding_service.get_embedding_dimension(),
        native_text_mode=str(getattr(cfg, "artifact_rag_native_text_mode", "auto") if cfg is not None else "auto"),
    )
    retriever = ArtifactRagRetriever(repository=repository, embedding_fn=embedding_service.embed)
    scope = ArtifactRetrievalScope(str(data.scope or "conversation"))
    top_k = data.top_k or int(getattr(cfg, "artifact_rag_top_k", 5) if cfg is not None else 5)
    similarity_threshold = (
        data.similarity_threshold
        if data.similarity_threshold is not None
        else float(getattr(cfg, "artifact_rag_similarity_threshold", 0.0) if cfg is not None else 0.0)
    )
    token_budget = data.token_budget or int(
        getattr(cfg, "artifact_rag_token_budget", 4000) if cfg is not None else 4000
    )
    hybrid_enabled = (
        data.hybrid_enabled
        if data.hybrid_enabled is not None
        else bool(getattr(cfg, "artifact_rag_hybrid_enabled", True) if cfg is not None else True)
    )
    vector_weight = (
        data.vector_weight
        if data.vector_weight is not None
        else float(getattr(cfg, "artifact_rag_vector_weight", 0.7) if cfg is not None else 0.7)
    )
    lexical_weight = (
        data.lexical_weight
        if data.lexical_weight is not None
        else float(getattr(cfg, "artifact_rag_lexical_weight", 0.3) if cfg is not None else 0.3)
    )
    candidate_multiplier = (
        data.candidate_multiplier
        if data.candidate_multiplier is not None
        else int(getattr(cfg, "artifact_rag_candidate_multiplier", 4) if cfg is not None else 4)
    )

    selection = retriever.retrieve(
        query_text=data.query_text,
        tenant_id=motet.tenant_id,
        motet_id=motet.motet_id,
        principal_id=motet.principal_id,
        role=data.role or "user",
        conversation_id=data.conversation_id or motet.conversation_id,
        scope=scope,
        artifact_ids=data.artifact_ids,
        artifact_tags=data.artifact_tags,
        top_k=top_k,
        similarity_threshold=similarity_threshold,
        token_budget=token_budget,
        hybrid_enabled=hybrid_enabled,
        vector_weight=vector_weight,
        lexical_weight=lexical_weight,
        candidate_multiplier=candidate_multiplier,
        position_ordered=bool(data.position_ordered),
    )
    return {
        "chunks": [chunk.model_dump(mode="json") for chunk in selection.chunks],
        "context_text": selection.context_text,
        "token_budget": selection.token_budget,
        "chunk_count": len(selection.chunks),
        "hybrid_enabled": hybrid_enabled,
    }


def rag_index_should_use_source_payload(motet: Any, *, source_artifact_id: str, derived_artifact_id: str) -> bool:
    """Return True when indexing should prepare the source bytes (office/JSON) rather than derived text."""

    from motet.core.artifacts.preparation import ArtifactPrepSelector
    from motet.core.artifacts.preparation.routing import should_prepare_source_instead_of_derived

    source_meta = motet.artifact_store.get_metadata(source_artifact_id)
    derived_meta = motet.artifact_store.get_metadata(derived_artifact_id)
    if not source_meta or not derived_meta:
        return False
    source_payload = motet.artifact_store.get(source_meta.id)
    derived_payload = motet.artifact_store.get(derived_artifact_id)
    if source_payload in (None, "", b"") or derived_payload in (None, "", b""):
        return False
    data = PrepareArtifactIndexData(source_artifact_id=source_artifact_id, derived_artifact_id=derived_artifact_id)
    smd = getattr(source_meta, "metadata", {}) or {}
    dmd = getattr(derived_meta, "metadata", {}) or {}
    tags = _metadata_tags(smd, dmd)
    cfg = getattr(getattr(motet, "stack", None), "config", None)
    selector = ArtifactPrepSelector()
    source_ctx = _build_prep_context_for_index(
        motet=motet,
        prepare_meta=source_meta,
        source_meta=source_meta,
        data=data,
        payload=source_payload,
        artifact_tags=tags,
        cfg=cfg,
    )
    derived_ctx = _build_prep_context_for_index(
        motet=motet,
        prepare_meta=derived_meta,
        source_meta=source_meta,
        data=data,
        payload=derived_payload,
        artifact_tags=tags,
        cfg=cfg,
    )
    return should_prepare_source_instead_of_derived(selector, source_context=source_ctx, derived_context=derived_ctx)


__all__ = [
    "prepare_artifact_index",
    "rag_retrieve_context",
    "PrepareArtifactIndexData",
    "RagRetrieveContextData",
]
