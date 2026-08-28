"""
Motet - Skills API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-15

Description:
    REST API for inspecting installed Agent Skills from deployed bundle catalogs.
    Provides a top-level resource shape for the Manage UI and motet-cli instead
    of requiring clients to flatten /api/v1/deploy responses themselves.

Dependencies:
    - fastapi: Web framework for REST API
    - motet.core.bundles.deploy: bundle catalog Redis helpers
    - interfaces.api.shared.auth: Principal authentication

Usage:
    from motet.interfaces.api.v1.skills import router
    app.include_router(router)

Notes:
    - This endpoint reports installed bundle skills from Redis catalogs. Runtime
      per-turn activation remains model-driven through core.activate_skill.
    - List visibility uses the authenticated principal's tenant/motet
      (issue #214). A foreign tenant_id or motet_id filter returns 403
      unless the caller has global tenant access.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..shared.auth import get_current_principal, require_motet_access, require_tenant_access
from ....core.types import Principal

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/skills", tags=["skills"])


class SkillInfo(BaseModel):
    """One installed Agent Skill from a bundle catalog."""

    skill_id: str = Field(..., description="Canonical skill id", json_schema_extra={"example": "skills-vendor-demo.pdf"})
    name: str = Field(..., description="Skill name from SKILL.md", json_schema_extra={"example": "pdf"})
    description: str = Field(default="", description="Skill description from SKILL.md")
    source: str = Field(default="bundle_catalog", description="Where this skill listing came from")
    bundle_id: str = Field(..., description="Owning bundle id", json_schema_extra={"example": "skills-vendor-demo"})
    bundle_version: str = Field(default="", description="Owning bundle version or git tree SHA")
    path: str = Field(default="", description="Skill directory path inside the bundle")
    dir: str = Field(default="", description="Skill directory name inside the bundle")
    dir_matches_name: Optional[bool] = Field(default=None, description="Whether directory name matches SKILL.md name")
    base_image_stack: str = Field(default="", description="Bundle base image stack, if declared")
    runtime_capabilities: List[str] = Field(default_factory=list, description="Bundle runtime capabilities, if declared")
    requirements_path: str = Field(default="", description="Bundle Python requirements path, if declared")
    oci_image_ref: str = Field(default="", description="Pinned bundle execution image ref, if declared")
    execution_available: bool = Field(default=True, description="Whether workspace-shell execution is available after activation")
    targeting: Dict[str, Any] = Field(default_factory=dict, description="Bundle targeting metadata")


class SkillsListResponse(BaseModel):
    """Response for GET /api/v1/skills."""

    skills: List[SkillInfo] = Field(default_factory=list, description="Installed Agent Skills")
    total: int = Field(default=0, description="Total number of returned skills")


def _targeting_allows_context(
    targeting: Optional[Dict[str, Any]],
    motet_id: Optional[str],
    tenant_id: Optional[str],
) -> bool:
    """Return whether bundle targeting allows the request context."""
    if not targeting:
        return True
    motet_ids = targeting.get("motet_ids") or []
    tenant_ids = targeting.get("tenant_ids") or []
    if not motet_ids and not tenant_ids:
        return True
    motet_ok = not motet_ids or (motet_id or "") in motet_ids
    tenant_ok = not tenant_ids or (tenant_id or "") in tenant_ids
    return motet_ok and tenant_ok


def _coerce_string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _coerce_string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _skill_rows_from_catalogs(
    catalogs: Dict[str, Dict[str, Any]],
    *,
    motet_id: Optional[str],
    tenant_id: Optional[str],
    bundle_id_filter: Optional[str],
) -> List[SkillInfo]:
    rows: List[SkillInfo] = []
    for bundle_id, catalog in sorted(catalogs.items()):
        if bundle_id_filter and bundle_id != bundle_id_filter:
            continue
        targeting = catalog.get("targeting") or {}
        if not _targeting_allows_context(targeting, motet_id, tenant_id):
            continue
        exec_meta = catalog.get("exec") if isinstance(catalog.get("exec"), dict) else {}
        for index, skill in enumerate(catalog.get("skills") or []):
            if not isinstance(skill, dict):
                continue
            name = _coerce_string(skill.get("name")) or _coerce_string(skill.get("dir")) or "skill"
            skill_id = _coerce_string(skill.get("id")) or f"{bundle_id}.{name}"
            rows.append(
                SkillInfo(
                    skill_id=skill_id,
                    name=name,
                    description=_coerce_string(skill.get("description")),
                    bundle_id=bundle_id,
                    bundle_version=_coerce_string(catalog.get("bundle_version")),
                    path=_coerce_string(skill.get("path")),
                    dir=_coerce_string(skill.get("dir")),
                    dir_matches_name=(
                        bool(skill.get("dir_matches_name"))
                        if skill.get("dir_matches_name") is not None
                        else None
                    ),
                    base_image_stack=_coerce_string(exec_meta.get("base_image_stack")),
                    runtime_capabilities=_coerce_string_list(exec_meta.get("runtime_capabilities")),
                    requirements_path=_coerce_string(exec_meta.get("requirements_path")),
                    oci_image_ref=_coerce_string(exec_meta.get("oci_image_ref")),
                    targeting=targeting if isinstance(targeting, dict) else {},
                )
            )
    return sorted(rows, key=lambda row: (row.skill_id, row.bundle_id))


@router.get(
    "",
    response_model=SkillsListResponse,
    summary="List installed Agent Skills",
    description="List Agent Skills discovered from deployed bundle catalogs.",
    responses={
        200: {"description": "Installed skills"},
        401: {"description": "Unauthorized"},
        403: {"description": "Foreign tenant_id or motet_id without global scope"},
        500: {"description": "Failed to list skills"},
    },
)
async def list_skills(
    bundle_id: Optional[str] = Query(None, description="Optional bundle_id filter"),
    motet_id: Optional[str] = Query(
        None,
        description=(
            "Visibility filter by motet_id. Omitted uses the authenticated "
            "principal's motet. A different motet requires global tenant access."
        ),
    ),
    tenant_id: Optional[str] = Query(
        None,
        description=(
            "Visibility filter by tenant_id. Omitted uses the authenticated "
            "principal's tenant. A different tenant requires global tenant access."
        ),
    ),
    principal: Principal = Depends(get_current_principal),
) -> SkillsListResponse:
    """List installed bundle-backed Agent Skills."""
    try:
        from ....core.distributed.redis_manager import get_sync_redis_client
        from motet.core.bundles.deploy import _list_all_catalogs

        redis_client = get_sync_redis_client()
        catalogs = _list_all_catalogs(redis_client)
        authorized_tenant = require_tenant_access(principal, tenant_id)
        authorized_motet = require_motet_access(principal, motet_id)
        rows = _skill_rows_from_catalogs(
            catalogs,
            motet_id=authorized_motet,
            tenant_id=authorized_tenant,
            bundle_id_filter=bundle_id,
        )
        return SkillsListResponse(skills=rows, total=len(rows))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "list_skills_failed",
            principal_id=getattr(principal, "id", None),
            error=str(exc),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=f"Failed to list skills: {exc}") from exc


__all__ = ["router"]
