"""
Motet - HTTP Vault Client

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Vault client for edge workers that resolves credentials via the cloud
    vault HTTPS endpoint (POST /api/v1/vault/resolve). Uses a direct HTTP call
    through the WireGuard tunnel.

    The vault master key stays cloud-side. Edge workers send credential
    resolution requests authenticated with their device token.

Dependencies:
    - requests: HTTP client for vault API calls
    - os: Environment variable configuration
    - motet.core.security.vault_service: Credential enum types

Usage:
    from motet.core.edge.http_vault_client import HttpVaultClient
    client = HttpVaultClient()
    data = client.get_credential("openai_api_key", context)

Notes:
    - MOTET_VAULT_RESOLVE_URL must point to the cloud API (e.g. https://api.motet.dev/api/v1/vault/resolve)
    - MOTET_VAULT_AUTH_TOKEN is the device token from registration
    - Uses MOTET_EDGE_AUTH_TOKEN when MOTET_VAULT_AUTH_TOKEN is not set
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests
import structlog

from motet.core.security.vault_service import (
    CredentialScope,
    CredentialSecurityLevel,
    CredentialType,
)

logger = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 15.0


class HttpVaultClient:
    """Edge worker vault client that resolves credentials via HTTPS (ADR-0095)."""

    def __init__(
        self,
        *,
        resolve_url: Optional[str] = None,
        auth_token: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self.resolve_url = (
            resolve_url
            or os.getenv("MOTET_VAULT_RESOLVE_URL", "").strip()
        )
        if not self.resolve_url:
            raise RuntimeError(
                "HttpVaultClient requires MOTET_VAULT_RESOLVE_URL "
                "(e.g. https://api.motet.dev/api/v1/vault/resolve)"
            )
        self.auth_token = (
            auth_token
            or os.getenv("MOTET_VAULT_AUTH_TOKEN", "").strip()
            or os.getenv("MOTET_EDGE_AUTH_TOKEN", "").strip()
        )
        if not self.auth_token:
            raise RuntimeError(
                "HttpVaultClient requires MOTET_VAULT_AUTH_TOKEN (device token)"
            )
        self.timeout_seconds = float(
            timeout_seconds
            if timeout_seconds is not None
            else os.getenv("MOTET_VAULT_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_SECONDS))
        )

    def _resolve(
        self,
        credential_key: str,
        motet_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Call POST /api/v1/vault/resolve and return credential_data or None."""
        try:
            resp = requests.post(
                self.resolve_url,
                json={"credential_key": credential_key, "motet_id": motet_id},
                headers={
                    "Authorization": f"Bearer {self.auth_token}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout_seconds,
            )
            if resp.status_code == 401:
                logger.warning(
                    "http_vault_auth_failed",
                    credential_key=credential_key,
                    status_code=resp.status_code,
                )
                return None
            resp.raise_for_status()
            body = resp.json()
            if not body.get("ok"):
                logger.warning(
                    "http_vault_resolve_not_ok",
                    credential_key=credential_key,
                    error_code=body.get("error_code"),
                    error_message=body.get("error_message"),
                )
                return None
            if not body.get("found"):
                return None
            return body.get("credential_data")
        except requests.exceptions.Timeout:
            logger.warning(
                "http_vault_timeout",
                credential_key=credential_key,
                timeout_seconds=self.timeout_seconds,
            )
            return None
        except Exception as e:
            logger.error(
                "http_vault_resolve_error",
                credential_key=credential_key,
                error=str(e),
                exc_info=True,
            )
            return None

    def get_credential(
        self,
        credential_key: str,
        context: Any,
        credential_type: Optional[CredentialType] = None,
        required_scopes: Optional[List[CredentialScope]] = None,
    ) -> Optional[Dict[str, Any]]:
        _ = credential_type, required_scopes
        motet_id = getattr(context, "motet_id", None)
        return self._resolve(credential_key, motet_id=motet_id)

    def store_credential(
        self,
        credential_key: str,
        credential_data: Dict[str, Any],
        context: Any,
        credential_type: CredentialType = CredentialType.CUSTOM,
        scope: CredentialScope = CredentialScope.PRINCIPAL,
        security_level: CredentialSecurityLevel = CredentialSecurityLevel.CONFIDENTIAL,
    ) -> bool:
        _ = credential_key, credential_data, context, credential_type, scope, security_level
        logger.warning("http_vault_store_not_supported")
        return False

    def delete_credential(self, credential_key: str, context: Any) -> bool:
        _ = credential_key, context
        logger.warning("http_vault_delete_not_supported")
        return False

    def get_bearer_token(self, service_name: str, context: Any) -> Optional[str]:
        credential_data = self.get_credential(f"{service_name}_bearer_token", context)
        if credential_data:
            return credential_data.get("access_token") or credential_data.get("token")
        return None

    def get_api_key(self, service_name: str, context: Any) -> Optional[str]:
        credential_data = self.get_credential(f"{service_name}_api_key", context)
        if credential_data:
            return credential_data.get("api_key") or credential_data.get("key")
        return None

    def clear_cache(self, principal_id: Optional[str] = None) -> None:
        _ = principal_id

    def clear_cached_credential(self, principal_id: str, credential_key: str) -> None:
        _ = principal_id, credential_key


__all__ = ["HttpVaultClient"]
