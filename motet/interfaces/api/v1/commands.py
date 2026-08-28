"""
Motet - Commands API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    REST API for inspecting and invoking registered distributed commands.

    Provides:
    - GET  /api/v1/commands            — list all registered command types
    - GET  /api/v1/commands/{type}     — inspect a specific command (schema, metadata)
    - POST /api/v1/commands/{type}/execute — invoke a command synchronously (for testing)

Dependencies:
    - fastapi: Web framework
    - motet.core.commands.command_type_registry: Command introspection
    - motet.core.workers: global_invoker for command execution
    - interfaces.api.shared.auth: Principal authentication
    - motet.core.bundles.deploy: bundle catalog (commands + capabilities)

Usage:
    from motet.interfaces.api.v1.commands import router
    app.include_router(router)

Notes:
    - The execute endpoint blocks until the command completes, making it
      straightforward for testing and debugging individual commands.
    - Command schema is derived from the data_class Pydantic model (if registered).
    - List/detail payloads include ``description`` from CommandRegistration
      (discovery/help prose) when present. Catalog-only bundle rows use
      ``command_descriptions`` / ``command_schemas`` from the Redis bundle
      catalog (descriptions from AST extract; schemas merged from AI-worker
      reload acks after import). Older catalogs may soft-fill descriptions
      from the shared function-discovery manifest.
    - Bundle deployment is handled by /api/v1/deploy.
    - Bundle execute applies catalog ``command_capabilities`` so CapabilityFilter
      routes edge-bound commands (e.g. app-builder.index_docs) to edge workers.
    - Execute allocates a conversation_id (``api-exec-<uuid>``) when the client
      omits one, so cost tracking and child workflow steps stay correlated.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..shared.auth import get_current_principal
from ....core.types import Principal

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/commands", tags=["commands"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ExecuteCommandRequest(BaseModel):
    """Request body for POST /api/v1/commands/{command_type}/execute."""

    data: Dict[str, Any] = Field(
        default_factory=dict,
        description="Command data payload matching the command's data_class schema",
        json_schema_extra={"example": {"query": "What is the weather?"}},
    )
    conversation_id: Optional[str] = Field(
        None,
        description=(
            "Conversation ID to associate with the execution. "
            "When omitted or empty, the API allocates ``api-exec-<uuid>`` so "
            "cost events and child commands share a correlation id."
        ),
        json_schema_extra={"example": "conv-abc123"},
    )
    timeout_seconds: int = Field(
        default=60,
        description="Execution timeout in seconds",
        json_schema_extra={"example": 60},
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _resolve_execute_conversation_id(req: ExecuteCommandRequest) -> str:
    """Use the client conversation_id when present; otherwise allocate one."""
    cid = (req.conversation_id or "").strip()
    if cid:
        return cid
    allocated = f"api-exec-{uuid.uuid4()}"
    logger.info(
        "execute_conversation_id_allocated",
        conversation_id=allocated,
    )
    return allocated


def _registration_to_dict(reg: Any) -> Dict[str, Any]:
    """Convert a CommandRegistration to a JSON-serialisable dict."""
    schema: Optional[Dict[str, Any]] = None
    if reg.data_class is not None:
        try:
            schema = reg.data_class.model_json_schema()
        except Exception:
            schema = None

    description = str(getattr(reg, "description", "") or "").strip()
    return {
        "command_type": reg.command_type,
        "implementation_type": (
            reg.implementation_type.value
            if hasattr(reg.implementation_type, "value")
            else str(reg.implementation_type)
        ),
        "version": reg.version,
        "bundle_id": reg.bundle_id,
        "description": description or None,
        "metadata": reg.metadata or {},
        "data_schema": schema,
    }


# ---------------------------------------------------------------------------
# Request-time targeting (ADR-0071 §5)
# ---------------------------------------------------------------------------


def _targeting_allows_context(
    targeting: Optional[Dict[str, Any]],
    motet_id: Optional[str],
    tenant_id: Optional[str],
) -> bool:
    """
    Return True if the request context (motet_id, tenant_id) is allowed by
    the bundle's targeting. Empty or missing targeting means global (allow).
    """
    if not targeting:
        return True
    motet_ids = targeting.get("motet_ids") or []
    tenant_ids = targeting.get("tenant_ids") or []
    if not motet_ids and not tenant_ids:
        return True
    motet_ok = not motet_ids or (motet_id or "") in motet_ids
    tenant_ok = not tenant_ids or (tenant_id or "") in tenant_ids
    return motet_ok and tenant_ok


def _get_targeting_for_command(
    command_type: str,
    redis_client: Any,
    command_type_registry: Any,
) -> Optional[Dict[str, Any]]:
    """
    Return targeting dict for a command (from local registry metadata or bundle catalog).
    """
    reg = command_type_registry.get(command_type)
    if reg and (reg.metadata or {}).get("targeting") is not None:
        return reg.metadata.get("targeting")
    from motet.core.bundles.deploy import _list_all_catalogs
    try:
        catalogs = _list_all_catalogs(redis_client)
    except Exception:
        return None
    for catalog in catalogs.values():
        if command_type in catalog.get("commands", []):
            return catalog.get("targeting") or None
    return None


# ---------------------------------------------------------------------------
# Bundle catalog helpers
# ---------------------------------------------------------------------------


def _find_in_catalogs(redis_client: Any, command_type: str) -> Optional[str]:
    """
    Search bundle catalogs (bundle:catalog:*) for command_type.

    Tries exact match first (user passed the namespaced form, e.g. 'hello-world.hello_world').
    Falls back to suffix match so bare names (e.g. 'hello_world') still resolve when
    exactly one bundle owns that command name.

    Returns the resolved namespaced command type, or None if not found.
    """
    from motet.core.bundles.deploy import _list_all_catalogs

    try:
        catalogs = _list_all_catalogs(redis_client)
    except Exception:
        return None

    # Exact match
    for catalog in catalogs.values():
        if command_type in catalog.get("commands", []):
            return command_type

    # Suffix match — bare name lookup
    matches = [
        ct
        for catalog in catalogs.values()
        for ct in catalog.get("commands", [])
        if ct == command_type or ct.endswith(f".{command_type}")
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        logger.warning(
            "bundle_command_ambiguous",
            command_type=command_type,
            matches=matches,
        )
        return matches[0]  # prefer first alphabetically (already sorted in catalog)
    return None


def _get_catalog_command_capabilities(
    redis_client: Any,
    resolved_command_type: str,
) -> List[str]:
    """Return required capability values for a namespaced bundle command from Redis catalogs."""
    from motet.core.bundles.deploy import _list_all_catalogs

    try:
        catalogs = _list_all_catalogs(redis_client)
    except Exception as e:
        logger.warning(
            "bundle_command_capabilities_lookup_failed",
            command_type=resolved_command_type,
            error=str(e),
        )
        return []

    for catalog in catalogs.values():
        caps_map = catalog.get("command_capabilities") or {}
        if not isinstance(caps_map, dict):
            continue
        caps = caps_map.get(resolved_command_type)
        if isinstance(caps, list) and caps:
            return [str(c).strip() for c in caps if str(c).strip()]
    return []


def _command_descriptions_from_discovery_manifest(
    redis_client: Any,
) -> Dict[str, str]:
    """
    Soft fallback: map command_type → description from the shared discovery index.

    Used when a Redis bundle catalog predates ``command_descriptions`` so the
    manage UI can still show prose without requiring an immediate redeploy.
    """
    try:
        from motet.core.tools.function_discovery_vector_store import (
            FunctionDiscoveryVectorStore,
        )

        from motet.core.distributed.tenant_keys import first_existing_key, product_key

        manifest_key = first_existing_key(
            redis_client, product_key("function_discovery:manifest")
        )
        raw = redis_client.get(manifest_key) if manifest_key else None
        payload = FunctionDiscoveryVectorStore._decode_manifest(raw)
        if not payload:
            return {}
        entries = payload.get("id_to_entry") or {}
        out: Dict[str, str] = {}
        for entry in entries.values():
            if not isinstance(entry, dict) or entry.get("type") != "command":
                continue
            ct = str(entry.get("command_type") or "").strip()
            desc = str(entry.get("description") or "").strip()
            if ct and desc:
                out[ct] = desc
        return out
    except Exception as e:
        logger.debug(
            "command_descriptions_discovery_fallback_failed",
            error=str(e),
            error_type=type(e).__name__,
        )
        return {}


def _get_bundle_catalog_commands(
    redis_client: Any,
    motet_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Return a list of command-info dicts for all bundle-contributed commands,
    sourced from bundle:catalog:* keys in Redis.
    If motet_id/tenant_id are provided, only includes commands whose targeting
    allows that context (ADR-0071 §5 request-time filter).
    """
    from motet.core.bundles.deploy import _list_all_catalogs

    try:
        catalogs = _list_all_catalogs(redis_client)
    except Exception:
        return []

    discovery_descs: Optional[Dict[str, str]] = None
    items = []
    for bundle_id, catalog in sorted(catalogs.items()):
        targeting = catalog.get("targeting") or {}
        if not _targeting_allows_context(targeting, motet_id, tenant_id):
            continue
        catalog_descs = catalog.get("command_descriptions") or {}
        if not isinstance(catalog_descs, dict):
            catalog_descs = {}
        catalog_schemas = catalog.get("command_schemas") or {}
        if not isinstance(catalog_schemas, dict):
            catalog_schemas = {}
        for ct in catalog.get("commands", []):
            description = None
            raw_desc = catalog_descs.get(ct)
            if isinstance(raw_desc, str) and raw_desc.strip():
                description = raw_desc.strip()
            else:
                # Older catalogs omit command_descriptions; soft-fill from the
                # shared discovery index when available (best-effort).
                if discovery_descs is None:
                    discovery_descs = _command_descriptions_from_discovery_manifest(
                        redis_client
                    )
                hit = discovery_descs.get(ct)
                if isinstance(hit, str) and hit.strip():
                    description = hit.strip()
            raw_schema = catalog_schemas.get(ct)
            data_schema = raw_schema if isinstance(raw_schema, dict) else None
            items.append({
                "command_type": ct,
                "implementation_type": "BUNDLE",
                "version": catalog.get("bundle_version", ""),
                "bundle_id": bundle_id,
                "description": description,
                "metadata": {"bundle_id": bundle_id, "source": "bundle_catalog", "targeting": targeting},
                "data_schema": data_schema,
            })
    return items


