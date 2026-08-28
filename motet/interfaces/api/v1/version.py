"""
Motet - Stack Version API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Authenticated read of Motet product versions across the running stack.
    Reports the API process version, each registered worker's stamped
    version, configured sibling services (embedding-server, mcp-manager),
    and a skew flag when any reported version is missing or disagrees
    with the API. Used by operators and ``motet-cli version`` to catch
    mixed-version deploys.

Dependencies:
    - fastapi: REST surface
    - httpx: Short-timeout probes of sibling health endpoints
    - pydantic: Response models
    - motet._version: Product version of this API process
    - motet.core.config: Sibling discovery (embedding / MCP manager endpoints)
    - motet.core.distributed.worker_readiness: Per-worker stamped versions
    - interfaces.api.shared.auth: Principal authentication

Usage:
    GET /api/v1/version

Notes:
    - Requires authentication (JWT, service account, or API key).
    - ``GET /health`` stays a cheap liveness probe and does not include versions.
    - Workers without ``motet_version`` (not yet restarted onto this build)
      count as skew.
    - A configured sibling that is unreachable or missing ``motet_version``
      also counts as skew. Unconfigured siblings are omitted.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...._version import get_version
from ....core.types import Principal
from ..shared.auth import get_current_principal

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/version", tags=["version"])

SIBLING_PROBE_TIMEOUT_SECONDS = 2.0
MCP_MANAGER_HEALTH_PORT = 9091


class WorkerVersionEntry(BaseModel):
    """One registered worker's Motet product version."""

    worker_id: str = Field(
        ...,
        description="Worker registry id",
        json_schema_extra={"example": "worker-1"},
    )
    motet_version: Optional[str] = Field(
        default=None,
        description="Motet product version stamped at registration; omitted when unknown",
        json_schema_extra={"example": "0.1.0"},
    )
    state: str = Field(
        ...,
        description="Current worker readiness state",
        json_schema_extra={"example": "ready"},
    )


class SiblingVersionEntry(BaseModel):
    """One configured Motet sibling process (embedding-server or mcp-manager)."""

    id: str = Field(
        ...,
        description="Sibling id (embedding-server or mcp-manager)",
        json_schema_extra={"example": "embedding-server"},
    )
    motet_version: Optional[str] = Field(
        default=None,
        description="Motet product version from the sibling health payload; omitted when unknown",
        json_schema_extra={"example": "0.1.0"},
    )
    reachable: bool = Field(
        ...,
        description="True when the sibling health endpoint returned JSON",
        json_schema_extra={"example": True},
    )


class StackVersionResponse(BaseModel):
    """API version plus worker and sibling versions and a fleet skew flag."""

    api: str = Field(
        ...,
        description="Motet product version of this API process",
        json_schema_extra={"example": "0.1.0"},
    )
    workers: List[WorkerVersionEntry] = Field(
        default_factory=list,
        description="Registered workers and the Motet version each process reported",
    )
    siblings: List[SiblingVersionEntry] = Field(
        default_factory=list,
        description=(
            "Configured Motet siblings (embedding-server, mcp-manager) probed "
            "via their health endpoints"
        ),
    )
    skew: bool = Field(
        ...,
        description=(
            "True when any worker or configured sibling is unreachable, missing "
            "a version, or reports a version other than the API process"
        ),
        json_schema_extra={"example": False},
    )


