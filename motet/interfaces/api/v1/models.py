"""
Motet - Models API

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Models API for the Motet distributed framework.
    Provides REST API endpoint for listing available language models,
    including whether each provider has an API key configured.

Dependencies:
    - fastapi: Web framework for REST API
    - motet.core.models: Model registry, specifications, and key-presence helpers
    - motet.core.security.auth: Optional principal extraction for vault key checks

Usage:
    from motet.interfaces.api.v1.models import router
    
    # Include in FastAPI app
    app.include_router(router)

Notes:
    - List endpoint is metadata-only and does not require authentication
    - When a principal is present, ``has_api_key`` also considers the vault
    - List responses never include secret values
"""

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
import structlog

from motet.core.types import Principal
from motet.interfaces.api.shared.auth import get_current_principal

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/models", tags=["models"])


class ModelInfo(BaseModel):
    """Model information."""
    provider: str = Field(..., description="Model provider", json_schema_extra={"example": "openai"})
    name: str = Field(..., description="Model selection key (use in model_name)", json_schema_extra={"example": "gpt-4o-mini"})
    model_id: str = Field(..., description="Unique model id for UI keys (same as name)", json_schema_extra={"example": "gpt-4o-mini"})
    display_name: str = Field(
        ...,
        description='User-friendly model display name for UI (e.g., "GPT-4o Mini").',
        json_schema_extra={"example": "GPT-4o Mini"},
    )
    capabilities: List[str] = Field(..., description="Model capabilities", json_schema_extra={"example": ["chat", "streaming", "function_calling"]})
    max_output_tokens: int = Field(..., description="Maximum output tokens", json_schema_extra={"example": 4096})
    supported_adapters: List[str] = Field(
        ...,
        description="Supported adapter/protocol names for this model.",
        json_schema_extra={"example": ["responses", "chat_completions"]},
    )
    default_adapter: str = Field(
        ...,
        description="Default adapter/protocol for this model.",
        json_schema_extra={"example": "responses"},
    )
    fallback_adapters: List[str] = Field(
        default_factory=list,
        description="Fallback adapters in priority order.",
        json_schema_extra={"example": ["chat_completions"]},
    )
    supported_builtin_tools: List[str] = Field(
        default_factory=list,
        description="Supported provider-native built-in tool names for this model.",
        json_schema_extra={"example": ["openai.web_search"]},
    )
    requires_api_key: bool = Field(
        ...,
        description="Whether this provider needs a cloud API key before the model can be called.",
        json_schema_extra={"example": True},
    )
    has_api_key: bool = Field(
        ...,
        description="Whether an API key is configured for this provider (environment or vault). Always false for providers that do not use a cloud key.",
        json_schema_extra={"example": True},
    )


class ModelProfileWrite(BaseModel):
    """Request body for creating/updating a model profile."""

    provider_defaults: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-provider default overrides. Values must match ModelRouteOverride fields.",
        json_schema_extra={"example": {"openai": {"adapter": "chat_completions", "tools_enabled": False}}},
    )
    model_overrides: Dict[str, Dict[str, Dict[str, Any]]] = Field(
        default_factory=dict,
        description="Per-provider per-model overrides. Values must match ModelRouteOverride fields.",
        json_schema_extra={"example": {"openai": {"gpt-4o-mini": {"adapter": "responses", "tool_allowlist": ["openai.web_search"]}}}},
    )


