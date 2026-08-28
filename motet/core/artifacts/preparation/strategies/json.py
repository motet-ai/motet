"""
Motet - JSON Artifact Preparation Strategy

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Implements deterministic JSON/API/tool-output preparation for.
    The strategy chunks structured values by JSON pointer, emits compact text
    renderings for embeddings, and preserves the structured object payload for
    provenance and future rendering.

Dependencies:
    - json for payload parsing and compact rendering
    - motet.core.artifacts.preparation models for JsonCoord chunks

Usage:
    strategy = JsonPreparationStrategy()
    result = strategy.prepare(strategy.plan(context), context)

Notes:
    - JSON pointers follow RFC 6901 escaping.
    - Large arrays are summarized by item windows to avoid one huge chunk.
"""

from __future__ import annotations

import json
from typing import Any

from ..hashing import canonical_json_hash, chunk_cache_key, structured_content_hash
from ..models import (
    ArtifactFeatureMatch,
    ArtifactPrepManifest,
    ArtifactPrepPlan,
    ArtifactPrepResult,
    ArtifactPrepStep,
    JsonCoord,
    PreparedArtifactChunk,
)
from ..strategy import ArtifactPrepContext

JSON_STRATEGY_ID = "json_pointer"
JSON_STRATEGY_VERSION = "1.0.0"


def _escape_pointer_part(value: Any) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _parse_json_payload(payload: Any) -> Any:
    if isinstance(payload, (dict, list)):
        return payload
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="ignore")
    if isinstance(payload, str):
        return json.loads(payload)
    return json.loads(str(payload))


def _compact_text(pointer: str, value: Any) -> str:
    rendered = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(rendered) > 2400:
        rendered = rendered[:2400] + "...[truncated]"
    label = pointer or "/"
    return f"JSON pointer {label}\n{rendered}"


def _object_kind(pointer: str, value: Any, source_kind: str) -> str:
    if source_kind == "tool_artifact":
        return "tool_result"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "value"


def _walk_json(value: Any, *, pointer: str = "", max_depth: int = 3, depth: int = 0) -> list[tuple[str, Any]]:
    if depth >= max_depth or not isinstance(value, (dict, list)):
        return [(pointer, value)]
    if isinstance(value, dict):
        if not value:
            return [(pointer, value)]
        items: list[tuple[str, Any]] = []
        for key, child in value.items():
            child_pointer = f"{pointer}/{_escape_pointer_part(key)}" if pointer else f"/{_escape_pointer_part(key)}"
            if isinstance(child, (dict, list)):
                items.extend(_walk_json(child, pointer=child_pointer, max_depth=max_depth, depth=depth + 1))
            else:
                items.append((child_pointer, child))
        return items or [(pointer, value)]
    if not value:
        return [(pointer, value)]
    items = []
    for index, child in enumerate(value[:100]):
        child_pointer = f"{pointer}/{index}" if pointer else f"/{index}"
        if isinstance(child, (dict, list)):
            items.extend(_walk_json(child, pointer=child_pointer, max_depth=max_depth, depth=depth + 1))
        else:
            items.append((child_pointer, child))
    if len(value) > 100:
        items.append((f"{pointer}/100-" if pointer else "/100-", {"remaining_items": len(value) - 100}))
    return items