async def _dispatch_bundle_command(
    resolved_command_type: str,
    req: "ExecuteCommandRequest",
    principal: "Principal",
    global_invoker: Any,
) -> JSONResponse:
    """
    Dispatch a bundle command by its namespaced type to workers via global_invoker.

    The command is registered on workers under its namespaced type (e.g.
    'hello-world.hello_world'). We build a minimal DistributedCommand subclass
    whose get_command_type() returns that type so the Celery routing and worker
    registry lookup both use the correct namespaced key.

    Required capabilities are loaded from the bundle catalog ``command_capabilities``
    map (populated at deploy time from ``@motet.command(required_capabilities=...)``)
    so CapabilityFilter can pin edge-bound commands to edge workers.
    """
    from ....core.distributed.redis_manager import get_sync_redis_client
    from motet.core.commands.distributed import DistributedCommand
    from motet.core.commands.base_command_data import BaseCommandData
    # EventPriority.NORMAL = 5 (avoid circular import via motet.core.events)
    _EVENT_PRIORITY_NORMAL = 5

    task_id = str(uuid.uuid4())
    conversation_id = _resolve_execute_conversation_id(req)
    try:
        redis_client = get_sync_redis_client()
        required_capabilities = _get_catalog_command_capabilities(
            redis_client, resolved_command_type
        )
    except Exception as e:
        logger.warning(
            "bundle_dispatch_capabilities_unavailable",
            command_type=resolved_command_type,
            error=str(e),
        )
        required_capabilities = []

    class _RawData(BaseCommandData):
        model_config = {"extra": "allow"}

    # Capture the resolved type in a closure-safe way
    _resolved_type = resolved_command_type
    _timeout = req.timeout_seconds

    class _BundleDispatch(DistributedCommand):
        """Transient command class for dispatching a bundle command from the API tier."""

        @classmethod
        def _get_data_class(cls):
            return _RawData

        def get_command_type(self) -> str:
            return _resolved_type

        def _get_default_timeout(self) -> int:
            return _timeout

        def _get_default_priority(self) -> int:
            return _EVENT_PRIORITY_NORMAL

        def _do_execute(self, worker_context: Dict[str, Any]) -> Dict[str, Any]:
            # Never runs in the API process — executes on the worker
            raise NotImplementedError

        def can_undo(self) -> bool:
            return False

        async def undo(self, stack: Any) -> Any:
            raise NotImplementedError

    try:
        data_instance = _RawData.model_validate(req.data)
    except Exception as ve:
        raise HTTPException(status_code=422, detail=f"Invalid data payload: {ve}")

    command = _BundleDispatch(
        task_id=task_id,
        conversation_id=conversation_id,
        tenant_id=getattr(principal, "tenant_id", "") or "",
        principal_id=getattr(principal, "id", "") or "",
        data=data_instance,
        required_capabilities=required_capabilities,
    )

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(global_invoker.execute_command, command),
            timeout=float(req.timeout_seconds),
        )
    except asyncio.TimeoutError:
        try:
            from motet.core.distributed.task_control import request_task_cancel

            request_task_cancel(
                task_id,
                reason=f"API timeout after {req.timeout_seconds}s",
                principal_id=getattr(principal, "id", None),
                source="api_timeout",
                tenant_id=getattr(principal, "tenant_id", None),
            )
        except Exception as cancel_err:
            logger.warning(
                "bundle_command_timeout_cancel_failed",
                task_id=task_id,
                error=str(cancel_err),
            )
        raise HTTPException(
            status_code=408,
            detail=f"Bundle command '{resolved_command_type}' timed out after {req.timeout_seconds}s",
        )
    except Exception as e:
        msg = str(e).lower()
        if any(x in msg for x in ("no workers", "not found", "unknown command", "no route")):
            raise HTTPException(
                status_code=404,
                detail=f"Bundle command '{resolved_command_type}' not found on any live worker: {e}",
            )
        raise HTTPException(status_code=500, detail=f"Bundle command dispatch failed: {e}")

    return JSONResponse({
        "command_type": resolved_command_type,
        "task_id": task_id,
        "conversation_id": conversation_id,
        "result": result,
    })


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    summary="List registered command types",
    description="Returns all command types currently registered in the command type registry on this worker.",
    responses={
        200: {"description": "List of registered command types"},
        401: {"description": "Unauthorized"},
    },
)
async def list_commands(
    implementation_type: Optional[str] = Query(
        None,
        description="Filter by implementation type: CLASS_BASED, DECORATOR_BASED, or BUNDLE",
        json_schema_extra={"example": "DECORATOR_BASED"},
    ),
    bundle_id: Optional[str] = Query(None, description="Filter by bundle_id (manifest name)"),
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """
    List all command types registered in the command type registry.

    Includes both core commands (registered in this process) and bundle-contributed
    commands sourced from the bundle content catalog in Redis.

    Useful for discovering available commands, verifying bundle deployments
    loaded the expected command types, and generating test payloads.
    """
    try:
        from motet.core.commands.command_type_registry import (
            command_type_registry,
            CommandImplementationType,
        )

        filter_type: Optional[CommandImplementationType] = None
        want_bundle_only = False
        if implementation_type:
            if implementation_type.upper() == "BUNDLE":
                want_bundle_only = True
            else:
                try:
                    filter_type = CommandImplementationType[implementation_type.upper()]
                except KeyError:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Unknown implementation_type '{implementation_type}'. Use CLASS_BASED, DECORATOR_BASED, or BUNDLE.",
                    )

        motet_id = getattr(principal, "motet_id", None) or ""
        tenant_id = getattr(principal, "tenant_id", None) or ""
        commands: List[Dict[str, Any]] = []

        if not want_bundle_only:
            types = command_type_registry.get_command_types(filter_type=filter_type, bundle_id=bundle_id)
            for ct in sorted(types):
                reg = command_type_registry.get(ct)
                if reg:
                    targeting = (reg.metadata or {}).get("targeting")
                    if not _targeting_allows_context(targeting, motet_id, tenant_id):
                        continue
                    commands.append(_registration_to_dict(reg))
                else:
                    commands.append({"command_type": ct})

        # Merge bundle-contributed commands from the content catalog (request-time filter applied)
        if filter_type is None or want_bundle_only:
            try:
                from ....core.distributed.redis_manager import get_sync_redis_client
                redis_client = get_sync_redis_client()
                bundle_commands = _get_bundle_catalog_commands(
                    redis_client, motet_id=motet_id, tenant_id=tenant_id
                )
                if bundle_id:
                    bundle_commands = [c for c in bundle_commands if c.get("bundle_id") == bundle_id]
                # Deduplicate: skip catalog entries for types already in local registry
                local_types = {c["command_type"] for c in commands}
                for bc in bundle_commands:
                    if bc["command_type"] not in local_types:
                        commands.append(bc)
            except Exception as catalog_err:
                logger.warning("list_commands_catalog_fetch_failed", error=str(catalog_err))

        return JSONResponse({"commands": commands, "total": len(commands)})

    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_commands_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list commands: {e}")


