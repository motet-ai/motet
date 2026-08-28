"""
Motet - Artifact Playback Tokens

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Stateless, short-lived HMAC-signed playback tokens for artifact streaming. Browser media elements (<video>, <audio>) cannot
    attach Authorization headers to their src requests, so an authenticated
    client mints a token bound to a single artifact + resolved access scope
    and passes it as a query parameter to GET /api/v1/artifacts/{id}/stream.

    Token format: base64url(claims_json) + "." + hex(HMAC-SHA256(secret, claims_json))

    Claims carry the artifact id, the tenant/principal/motet scope resolved at
    mint time, and an expiry. Verification checks the signature, expiry, and
    artifact-id binding — the stream endpoint then reads its access scope from
    the verified claims rather than trusting the caller.

Dependencies:
    - hashlib/hmac/secrets (stdlib): signing and ephemeral secret generation
    - motet.core.config: optional MOTET_ARTIFACT_PLAYBACK_TOKEN_SECRET

Usage:
    from motet.core.artifacts.playback_tokens import (
        mint_playback_token, verify_playback_token, PlaybackTokenError,
    )

    token = mint_playback_token(
        artifact_id="art-1", tenant_id="t1", principal_id="p1",
        motet_id="m1", ttl_seconds=300,
    )
    claims = verify_playback_token(token, artifact_id="art-1")  # raises on failure

Notes:
    - The signing secret comes from Config.artifact_playback_token_secret;
      when unset, an ephemeral per-process secret is generated. Multi-process
      API deployments MUST set the secret explicitly or tokens minted on one
      process will not verify on another (documented in).
    - Tokens travel in query strings; keep TTLs short (default 300 s).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)

# Ephemeral fallback secret for single-process/dev deployments (see Notes).
_PROCESS_SECRET: bytes = secrets.token_bytes(32)


class PlaybackTokenError(Exception):
    """Raised when a playback token fails verification (signature, expiry, binding)."""


@dataclass(frozen=True)
class PlaybackTokenClaims:
    """Verified claims carried by a playback token."""

    artifact_id: str
    tenant_id: Optional[str]
    principal_id: Optional[str]
    motet_id: Optional[str]
    expires_at: float


def _resolve_secret(secret: Optional[str] = None) -> bytes:
    """Resolve the signing secret: explicit arg > config > per-process fallback."""
    if secret:
        return secret.encode("utf-8")
    try:
        from ..config import Config

        configured = getattr(Config(), "artifact_playback_token_secret", None)
        if configured:
            return str(configured).encode("utf-8")
    except Exception as e:
        logger.warning(
            "playback_token_config_unavailable",
            error=str(e),
            error_type=type(e).__name__,
        )
    return _PROCESS_SECRET


def _sign(payload: bytes, secret: bytes) -> str:
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def mint_playback_token(
    *,
    artifact_id: str,
    tenant_id: Optional[str],
    principal_id: Optional[str],
    motet_id: Optional[str],
    ttl_seconds: int,
    secret: Optional[str] = None,
) -> str:
    """Mint a signed playback token bound to one artifact and access scope.

    Args:
        artifact_id: Artifact the token grants streaming access to.
        tenant_id: Tenant scope resolved for the minting principal.
        principal_id: Principal scope (None for admin tenant-wide scope).
        motet_id: Motet/environment scope.
        ttl_seconds: Token lifetime in seconds.
        secret: Optional explicit secret (tests); defaults to config/process secret.

    Returns:
        Token string: base64url(claims_json).hex_signature
    """
    claims = {
        "aid": artifact_id,
        "tid": tenant_id,
        "pid": principal_id,
        "mid": motet_id,
        "exp": time.time() + max(1, int(ttl_seconds)),
    }
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    signature = _sign(payload, _resolve_secret(secret))
    return f"{encoded}.{signature}"


def verify_playback_token(
    token: str,
    *,
    artifact_id: str,
    secret: Optional[str] = None,
) -> PlaybackTokenClaims:
    """Verify a playback token and return its claims.

    Raises:
        PlaybackTokenError: malformed token, bad signature, expired, or bound
            to a different artifact.
    """
    try:
        encoded, signature = token.rsplit(".", 1)
        padding = "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(encoded + padding)
    except Exception as e:
        raise PlaybackTokenError("Malformed playback token") from e

    expected = _sign(payload, _resolve_secret(secret))
    if not hmac.compare_digest(signature, expected):
        raise PlaybackTokenError("Invalid playback token signature")

    try:
        claims = json.loads(payload)
    except Exception as e:
        raise PlaybackTokenError("Malformed playback token claims") from e

    expires_at = float(claims.get("exp") or 0)
    if time.time() >= expires_at:
        raise PlaybackTokenError("Playback token expired")

    if str(claims.get("aid") or "") != artifact_id:
        raise PlaybackTokenError("Playback token not valid for this artifact")

    return PlaybackTokenClaims(
        artifact_id=artifact_id,
        tenant_id=claims.get("tid"),
        principal_id=claims.get("pid"),
        motet_id=claims.get("mid"),
        expires_at=expires_at,
    )
