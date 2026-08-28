"""
Motet - Shared Checkpoint Redis Helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    DRY Redis plumbing for turn and workflow checkpoint stores. Both stores persist nested JSON blobs with TTL and
    optional id→resource index entries; resume commands share principal
    re-authorization and conversation rebind. Domain models
    (TurnCheckpoint / WorkflowCheckpoint) stay separate — this module only
    owns storage shape and resume auth helpers.

Dependencies:
    - motet.core.distributed.redis_manager: store/retrieve + sync Redis client
    - structlog: Structured logging

Usage:
    from motet.core.checkpoints.redis_store import (
        scoped_key, store_json_blob, load_json_blob,
        write_id_index, lookup_id_index, clear_id_index,
        flatten_nested_blob, to_nested_blob,
        assert_checkpoint_principal, bind_resume_conversation,
        validate_handback_observations,
    )

Notes:
    - Must not import reasoning or orchestration (package cycle rule).
    - store_json_blob raises on failure; load_json_blob soft-fails to None.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import structlog

logger = structlog.get_logger(__name__)


def scoped_key(
    prefix: str,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    resource_id: str,
) -> str:
    """Build a tenant/motet-scoped Redis key: ``{tenant}:{prefix}:{tenant}:{motet}:{id}``."""
    from motet.core.distributed.tenant_keys import tenant_key

    tid = (tenant_id or "global").strip() or "global"
    logical = f"{prefix}:{tid}:{motet_id or 'default'}:{resource_id}"
    if tid == "global":
        return logical
    return tenant_key(tid, logical)


def flatten_nested_blob(
    data: Dict[str, Any],
    *,
    sections: Sequence[Tuple[Sequence[str], str]],
    id_field: str,
    schema_version: int,
    extras: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Accept nested sectioned blobs or legacy flat blobs as flat constructor kwargs.

    ``sections`` is a sequence of ``(field_names, section_name)``. When none of
    the section names appear in ``data``, the blob is treated as legacy flat.
    """
    if not isinstance(data, dict):
        return data
    section_names = {name for _, name in sections}
    if not any(name in data for name in section_names):
        return data

    flat: Dict[str, Any] = {
        id_field: data.get(id_field),
        "created_at": data.get("created_at"),
        "schema_version": data.get("schema_version", schema_version),
    }
    for extra in extras or ():
        if extra in data:
            flat[extra] = data[extra]
    for fields, section_name in sections:
        section = data.get(section_name)
        section = section if isinstance(section, dict) else {}
        for key in fields:
            if key in section:
                flat[key] = section[key]
            elif key in data:
                flat[key] = data[key]
    keep_none = {id_field, "created_at"}
    return {k: v for k, v in flat.items() if v is not None or k in keep_none}


