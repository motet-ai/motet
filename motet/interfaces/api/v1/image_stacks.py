"""
Motet - Image-stack registry API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Read-only operator surface for the platform image-stack registry. Returns
    the list of stacks the platform knows about (builtins + env-registered)
    so the ops UI can show which stacks exist, which are pinned, and which
    are placeholders awaiting an operator action.

    Stacks are platform configuration set via env vars (``MOTET_IMAGE_STACK_*``);
    there is intentionally no write endpoint here. Tenant-scoped configuration
    happens at bundle-publish time via ``config/exec.yaml`` ``base_image_stack``,
    not at this surface.

    Endpoints:
    - GET /api/v1/exec/image-stacks  — list all registered stacks
"""

from __future__ import annotations

from typing import List

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ....core.execution.image_stacks import ImageStack, list_image_stacks
from ....core.types import Principal
from ..shared.auth import get_current_principal

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/exec", tags=["exec"])


class ImageStackResponse(BaseModel):
    """Single image-stack registry entry as returned to the ops UI."""

    name: str = Field(..., description="Stack name as used in config/exec.yaml base_image_stack")
    oci_image_ref: str = Field(
        "",
        description=(
            "Resolved OCI image ref (recommended image@sha256:...). Empty "
            "string means the stack is registered but unpinned and operators "
            "must set MOTET_IMAGE_STACK_<NAME> before bundles can build "
            "against it."
        ),
    )
    description: str = Field("", description="Human-readable summary")
    builtin: bool = Field(
        False,
        description="True for stacks Motet ships knowing about by default.",
    )
    is_pinned: bool = Field(
        False,
        description=(
            "Convenience flag: True when oci_image_ref is non-empty. Note: "
            "this does not validate digest-pinning (image@sha256:...). "
            "Digest enforcement is gated by MOTET_REQUIRE_DIGEST_PINNED_PUBLISH."
        ),
    )


class ImageStacksListResponse(BaseModel):
    """Response shape for GET /api/v1/exec/image-stacks."""

    stacks: List[ImageStackResponse] = Field(
        default_factory=list,
        description="All registered stacks, sorted by name. Builtins always present.",
    )


def _to_response(stack: ImageStack) -> ImageStackResponse:
    return ImageStackResponse(
        name=stack.name,
        oci_image_ref=stack.oci_image_ref,
        description=stack.description,
        builtin=stack.builtin,
        is_pinned=stack.is_pinned,
    )


@router.get(
    "/image-stacks",
    response_model=ImageStacksListResponse,
    summary="List platform image stacks",
)
async def get_image_stacks(
    _principal: Principal = Depends(get_current_principal),
) -> ImageStacksListResponse:
    """Return all image stacks the platform currently knows about.

    Authentication is required (this is operator-facing data); no role gate
    beyond authentication is enforced here because the registry contents are
    not tenant-private and the same data is visible to anyone who can see
    deployed bundles via /api/v1/deploy.
    """
    stacks = list_image_stacks()
    return ImageStacksListResponse(stacks=[_to_response(s) for s in stacks])
