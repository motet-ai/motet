"""
Motet - CLI Authentication Helper

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Centralized authentication helper for CLI commands. Supports JWT tokens,
    service account tokens, and header-based authentication (dev mode).
    Provides credential storage and retrieval for seamless CLI usage.

Dependencies:
    - pathlib: File path handling
    - json: Credential storage
    - os: Environment variable access

Usage:
    from motet_sdk.cli._auth import get_api_headers, store_credentials
    
    headers = get_api_headers()
    response = requests.get(url, headers=headers)
    
    # Store credentials for future use
    store_credentials(sa_token="sa_20251122_...")

Notes:
    - Priority: JWT env var > Service account env var > Stored credentials > Headers
    - Credentials stored in ~/.motet/credentials.json with 600 permissions
    - Part of Week 2-3: CLI JWT Support
"""

import os
import json
from pathlib import Path
from typing import Dict, Optional

import structlog

logger = structlog.get_logger(__name__)


def get_credentials_path() -> Path:
    """Get path to stored credentials file."""
    return Path.home() / ".motet" / "credentials.json"


def get_api_headers() -> Dict[str, str]:
    """
    Get API headers - supports JWT, service accounts, and header auth.
    
    Priority:
    1. MOTET_JWT_TOKEN environment variable
    2. MOTET_SERVICE_ACCOUNT_TOKEN environment variable
    3. Stored credentials in ~/.motet/credentials.json
    4. MOTET_PRINCIPAL_ID/MOTET_TENANT_ID (dev mode headers)
    
    Returns:
        Dict with headers for API requests
    """
    headers = {"Content-Type": "application/json"}
    
    # API Key (always include if available)
    api_key = os.getenv("MOTET_API_KEY") or os.getenv("MOTET_API_KEY") or "dev-key"
    if api_key:
        headers["X-API-Key"] = api_key
    
    # 1. Try JWT token from environment
    jwt_token = os.getenv("MOTET_JWT_TOKEN")
    if jwt_token:
        headers["Authorization"] = f"Bearer {jwt_token}"
        logger.debug("Using JWT token from MOTET_JWT_TOKEN environment variable")
        return headers
    
    # 2. Try service account token from environment
    sa_token = os.getenv("MOTET_SERVICE_ACCOUNT_TOKEN")
    if sa_token:
        headers["Authorization"] = f"Bearer {sa_token}"
        logger.debug("Using service account token from MOTET_SERVICE_ACCOUNT_TOKEN environment variable")
        return headers
    
    # 3. Try stored credentials
    creds_path = get_credentials_path()
    if creds_path.exists():
        try:
            with open(creds_path) as f:
                creds = json.load(f)
            
            if "jwt_token" in creds and creds["jwt_token"]:
                headers["Authorization"] = f"Bearer {creds['jwt_token']}"
                logger.debug("Using JWT token from stored credentials")
                return headers
            elif "service_account_token" in creds and creds["service_account_token"]:
                headers["Authorization"] = f"Bearer {creds['service_account_token']}"
                logger.debug("Using service account token from stored credentials")
                return headers
        except Exception as e:
            logger.debug("Failed to read stored credentials", error=str(e))
            # Fall through to header auth
    
    # 4. Fallback to header auth (dev mode)
    principal_id = os.getenv("MOTET_PRINCIPAL_ID") or os.getenv("MOTET_PRINCIPAL_ID") or "cli-user"
    tenant_id = os.getenv("MOTET_TENANT_ID") or os.getenv("MOTET_TENANT_ID") or "default"
    motet_id = os.getenv("MOTET_MOTET_ID") or os.getenv("MOTET_MOTET_ID") or "default"
    
    headers["X-Principal-Id"] = principal_id
    headers["X-Tenant-Id"] = tenant_id
    headers["X-Motet-Id"] = motet_id
    
    logger.debug("Using header-based authentication (dev mode)", principal_id=principal_id, tenant_id=tenant_id, motet_id=motet_id)
    return headers


def store_credentials(jwt_token: Optional[str] = None, sa_token: Optional[str] = None) -> None:
    """
    Store credentials in ~/.motet/credentials.json.
    
    Args:
        jwt_token: JWT token to store (optional)
        sa_token: Service account token to store (optional)
    """
    creds_path = get_credentials_path()
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Load existing credentials if file exists
    creds = {}
    if creds_path.exists():
        try:
            with open(creds_path) as f:
                creds = json.load(f)
        except Exception:
            pass  # Start fresh if file is corrupted
    
    # Update with new credentials
    if jwt_token:
        creds["jwt_token"] = jwt_token
    if sa_token:
        creds["service_account_token"] = sa_token
    
    # Write credentials
    with open(creds_path, "w") as f:
        json.dump(creds, f, indent=2)
    
    # Set restrictive permissions (owner read/write only)
    creds_path.chmod(0o600)
    
    logger.info("Credentials stored", path=str(creds_path), has_jwt=bool(jwt_token), has_sa=bool(sa_token))


def clear_credentials() -> None:
    """Remove stored credentials."""
    creds_path = get_credentials_path()
    if creds_path.exists():
        creds_path.unlink()
        logger.info("Credentials cleared", path=str(creds_path))


def get_stored_token() -> Optional[str]:
    """
    Get stored token (JWT or service account) from credentials file.
    
    Returns:
        Token string if found, None otherwise
    """
    creds_path = get_credentials_path()
    if not creds_path.exists():
        return None
    
    try:
        with open(creds_path) as f:
            creds = json.load(f)
        
        # Prefer JWT over service account
        if "jwt_token" in creds and creds["jwt_token"]:
            return creds["jwt_token"]
        elif "service_account_token" in creds and creds["service_account_token"]:
            return creds["service_account_token"]
    except Exception:
        pass
    
    return None


__all__ = [
    "get_api_headers",
    "store_credentials",
    "clear_credentials",
    "get_stored_token",
    "get_credentials_path",
]

