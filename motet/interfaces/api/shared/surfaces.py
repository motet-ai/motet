"""
Motet - Shared Surface Catalog HTTP Helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    HTTP mapping for conversation surface catalog checks shared by chat and
    conversations APIs. Catalog membership is "does this surface exist?";
    agent allow-list checks stay in the chat path.

Dependencies:
    - fastapi: HTTPException for 400/500 mapping
    - motet.core.surfaces: require_existing_surface and catalog errors
    - structlog: unexpected Redis/catalog failures

Usage:
    from motet.interfaces.api.shared.surfaces import require_catalog_surface

    surface_id = require_catalog_surface(req.surface_id)

Notes:
    - Invalid or unknown ids become HTTP 400
    - Unexpected catalog errors become HTTP 500
    - Chat still applies agent_may_use_surface after this check
"""

from __future__ import annotations

from typing import Optional

import structlog
from fastapi import HTTPException

from motet.core.surfaces import (
    SurfaceNotFoundError,
    SurfaceRegistry,
    SurfaceValidationError,
    require_existing_surface,
)

logger = structlog.get_logger(__name__)


def require_catalog_surface(
    surface_id: str,
    *,
    registry: Optional[SurfaceRegistry] = None,
) -> str:
    """Return a normalized catalog surface id, or raise HTTP 400/500."""
    try:
        return require_existing_surface(surface_id, registry=registry)
    except HTTPException:
        raise
    except (SurfaceValidationError, SurfaceNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(
            "surface_catalog_validation_failed",
            surface_id=surface_id,
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Surface validation failed: {e}",
        ) from e