def to_nested_blob(
    flat: Dict[str, Any],
    *,
    sections: Sequence[Tuple[Sequence[str], str]],
    id_field: str,
    schema_version: int,
    extras: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Build a nested Redis blob from a flat model dump."""
    nested: Dict[str, Any] = {
        "schema_version": schema_version,
        id_field: flat[id_field],
        "created_at": flat["created_at"],
    }
    for extra in extras or ():
        nested[extra] = flat.get(extra)
    for fields, section_name in sections:
        nested[section_name] = {key: flat.get(key) for key in fields}
    return nested


def store_json_blob(
    service: str,
    key: str,
    payload: Dict[str, Any],
    ttl_seconds: int,
    *,
    error_label: str,
) -> None:
    """Persist a JSON blob and set TTL. Raises RuntimeError on failure."""
    from motet.core.distributed.redis_manager import (
        get_sync_redis_client,
        store_structured_data_sync,
    )

    try:
        store_structured_data_sync(service, key, payload, format_type="json_string")
        client = get_sync_redis_client(service)
        client.expire(key, ttl_seconds)
    except Exception as e:
        logger.error(
            f"{error_label}_store_failed",
            key=key,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        raise RuntimeError(f"failed to persist {error_label}: {e}") from e


def load_json_blob(service: str, key: str, *, error_label: str) -> Optional[Dict[str, Any]]:
    """Load a JSON blob; returns None on miss or soft failure."""
    try:
        from motet.core.distributed.redis_manager import retrieve_structured_data_sync

        from motet.core.distributed.tenant_keys import already_tenant_prefixed_key

        candidates = [key]
        if already_tenant_prefixed_key(key) and ":" in key:
            candidates.append(key.split(":", 1)[1])
        data = None
        for candidate in candidates:
            data = retrieve_structured_data_sync(service, candidate, format_type="json_string")
            if isinstance(data, dict):
                return data
        return None
    except Exception as e:
        logger.warning(
            f"{error_label}_load_failed",
            key=key,
            error=str(e),
            error_type=type(e).__name__,
        )
        return None


def write_id_index(
    service: str,
    index_key: str,
    *,
    target_field: str,
    target_id: str,
    ttl_seconds: int,
) -> None:
    """Write a handle→resource index entry with the same TTL as the primary blob."""
    from motet.core.distributed.redis_manager import (
        get_sync_redis_client,
        store_structured_data_sync,
    )

    store_structured_data_sync(
        service,
        index_key,
        {target_field: target_id},
        format_type="json_string",
    )
    client = get_sync_redis_client(service)
    client.expire(index_key, ttl_seconds)


def lookup_id_index(
    service: str,
    index_key: str,
    *,
    target_field: str,
    error_label: str = "checkpoint_index",
) -> Optional[str]:
    """Resolve an index key to the target resource id."""
    data = load_json_blob(service, index_key, error_label=error_label)
    value = (data or {}).get(target_field)
    return str(value) if value else None


def clear_id_index(
    service: str,
    index_keys: Iterable[str],
    *,
    error_label: str = "checkpoint_index",
) -> None:
    """Delete index keys (best-effort)."""
    keys = [k for k in index_keys if k]
    if not keys:
        return
    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client

        client = get_sync_redis_client(service)
        for key in keys:
            client.delete(key)
    except Exception as e:
        logger.warning(
            f"{error_label}_clear_failed",
            key_count=len(keys),
            error=str(e),
            error_type=type(e).__name__,
        )


def assert_checkpoint_principal(
    recorded_principal: Optional[str],
    caller_principal: Optional[str],
    *,
    resource_label: str,
    resource_id: str,
) -> None:
    """Raise PermissionError when a non-empty recorded principal does not match caller."""
    recorded = str(recorded_principal or "").strip()
    caller = str(caller_principal or "").strip()
    if recorded and caller and caller != recorded:
        logger.warning(
            "checkpoint_principal_mismatch",
            resource_label=resource_label,
            resource_id=resource_id,
            recorded_principal=recorded,
            caller_principal=caller,
        )
        raise PermissionError(
            f"{resource_label}: '{resource_id}' belongs to a different principal"
        )


def bind_resume_conversation(
    motet: Any,
    conversation_id: Optional[str],
    *,
    log_context: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Rebind ``motet.conversation_id`` to the checkpoint conversation when needed.

    Facade clients often omit conversation headers on tool-result POSTs, so Motet
    mints a fresh id. Binding back preserves prompt-cache affinity and cost
    attribution with the suspend call.
    """
    cid = str(conversation_id or "").strip()
    if not cid:
        return None
    current = str(getattr(motet, "conversation_id", "") or "").strip()
    if current == cid:
        return cid

    ctx = dict(log_context or {})
    if isinstance(getattr(type(motet), "conversation_id", None), property):
        dctx = getattr(motet, "distributed_context", None)
        if dctx is not None and hasattr(dctx, "conversation_id"):
            dctx.conversation_id = cid
        if hasattr(motet, "_conversation_id_fallback"):
            motet._conversation_id_fallback = cid
    else:
        try:
            motet.conversation_id = cid
        except Exception as e:
            logger.warning(
                "resume_conversation_bind_failed",
                checkpoint_conversation_id=cid,
                request_conversation_id=current or None,
                error=str(e),
                error_type=type(e).__name__,
                **ctx,
            )
            return None

    if str(getattr(motet, "conversation_id", "") or "").strip() != cid:
        logger.warning(
            "resume_conversation_bind_failed",
            checkpoint_conversation_id=cid,
            request_conversation_id=current or None,
            bound_conversation_id=str(getattr(motet, "conversation_id", "") or "") or None,
            **ctx,
        )
        return None

    logger.info(
        "resume_conversation_rebound",
        from_conversation_id=current or None,
        to_conversation_id=cid,
        **ctx,
    )
    return cid


def validate_handback_observations(
    recorded_ids: Iterable[str],
    observations: List[Dict[str, Any]],
    *,
    exclude_ids: Optional[Iterable[str]] = None,
    id_field: str = "tool_call_id",
    error_prefix: str = "resume",
) -> Dict[str, Dict[str, Any]]:
    """Validate handback observations: known ids, no duplicates, none missing.

    ``exclude_ids`` are recorded ids the caller need not cover (e.g. Motet-owned
    execute-at-resume calls). Observations for excluded ids are discarded.
    Returns observations keyed by id.
    """
    recorded = {str(i) for i in recorded_ids if str(i or "").strip()}
    excluded = {str(i) for i in (exclude_ids or ()) if str(i or "").strip()}
    by_id: Dict[str, Dict[str, Any]] = {}
    for obs in observations:
        obs_id = str(obs.get(id_field) or "").strip()
        if not obs_id:
            raise ValueError(f"{error_prefix}: observation missing {id_field}")
        if obs_id not in recorded:
            raise ValueError(
                f"{error_prefix}: observation for unknown {id_field} '{obs_id}'"
            )
        if obs_id in excluded:
            logger.warning(
                "handback_observation_for_excluded_id_discarded",
                **{id_field: obs_id},
                error_prefix=error_prefix,
            )
            continue
        if obs_id in by_id:
            raise ValueError(
                f"{error_prefix}: duplicate observation for {id_field} '{obs_id}'"
            )
        by_id[obs_id] = obs

    missing = sorted(recorded - set(by_id) - excluded)
    if missing:
        raise ValueError(
            f"{error_prefix}: missing observations for {id_field}s: "
            + ", ".join(missing)
        )
    return by_id
