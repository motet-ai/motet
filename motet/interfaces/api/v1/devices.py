"""
Motet - Devices API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    REST API for registering and managing edge worker devices: issue device tokens,
    provision WireGuard peer configuration, list devices, revoke access, and
    deregister edge workers from readiness after a local stop.

    registration response includes WireGuard tunnel config and cloud
    Valkey URL so edge workers can connect directly via tunnel.

    (Phase D): registration accepts an optional explicit worker_id so
    remote app-builder instances can register as edge_app_builder_<app>;
    malformed ids return 400, ids already claimed by an active device return 409.

Dependencies:
    - fastapi: Router and HTTP exceptions
    - motet.core.edge.device_registry: EdgeDeviceRegistry
    - motet.core.distributed.worker_readiness: WorkerReadinessService (edge deregister)
    - motet.core.config: configuration (Valkey URL, WireGuard endpoint)

Usage:
    from motet.interfaces.api.v1.devices import router

Notes:
    - Device tokens are secrets; returned once at registration
    - WireGuard private key is generated server-side and returned once
    - POST /workers/{worker_id}/deregister is device-scoped (device token or
      owning principal); it does not replace admin worker terminate
"""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
import structlog

from ..shared.auth import get_current_principal
from ....core.config import Config
from ....core.edge.device_registry import (
    COMMAND_SCOPE_PRINCIPAL,
    COMMAND_SCOPE_TENANT,
    DeviceRecord,
    EdgeDeviceRegistry,
    validate_worker_id,
)
from ....core.types import Principal

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/devices", tags=["devices"])


def _derive_vault_resolve_url(request: Request, cfg: Config) -> str:
    """Derive the vault resolve URL for local workers (ADR-0095)."""
    configured = getattr(cfg, "vault_resolve_url", None)
    if configured:
        return str(configured).rstrip("/")
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/v1/vault/resolve"


