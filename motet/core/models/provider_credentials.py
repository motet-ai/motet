"""
Motet - Provider API Key Presence

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Reports whether a model provider has an API key available (environment /
    config, then vault) without returning the secret. Used by the models list
    API so UIs can show which catalog entries are ready to call.

Dependencies:
    - motet.core.config: Provider ``*_api_key`` settings from the environment
    - motet.core.security.vault_client: Optional tenant/principal vault lookup
    - structlog: Debug logging when vault lookup fails (never logs key values)

Usage:
    from motet.core.models.provider_credentials import (
        provider_has_api_key,
        provider_requires_api_key,
    )

    if provider_requires_api_key("openai") and not provider_has_api_key("openai"):
        # Show in the picker but do not allow selection
        ...

Notes:
    - ``local`` and ``mock`` do not need a cloud API key.
    - Config is checked first so a list endpoint does not hit the vault when
      ``MOTET_*_API_KEY`` is already set.
    - Vault is consulted only when a command context with principal/tenant is
      provided and config has no key.
"""

from __future__ import annotations

from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)

PROVIDERS_WITHOUT_API_KEY = frozenset({"local", "mock"})


def provider_requires_api_key(provider: str) -> bool:
    """Return True when the provider needs a cloud API key to run inference."""
    return str(provider or "").strip().lower() not in PROVIDERS_WITHOUT_API_KEY


def provider_has_api_key(
    provider: str,
    *,
    command_context: Optional[Any] = None,
    cfg: Optional[Any] = None,
) -> bool:
    """
    Return True when an API key is configured for ``provider``.

    Checks config/environment first, then vault when ``command_context`` is set.
    Never returns or logs the key value.
    """
    resolved_provider = str(provider or "").strip()
    if not resolved_provider:
        return False

    resolved_cfg = cfg
    if resolved_cfg is None:
        from motet.core.config import Config

        resolved_cfg = Config()

    api_key_attr = f"{resolved_provider}_api_key"
    api_key = getattr(resolved_cfg, api_key_attr, None)
    if isinstance(api_key, str) and api_key.strip():
        return True

    if command_context is None:
        return False

    try:
        from motet.core.security.vault_client import get_vault_client

        vault_client = get_vault_client()
        vault_key = vault_client.get_api_key(resolved_provider, command_context)
        return bool(isinstance(vault_key, str) and vault_key.strip())
    except Exception as e:
        logger.debug(
            "provider_api_key_vault_lookup_failed",
            provider=resolved_provider,
            error=str(e),
            error_type=type(e).__name__,
        )
        return False