@router.get(
    "/{command_type}",
    summary="Get command details",
    description=(
        "Returns full metadata and data schema for a registered command type. "
        "The data_schema field is the JSON Schema derived from the command's Pydantic data class "
        "and can be used to construct a valid execute payload."
    ),
    responses={
        200: {"description": "Command registration details"},
        401: {"description": "Unauthorized"},
        404: {"description": "Command type not found"},
    },
)
async def get_command(
    command_type: str,
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """
    Inspect a specific command type — metadata, version, and data schema.

    The `data_schema` JSON Schema field shows the exact payload shape expected
    by POST /api/v1/commands/{command_type}/execute.
    """
    try:
        from motet.core.commands.command_type_registry import command_type_registry

        reg = command_type_registry.get(command_type)
        if not reg:
            raise HTTPException(status_code=404, detail=f"Command type '{command_type}' not found")

        return JSONResponse(_registration_to_dict(reg))

    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_command_failed", command_type=command_type, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get command: {e}")


@router.post(
    "/{command_type}/execute",
    summary="Execute a command",
    description=(
        "Invoke a registered command by type synchronously (blocks until completion). "
        "Designed for testing and debugging — not for production orchestration workflows, "
        "which should use the chat API / agent_turn instead."
    ),
    responses={
        200: {"description": "Command result"},
        401: {"description": "Unauthorized"},
        404: {"description": "Command type not found"},
        408: {"description": "Execution timed out"},
        422: {"description": "Invalid data payload"},
        500: {"description": "Command failed"},
    },
)
async def execute_command(
    command_type: str,
    req: ExecuteCommandRequest,
    principal: Principal = Depends(get_current_principal),
) -> JSONResponse:
    """
    Execute a command and return its result synchronously.

    Use GET /api/v1/commands/{command_type} first to retrieve the `data_schema`
    so you know the exact shape of the `data` payload.

    Bundle commands (loaded on workers via POST /api/v1/deploy) are resolved via
    the bundle content catalog in Redis. The catalog maps the
    namespaced command type (e.g. 'hello-world.hello_world') to its bundle and
    the bare name also resolves when exactly one bundle owns it.
    """
    try:
        from motet.core.commands.command_type_registry import command_type_registry
        from ....core.workers import global_invoker
        from ....core.distributed.redis_manager import get_sync_redis_client

        motet_id = getattr(principal, "motet_id", None) or ""
        tenant_id = getattr(principal, "tenant_id", None) or ""

        reg = command_type_registry.get(command_type)
        if not reg:
            # Fall back to bundle catalog lookup — the command may be registered
            # on workers but not in this API process's local registry.
            try:
                from ....core.distributed.redis_manager import get_sync_redis_client
                redis_client = get_sync_redis_client()
                resolved_type = _find_in_catalogs(redis_client, command_type)
            except Exception:
                resolved_type = None

            if not resolved_type:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"Command type '{command_type}' not found in local registry or bundle catalog. "
                        "If this is a bundle command, use the namespaced form (e.g. 'bundle-id.command_name') "
                        "or deploy the bundle first."
                    ),
                )
            # Dispatch guard: check targeting allows this context (ADR-0071 §5)
            redis_client = get_sync_redis_client()
            targeting = _get_targeting_for_command(resolved_type, redis_client, command_type_registry)
            if not _targeting_allows_context(targeting, motet_id, tenant_id):
                raise HTTPException(
                    status_code=403,
                    detail="Command not available in this motet/tenant context.",
                )
            return await _dispatch_bundle_command(resolved_type, req, principal, global_invoker)

        # Dispatch guard for locally registered command (ADR-0071 §5)
        targeting = (reg.metadata or {}).get("targeting")
        if not _targeting_allows_context(targeting, motet_id, tenant_id):
            raise HTTPException(
                status_code=403,
                detail="Command not available in this motet/tenant context.",
            )

        data_instance: Any
        if reg.data_class is not None:
            try:
                data_instance = reg.data_class.model_validate(req.data)
            except Exception as ve:
                raise HTTPException(status_code=422, detail=f"Invalid data payload: {ve}")
        else:
            data_instance = req.data

        task_id = str(uuid.uuid4())
        conversation_id = _resolve_execute_conversation_id(req)
        tenant_id = getattr(principal, "tenant_id", "") or ""
        principal_id = getattr(principal, "id", "") or ""

        impl = reg.implementation
        try:
            command = impl(
                task_id=task_id,
                conversation_id=conversation_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
                data=data_instance,
            )
        except TypeError:
            try:
                command = impl(task_id=task_id, conversation_id=conversation_id, data=data_instance)
            except Exception as ce:
                raise HTTPException(status_code=500, detail=f"Failed to construct command: {ce}")

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(global_invoker.execute_command, command),
                timeout=float(req.timeout_seconds),
            )
        except asyncio.TimeoutError:
            try:
                from motet.core.distributed.task_control import request_task_cancel

                request_task_cancel(
                    task_id,
                    reason=f"API timeout after {req.timeout_seconds}s",
                    principal_id=principal_id,
                    source="api_timeout",
                    tenant_id=tenant_id,
                )
            except Exception as cancel_err:
                logger.warning(
                    "command_timeout_cancel_failed",
                    task_id=task_id,
                    error=str(cancel_err),
                )
            raise HTTPException(
                status_code=408,
                detail=f"Command '{command_type}' timed out after {req.timeout_seconds}s",
            )

        return JSONResponse({
            "command_type": command_type,
            "task_id": task_id,
            "conversation_id": conversation_id,
            "result": result,
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error("execute_command_failed", command_type=command_type, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Command execution failed: {e}")