class JsonPreparationStrategy:
    """Built-in JSON pointer preparation strategy."""

    manifest = ArtifactPrepManifest(
        strategy_id=JSON_STRATEGY_ID,
        strategy_version=JSON_STRATEGY_VERSION,
        handles=[
            ArtifactFeatureMatch(kinds=["tool_artifact"], content_types=["application/json"], extensions=[".json"]),
            ArtifactFeatureMatch(content_types=["application/json"], extensions=[".json"]),
        ],
        priority=30,
        cost_class="cheap",
        produces_chunk_kinds=["json_object"],
        fallback_chain=["text_default"],
    )

    def plan(self, context: ArtifactPrepContext) -> ArtifactPrepPlan:
        max_depth = int(context.config.get("json_max_depth", 3))
        config_hash = canonical_json_hash({"strategy": JSON_STRATEGY_ID, "version": JSON_STRATEGY_VERSION, "max_depth": max_depth})
        return ArtifactPrepPlan(
            source_artifact_id=getattr(context.artifact, "id", None),
            strategy_id=JSON_STRATEGY_ID,
            strategy_version=JSON_STRATEGY_VERSION,
            prep_decision_source="dispatch",
            steps=[ArtifactPrepStep(name="chunk_json", parameters={"max_depth": max_depth})],
            expected_chunk_kinds=["json_object"],
            canonical_config_hash=config_hash,
        )

    def prepare(self, plan: ArtifactPrepPlan, context: ArtifactPrepContext) -> ArtifactPrepResult:
        from ....media.utils import normalize_to_bytes

        from ..hashing import effective_source_content_hash

        try:
            parsed = _parse_json_payload(context.payload)
        except Exception as e:
            return ArtifactPrepResult(plan=plan, prep_state="prep_failed", diagnostics=[f"json_parse_failed: {e}"])

        max_depth = int((plan.steps[0].parameters if plan.steps else {}).get("max_depth", 3))
        items = _walk_json(parsed, max_depth=max_depth)
        payload_bytes = normalize_to_bytes(context.payload)
        source_hash = str(context.payload_info.content_hash or "").strip() or effective_source_content_hash(
            declared_hash=None, payload_bytes=payload_bytes
        )
        cache_key = chunk_cache_key(
            source_content_hash=source_hash,
            strategy_id=plan.strategy_id,
            strategy_version=plan.strategy_version,
            canonical_config_hash=plan.canonical_config_hash,
        )
        source_kind = str(getattr(getattr(context.artifact, "kind", ""), "value", getattr(context.artifact, "kind", "")))
        src_id = (
            str(context.source_artifact_id).strip()
            if str(context.source_artifact_id or "").strip()
            else str(getattr(context.artifact, "source_artifact_id", None) or getattr(context.artifact, "id"))
        )
        tags = list(context.artifact_tags) if context.artifact_tags else list(
            (getattr(context.artifact, "metadata", {}) or {}).get("tags") or []
        )
        chunks: list[PreparedArtifactChunk] = []
        for index, (pointer, value) in enumerate(items):
            content_text = _compact_text(pointer, value)
            chunks.append(
                PreparedArtifactChunk(
                    source_artifact_id=src_id,
                    derived_artifact_id=None,
                    chunk_index=index,
                    chunk_kind="json_object",
                    content_text=content_text,
                    structured_payload=value if isinstance(value, dict) else {"value": value},
                    content_hash=structured_content_hash(content_text=content_text, structured_payload=value),
                    coordinates=JsonCoord(pointer=pointer or "", object_kind=_object_kind(pointer, value, source_kind)),
                    tenant_id=context.tenant_id,
                    principal_id=context.principal_id,
                    motet_id=context.motet_id,
                    role=context.role,
                    conversation_id=context.conversation_id,
                    content_type=context.payload_info.content_type,
                    filename=context.payload_info.filename,
                    artifact_tags=tags,
                    modality="structured",
                    confidence=1.0,
                    prep_strategy_id=plan.strategy_id,
                    prep_strategy_version=plan.strategy_version,
                    chunk_cache_key=cache_key,
                    created_at=float(getattr(context.artifact, "created_at", 0.0) or 0.0),
                    expires_at=getattr(context.artifact, "expires_at", None),
                )
            )
        return ArtifactPrepResult(
            plan=plan,
            prep_state="prep_complete" if chunks else "prep_failed",
            chunks=chunks,
            diagnostics=[] if chunks else ["empty_json"],
            chunk_cache_key=cache_key if chunks else "",
        )

