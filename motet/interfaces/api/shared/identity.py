"""
Motet - Principal Propagation Helpers

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-18

Description:
    Helper utilities for wiring authenticated principal identity into runtime
    configuration and stack instances. Ensures downstream components receive
    verified principal_id, tenant_id, and motet_id values extracted at the API boundary.

Dependencies:
    - structlog: Structured logging
    - motet.core.config: Runtime configuration model
    - motet.core.types: Principal model definition
    - motet.core: MotetStack core interface (type checking only at import time)

Usage:
    from motet.interfaces.api.shared.identity import (
        apply_principal_to_config,
        attach_principal_to_stack,
        get_principal_context,
    )

    cfg = Config()
    apply_principal_to_config(cfg, principal)
    stack = MotetStack(cfg)
    attach_principal_to_stack(stack, principal)

    motet_id, tenant_id, principal_id = get_principal_context(principal)

Notes:
    - Helper functions are intentionally side-effect free beyond updating
      provided objects to avoid hidden configuration mutations.
    - Stack attachment stores the full Principal object so orchestrator logic
      can access both identifiers and claims without re-parsing JWTs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import structlog

from motet.core.config import Config
from motet.core.security.system_principals import SYSTEM_MOTET_ID, SYSTEM_TENANT_ID
from motet.core.types import Principal

if TYPE_CHECKING:
    from motet.core import MotetStack

logger = structlog.get_logger(__name__)


def get_principal_context(principal: Optional[Principal]) -> tuple[str, str, str]:
    """
    Return (motet_id, tenant_id, principal_id) from an authenticated principal.

    Single source of truth for principal-derived context used by conversations API,
    chat registry touch, and any other callers that need these three values.
    """
    if not principal:
        raise ValueError("principal is required for principal context")
    principal_id = (principal.id or "").strip()
    if not principal_id:
        raise ValueError("principal.id is required for principal context")
    return (
        principal.motet_id or SYSTEM_MOTET_ID,
        principal.tenant_id or SYSTEM_TENANT_ID,
        principal_id,
    )


def apply_principal_to_config(cfg: Config, principal: Principal) -> None:
    """Inject principal identity fields into Config prior to stack construction."""
    cfg.principal_id = principal.id
    cfg.tenant_id = principal.tenant_id
    cfg.motet_id = principal.motet_id


def attach_principal_to_stack(stack: "MotetStack", principal: Principal) -> None:
    """Attach principal metadata to an MotetStack instance for orchestrator access."""
    setattr(stack, "_principal", principal)
    logger.debug(
        "attached_principal_to_stack",
        principal_id=principal.id,
        tenant_id=principal.tenant_id,
        motet_id=principal.motet_id,
    )