def _bearer_token(request: Request) -> str:
    """Extract Bearer token from Authorization header, if present."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return ""


async def _get_device_or_user_principal(request: Request) -> Principal:
    """Authenticate with an edge device token (ld_...) or a normal principal.

    Device tokens are preferred when the Bearer value starts with ``ld_`` so
    ``motet-cli device stop`` can deregister readiness without ``motet-admin``.
    """
    token = _bearer_token(request)
    if token.startswith("ld_"):
        session = EdgeDeviceRegistry().verify_token(token)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked device token",
            )
        return Principal(
            id=session.principal_id,
            tenant_id=session.tenant_id,
            roles=["device"],
            claims={
                "auth_type": "edge_device_token",
                "device_id": session.device_id,
                "worker_id": session.worker_id,
            },
        )
    return await get_current_principal(request)


def _normalize_edge_worker_id(worker_id: str) -> str:
    """Normalize path worker id to canonical ``edge_*`` form."""
    wid = (worker_id or "").strip()
    # Legacy Celery hostname prefix sometimes appeared as cloud_edge_*.
    if wid.startswith("cloud_edge_"):
        wid = wid[len("cloud_") :]
    try:
        return validate_worker_id(wid)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e


def _principal_owns_edge_worker(principal: Principal, worker_id: str) -> bool:
    """True if principal may remove this edge worker from readiness."""
    claims = principal.claims or {}
    if claims.get("auth_type") == "edge_device_token":
        return str(claims.get("worker_id") or "") == worker_id

    roles = principal.roles or []
    if "motet-admin" in roles:
        return True

    reg = EdgeDeviceRegistry()
    for device in reg.list_devices(principal.id, tenant_id=principal.tenant_id):
        if device.revoked:
            continue
        if device.worker_id == worker_id:
            return True

    # Readiness ownership covers the case where the device was already revoked
    # but the edge worker entry is still present after a local stop.
    from ....core.distributed.worker_readiness import WorkerReadinessService

    info = WorkerReadinessService().get_worker_info(worker_id)
    if info is not None and info.owner_principal_id == principal.id:
        return True
    return False


def _derive_valkey_url(cfg: Config) -> Optional[str]:
    """Return the tunnel-reachable Valkey URL for local workers.

    MOTET_WIREGUARD_VALKEY_URL takes precedence — it should be the IP/host
    reachable through the WireGuard tunnel (e.g. redis://172.28.0.10:6379/0
    for local dev, or redis://10.0.1.50:6379/0 for a VPC endpoint in prod).
    Falls back to the API's own redis_url if unset.
    """
    return cfg.wireguard_valkey_url or cfg.redis_url


def _generate_wireguard_keypair() -> tuple[str, str]:
    """Generate a WireGuard keypair (private_key, public_key) using subprocess.

    Falls back to Python cryptography if wg is not available.
    """
    import subprocess

    try:
        privkey_proc = subprocess.run(
            ["wg", "genkey"], capture_output=True, text=True, check=True
        )
        private_key = privkey_proc.stdout.strip()
        pubkey_proc = subprocess.run(
            ["wg", "pubkey"],
            input=private_key,
            capture_output=True,
            text=True,
            check=True,
        )
        public_key = pubkey_proc.stdout.strip()
        return private_key, public_key
    except (FileNotFoundError, subprocess.CalledProcessError):
        import base64
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            PublicFormat,
        )

        key = X25519PrivateKey.generate()
        raw_private = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        raw_public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return base64.b64encode(raw_private).decode(), base64.b64encode(raw_public).decode()


def _allocate_peer_ip(subnet_cidr: str) -> str:
    """Allocate next available peer IP from the WireGuard subnet.

    Skips .0 (network) and .1 (server). In production this should be
    backed by a Redis counter for atomicity; here we use a simple INCR.
    """
    from ....core.distributed.redis_manager import get_sync_redis_client

    network = ipaddress.IPv4Network(subnet_cidr, strict=False)
    r = get_sync_redis_client("device_registry")
    counter_key = "edge_device:wg_peer_ip_counter"
    next_offset = int(r.incr(counter_key))  # type: ignore[arg-type]
    # .0 = network, .1 = server, peers start at .2
    host_offset = next_offset + 1
    hosts = list(network.hosts())
    if host_offset >= len(hosts):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WireGuard peer IP pool exhausted",
        )
    return str(hosts[host_offset])


def _resolve_wg_server_public_key(cfg: Config) -> str:
    """Return the WireGuard server public key, falling back to a mounted file.

    Production: set MOTET_WIREGUARD_SERVER_PUBLIC_KEY directly.
    Local dev: the linuxserver/wireguard container auto-generates the keypair
    into its config volume.  Mount that volume into the API container and point
    MOTET_WIREGUARD_SERVER_PUBLICKEY_FILE at the key file (default:
    /config/wg_server/server/publickey-server).
    """
    from_env = cfg.wireguard_server_public_key or ""
    if from_env.strip():
        return from_env.strip()

    key_file = cfg.wireguard_server_publickey_file or ""
    if not key_file.strip():
        return ""

    from pathlib import Path

    p = Path(key_file.strip())
    if not p.is_file():
        logger.warning(
            "wireguard_publickey_file_not_found",
            path=str(p),
            hint="WireGuard server may not have started yet",
        )
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception as exc:
        logger.warning("wireguard_publickey_file_read_error", path=str(p), error=str(exc))
        return ""


class DeviceRegistrationRequest(BaseModel):
    """Request to register a local device."""

    device_name: str = Field(
        default="local-device",
        description="Human-readable device label",
        json_schema_extra={"example": "matt-macbook"},
    )
    command_scope: str = Field(
        default=COMMAND_SCOPE_PRINCIPAL,
        description=(
            "Which commands this device accepts. "
            "'principal' (default) — only from the registering user. "
            "'tenant' — from any user in the same tenant."
        ),
        json_schema_extra={"example": "principal"},
    )
    worker_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional explicit worker id. Must match "
            "^edge_[a-z0-9][a-z0-9_.-]{0,58}$ and be unclaimed. Used by remote "
            "app-builder instances so the id matches the multi-app instancing "
            "scheme. Default derives edge_<uuid8> from the device id."
        ),
        json_schema_extra={"example": "edge_app_builder_myapp"},
    )


class DeviceCredentials(BaseModel):
    """Credentials for authenticating the local worker."""

    device_token: str = Field(
        ...,
        description="Secret device token (save securely; shown once)",
        json_schema_extra={"example": "ld_..."},
    )


class DeviceWorkerDeregisterResponse(BaseModel):
    """Result of removing an edge worker from cloud readiness."""

    worker_id: str = Field(
        ...,
        description="Canonical edge worker id that was targeted",
        json_schema_extra={"example": "edge_ab12cd34"},
    )
    removed: bool = Field(
        ...,
        description="True if a readiness entry existed and was deleted",
        json_schema_extra={"example": True},
    )


class WireGuardConfig(BaseModel):
    """WireGuard tunnel configuration for local worker."""

    server_public_key: str = Field(..., description="WireGuard server public key")
    server_endpoint: str = Field(
        ...,
        description="WireGuard server UDP endpoint (host:port)",
        json_schema_extra={"example": "wg.motet.example.com:51820"},
    )
    client_private_key: str = Field(
        ...,
        description="Generated WireGuard private key for this device (shown once)",
    )
    client_address: str = Field(
        ...,
        description="Assigned tunnel IP for this device (CIDR)",
        json_schema_extra={"example": "10.0.100.2/32"},
    )
    allowed_ips: str = Field(
        default="10.0.0.0/16",
        description="VPC CIDR routed through tunnel",
    )
    dns: Optional[str] = Field(
        default=None,
        description="Optional DNS server inside VPC",
    )


class DeviceRegistrationResponse(BaseModel):
    """Response after registering a device."""

    device_id: str = Field(..., description="Stable device identifier", json_schema_extra={"example": "uuid"})
    worker_id: str = Field(
        ...,
        description="Logical Celery worker id for this device",
        json_schema_extra={"example": "edge_5a0aaf4d"},
    )
    principal_id: str = Field(..., description="Principal (user) id that owns this device")
    tenant_id: str = Field(..., description="Tenant id for this device")
    command_scope: str = Field(
        default=COMMAND_SCOPE_PRINCIPAL,
        description="Command acceptance scope: 'principal' or 'tenant'",
    )
    valkey_url: Optional[str] = Field(
        None,
        description="Cloud Valkey URL reachable through WireGuard tunnel",
        json_schema_extra={"example": "redis://10.0.1.50:6379/0"},
    )
    vault_resolve_url: Optional[str] = Field(
        None,
        description="HTTPS endpoint for vault credential resolution",
        json_schema_extra={"example": "https://api.motet.dev/api/v1/vault/resolve"},
    )
    wireguard: Optional[WireGuardConfig] = Field(
        None,
        description="WireGuard tunnel config; None if WireGuard not configured",
    )
    credentials: DeviceCredentials = Field(..., description="Device authentication credentials")


class DeviceInfo(BaseModel):
    """Device metadata (non-secret)."""

    device_id: str = Field(..., description="Device id")
    worker_id: str = Field(..., description="Worker id")
    device_name: str = Field(..., description="Device label")
    tenant_id: str = Field(..., description="Tenant id")
    command_scope: str = Field(default=COMMAND_SCOPE_PRINCIPAL, description="Command acceptance scope")
    created_at: str = Field(..., description="Registration time (ISO 8601)")
    revoked: bool = Field(..., description="Whether the device has been revoked")
    last_connected_at: Optional[str] = Field(
        default=None,
        description="Last successful connection time (ISO 8601), if known",
        json_schema_extra={"example": "2026-03-26T20:11:00Z"},
    )


@router.post(
    "/register",
    response_model=DeviceRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Device registered"},
        400: {"description": "Missing tenant or invalid request (e.g. malformed worker_id)"},
        401: {"description": "Unauthorized"},
        409: {"description": "Requested worker_id is already claimed by an active device"},
    },
)
async def register_device(
    body: DeviceRegistrationRequest,
    request: Request,
    principal: Principal = Depends(get_current_principal),
) -> DeviceRegistrationResponse:
    """Register a new local device for the current user.

    when the server has WireGuard configured, the response includes
    tunnel configuration (keypair, assigned IP, server endpoint) and the cloud
    Valkey URL.
    """
    cfg = Config()
    tenant_id = principal.tenant_id
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tenant ID is required to register a device",
        )
    if body.command_scope not in {COMMAND_SCOPE_PRINCIPAL, COMMAND_SCOPE_TENANT}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid command_scope: {body.command_scope!r}. Must be 'principal' or 'tenant'.",
        )

    wg_config: Optional[WireGuardConfig] = None
    wg_server_pubkey = _resolve_wg_server_public_key(cfg)
    wg_server_endpoint = cfg.wireguard_server_endpoint or ""
    wg_peer_subnet = cfg.wireguard_peer_subnet
    wg_allowed_ips = cfg.wireguard_allowed_ips

    wg_public_key: Optional[str] = None
    wg_peer_ip: Optional[str] = None

    if wg_server_pubkey and wg_server_endpoint:
        client_private_key, client_public_key = _generate_wireguard_keypair()
        peer_ip = _allocate_peer_ip(wg_peer_subnet)
        wg_public_key = client_public_key
        wg_peer_ip = peer_ip
        wg_config = WireGuardConfig(
            server_public_key=wg_server_pubkey,
            server_endpoint=wg_server_endpoint,
            client_private_key=client_private_key,
            client_address=f"{peer_ip}/32",
            allowed_ips=wg_allowed_ips,
        )

    reg = EdgeDeviceRegistry()
    try:
        device_id, worker_id, token = reg.register_device(
            principal_id=principal.id,
            tenant_id=tenant_id,
            device_name=body.device_name,
            command_scope=body.command_scope,
            worker_id=body.worker_id,
            wg_public_key=wg_public_key,
            wg_peer_ip=wg_peer_ip,
            wg_endpoint=wg_server_endpoint or None,
        )
    except ValueError as exc:
        # Explicit worker_id problems (ADR-0123 Phase D): malformed → 400,
        # already claimed → 409.
        conflict = "already in use" in str(exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT if conflict else status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    valkey_url = _derive_valkey_url(cfg) if wg_config else None
    vault_resolve_url = _derive_vault_resolve_url(request, cfg) if wg_config else None

    logger.info(
        "device_registered",
        principal_id=principal.id,
        device_id=device_id,
        wireguard=wg_config is not None,
    )
    return DeviceRegistrationResponse(
        device_id=device_id,
        worker_id=worker_id,
        principal_id=principal.id,
        tenant_id=tenant_id,
        command_scope=body.command_scope,
        valkey_url=valkey_url,
        vault_resolve_url=vault_resolve_url,
        wireguard=wg_config,
        credentials=DeviceCredentials(device_token=token),
    )


@router.get(
    "",
    response_model=List[DeviceInfo],
    responses={200: {"description": "List of devices"}, 401: {"description": "Unauthorized"}},
)
async def list_devices(principal: Principal = Depends(get_current_principal)) -> List[DeviceInfo]:
    """List registered devices for the current principal (non-revoked index)."""
    reg = EdgeDeviceRegistry()
    rows: List[DeviceRecord] = reg.list_devices(principal.id, tenant_id=principal.tenant_id)
    return [
        DeviceInfo(
            device_id=r.device_id,
            worker_id=r.worker_id,
            device_name=r.device_name,
            tenant_id=r.tenant_id,
            command_scope=r.command_scope,
            created_at=r.created_at,
            revoked=r.revoked,
            last_connected_at=r.last_connected_at,
        )
        for r in rows
        if not r.revoked
    ]


@router.delete(
    "/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={204: {"description": "Device revoked"}, 401: {"description": "Unauthorized"}},
)
async def revoke_device(device_id: str, principal: Principal = Depends(get_current_principal)) -> Response:
    """Revoke a device: invalidates its token and removes it from the active index."""
    reg = EdgeDeviceRegistry()
    ok = reg.revoke_device(principal.id, device_id, tenant_id=principal.tenant_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/workers/{worker_id}/deregister",
    response_model=DeviceWorkerDeregisterResponse,
    responses={
        200: {"description": "Edge worker readiness entry removed (or already absent)"},
        400: {"description": "Not a valid edge worker id"},
        401: {"description": "Unauthorized"},
        403: {"description": "Not the device owner / device token mismatch"},
    },
)
async def deregister_edge_worker(
    worker_id: str,
    principal: Principal = Depends(_get_device_or_user_principal),
) -> DeviceWorkerDeregisterResponse:
    """Remove an edge worker from cloud readiness after a local device stop.

    Unlike ``POST /api/v1/workers/{id}/terminate`` (admin lifecycle kill), this
    endpoint only clears readiness/health registry state for ``edge_*`` workers.
    Auth: device token for that worker, owning principal, or ``motet-admin``.
    Idempotent when the worker is already absent.
    """
    from ....core.distributed.worker_readiness import WorkerReadinessService

    canonical_id = _normalize_edge_worker_id(worker_id)
    if not _principal_owns_edge_worker(principal, canonical_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to deregister this edge worker",
        )

    readiness = WorkerReadinessService()
    existed = readiness.get_worker_info(canonical_id) is not None
    if existed:
        readiness.remove_worker(canonical_id)
        logger.info(
            "edge_worker_deregistered",
            worker_id=canonical_id,
            principal_id=principal.id,
            auth_type=(principal.claims or {}).get("auth_type", "principal"),
        )
    else:
        logger.info(
            "edge_worker_deregister_noop",
            worker_id=canonical_id,
            principal_id=principal.id,
        )

    return DeviceWorkerDeregisterResponse(worker_id=canonical_id, removed=existed)


__all__ = ["router"]