@router.get(
    "",
    summary="List available models",
    description="Get list of all available language models with their capabilities and whether a provider API key is configured",
    response_model=List[ModelInfo],
    response_description="List of available models"
)
async def list_models(request: Request):
    """
    List available models.
    
    Returns a list of all configured language models with their capabilities,
    providers, token limits, and whether a provider API key is configured.
    
    This endpoint does not require authentication as it only provides
    metadata about available models, not access to them. When the caller
    is authenticated, ``has_api_key`` also checks the vault for that
    principal and tenant.
    
    Returns:
        List of model information dictionaries
    """
    try:
        from motet.core.commands.base import CommandContext
        from motet.core.config import Config
        from motet.core.models.provider_credentials import (
            provider_has_api_key,
            provider_requires_api_key,
        )
        from motet.core.security.auth import extract_principal
        from ....core.models.registry import list_models_with_keys

        cfg = Config()
        principal = extract_principal(cfg, request)
        command_context: Optional[CommandContext] = None
        if principal and principal.id:
            command_context = CommandContext(
                task_id="models_list",
                tenant_id=principal.tenant_id or "",
                principal_id=principal.id,
                motet_id=principal.motet_id or "default",
                conversation_id="",
            )

        has_key_by_provider: Dict[str, bool] = {}

        def has_key_for(provider: str) -> bool:
            if provider not in has_key_by_provider:
                has_key_by_provider[provider] = provider_has_api_key(
                    provider,
                    command_context=command_context,
                    cfg=cfg,
                )
            return has_key_by_provider[provider]

        items = []
        for prov, registry_key, spec in list_models_with_keys():
            # Use registry_key as name so aliases (e.g. gpt-4o-mini-chat) have unique ids for UI keys.
            items.append({
                "provider": prov,
                "name": registry_key,
                "model_id": registry_key,
                "display_name": str(getattr(spec, "display_name", None) or spec.name),
                "capabilities": sorted(list(spec.capabilities)),
                "max_output_tokens": spec.max_output_tokens,
                "supported_adapters": list(getattr(spec, "supported_adapters", None) or []),
                "default_adapter": str(getattr(spec, "default_adapter", "") or ""),
                "fallback_adapters": list(getattr(spec, "fallback_adapters", None) or []),
                "supported_builtin_tools": list(getattr(spec, "supported_builtin_tools", None) or []),
                "requires_api_key": provider_requires_api_key(prov),
                "has_api_key": has_key_for(prov),
            })
        return items
        
    except Exception as e:
        logger.error("Failed to list models", error=str(e), exc_info=True)
        # Return empty list rather than error - allows graceful degradation
        return []


@router.get(
    "/profiles/{profile_name}",
    summary="Get model profile for current tenant",
    description="Fetch the Redis-backed model profile used for per-tenant/per-model routing and policy overrides.",
    response_description="Model profile document",
    responses={
        200: {"description": "Model profile"},
        401: {"description": "Authentication required"},
        404: {"description": "Profile not found"},
    },
)
async def get_model_profile(
    profile_name: str,
    principal: Principal = Depends(get_current_principal),
):
    """
    Get the current tenant's model profile by name.

    Args:
        profile_name: Profile name (e.g., "default")
    """
    from motet.core.models.profiles import load_model_profile

    tenant_id = principal.tenant_id or ""
    profile = await load_model_profile(tenant_id=tenant_id, profile_name=profile_name)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model profile not found")
    return profile.model_dump()


@router.put(
    "/profiles/{profile_name}",
    summary="Create or update model profile for current tenant",
    description="Store the Redis-backed model profile used for per-tenant/per-model routing and policy overrides.",
    response_description="Updated model profile document",
    responses={
        200: {"description": "Model profile updated"},
        401: {"description": "Authentication required"},
    },
)
async def put_model_profile(
    profile_name: str,
    body: ModelProfileWrite,
    principal: Principal = Depends(get_current_principal),
):
    """
    Create or update a model profile for the current tenant.

    Notes:
        - ModelSpec gates what a model *can* do; profiles control what we *choose* to do for a tenant.
    """
    from motet.core.models.profiles import ModelProfile, ModelRouteOverride, store_model_profile

    tenant_id = principal.tenant_id or ""

    provider_defaults = {
        prov: ModelRouteOverride.model_validate(v) for prov, v in (body.provider_defaults or {}).items()
    }
    model_overrides: Dict[str, Dict[str, ModelRouteOverride]] = {}
    for prov, by_model in (body.model_overrides or {}).items():
        model_overrides[prov] = {mn: ModelRouteOverride.model_validate(v) for mn, v in (by_model or {}).items()}

    profile = ModelProfile(
        name=profile_name,
        tenant_id=tenant_id,
        provider_defaults=provider_defaults,
        model_overrides=model_overrides,
    )
    await store_model_profile(profile=profile)
    return profile.model_dump()