def normalize_motet_version(value: Any) -> Optional[str]:
    """Return a product version string, or None when the process has not stamped one."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"None", "null"}:
        return None
    return text


def worker_state_value(worker: Any) -> str:
    """Return the readiness state as a lowercase string."""
    state = getattr(worker, "state", None)
    if state is None:
        return "unknown"
    value = getattr(state, "value", state)
    return str(value)


def sibling_health_url(sibling_id: str, endpoint: Optional[str]) -> Optional[str]:
    """Build a health URL from a configured sibling endpoint, or None if unset."""
    raw = (endpoint or "").strip()
    if not raw:
        return None
    if sibling_id == "embedding-server":
        if "://" not in raw:
            raw = f"http://{raw}"
        parsed = urlparse(raw)
        path = (parsed.path or "").rstrip("/")
        if path.endswith("/healthz"):
            return raw.rstrip("/")
        return raw.rstrip("/") + "/healthz"
    if sibling_id == "mcp-manager":
        if "://" not in raw:
            host_part = raw.rsplit("]", 1)[-1]
            if ":" in host_part:
                raw = f"http://{raw}"
            else:
                raw = f"http://{raw}:{MCP_MANAGER_HEALTH_PORT}"
        parsed = urlparse(raw)
        path = (parsed.path or "").rstrip("/")
        if path.endswith("/health"):
            return raw.rstrip("/")
        return raw.rstrip("/") + "/health"
    return None


def configured_sibling_targets(cfg: Any = None) -> List[Tuple[str, str]]:
    """Return ``(sibling_id, health_url)`` for siblings the API is configured to use."""
    if cfg is None:
        from ....core.config import Config

        cfg = Config()
    targets: List[Tuple[str, str]] = []
    embedding = sibling_health_url(
        "embedding-server", getattr(cfg, "embedding_endpoint", None)
    )
    if embedding:
        targets.append(("embedding-server", embedding))
    manager = sibling_health_url(
        "mcp-manager", getattr(cfg, "mcp_manager_endpoint", None)
    )
    if manager:
        targets.append(("mcp-manager", manager))
    return targets


def stack_has_skew(api_version: str, reported_versions: Sequence[Optional[str]]) -> bool:
    """True when any reported version is missing or differs from ``api_version``."""
    return any(version is None or version != api_version for version in reported_versions)


def build_stack_version(
    api_version: str,
    workers: Mapping[str, Any],
    siblings: Optional[Sequence[SiblingVersionEntry]] = None,
) -> StackVersionResponse:
    """Assemble the stack version payload from API, workers, and sibling probes."""
    entries: List[WorkerVersionEntry] = []
    for worker_id, worker in workers.items():
        entries.append(
            WorkerVersionEntry(
                worker_id=str(worker_id),
                motet_version=normalize_motet_version(getattr(worker, "motet_version", None)),
                state=worker_state_value(worker),
            )
        )
    entries.sort(key=lambda entry: entry.worker_id)
    sibling_entries = list(siblings or [])
    sibling_entries.sort(key=lambda entry: entry.id)
    reported: List[Optional[str]] = [entry.motet_version for entry in entries]
    for sibling in sibling_entries:
        if not sibling.reachable:
            reported.append(None)
        else:
            reported.append(sibling.motet_version)
    return StackVersionResponse(
        api=api_version,
        workers=entries,
        siblings=sibling_entries,
        skew=stack_has_skew(api_version, reported),
    )


async def probe_sibling(sibling_id: str, url: str) -> SiblingVersionEntry:
    """GET a sibling health URL and extract ``motet_version``."""
    try:
        async with httpx.AsyncClient(timeout=SIBLING_PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
            payload = response.json()
        if not isinstance(payload, dict):
            logger.warning(
                "sibling_version_probe_unexpected_payload",
                sibling_id=sibling_id,
                url=url,
                status_code=response.status_code,
            )
            return SiblingVersionEntry(id=sibling_id, motet_version=None, reachable=True)
        return SiblingVersionEntry(
            id=sibling_id,
            motet_version=normalize_motet_version(payload.get("motet_version")),
            reachable=True,
        )
    except Exception as exc:
        logger.warning(
            "sibling_version_probe_failed",
            sibling_id=sibling_id,
            url=url,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return SiblingVersionEntry(id=sibling_id, motet_version=None, reachable=False)


async def probe_configured_siblings(
    targets: Optional[Sequence[Tuple[str, str]]] = None,
) -> List[SiblingVersionEntry]:
    """Probe every configured sibling health endpoint in parallel."""
    resolved = list(targets) if targets is not None else configured_sibling_targets()
    if not resolved:
        return []
    return list(await asyncio.gather(*(probe_sibling(sid, url) for sid, url in resolved)))


@router.get(
    "",
    response_model=StackVersionResponse,
    summary="Inspect Motet versions across the running stack",
    description=(
        "Returns the Motet product version of this API process, each registered "
        "worker's stamped version, configured sibling versions (embedding-server "
        "and mcp-manager), and ``skew=true`` when any worker or configured "
        "sibling is unreachable, missing a version, or disagrees with the API."
    ),
    response_description="API, worker, and sibling versions plus a skew flag",
    responses={
        200: {
            "description": "Stack version snapshot",
            "content": {
                "application/json": {
                    "example": {
                        "api": "0.1.0",
                        "workers": [
                            {
                                "worker_id": "worker-1",
                                "motet_version": "0.1.0",
                                "state": "ready",
                            }
                        ],
                        "siblings": [
                            {
                                "id": "embedding-server",
                                "motet_version": "0.1.0",
                                "reachable": True,
                            },
                            {
                                "id": "mcp-manager",
                                "motet_version": "0.1.0",
                                "reachable": True,
                            },
                        ],
                        "skew": False,
                    }
                }
            },
        },
        401: {"description": "Authentication required"},
        500: {"description": "Failed to read worker registry"},
    },
)
async def get_stack_version(
    principal: Principal = Depends(get_current_principal),
) -> StackVersionResponse:
    """Return API, worker, and sibling Motet versions, plus a skew flag."""
    try:
        from ....core.distributed.worker_readiness import get_readiness_service

        readiness_service = get_readiness_service()
        workers: Dict[str, Any] = readiness_service.get_all_workers()
    except Exception as exc:
        logger.error(
            "Failed to collect stack version",
            principal_id=getattr(principal, "id", None),
            error=str(exc),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=f"Failed to collect stack version: {exc}") from exc
    siblings = await probe_configured_siblings()
    return build_stack_version(get_version(), workers, siblings)
