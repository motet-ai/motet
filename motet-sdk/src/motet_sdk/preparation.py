"""
Motet SDK - Artifact Preparation Models

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Public models for bundle authors who register artifact preparation
tools with @motet.tool(prep_manifest=...). These SDK shapes intentionally avoid
runtime imports so bundles can construct manifests in local tests and CI.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


PrepCostClass = Literal["cheap", "moderate", "expensive"]
ChunkKind = Literal[
    "text",
    "section",
    "table",
    "code_symbol",
    "json_object",
    "ocr_region",
    "caption",
    "transcript_segment",
    "video_scene",
]


class ArtifactFeatureMatch(BaseModel):
    """Matcher declaring artifact features handled by a prep strategy."""

    kinds: list[str] = Field(default_factory=list, description="Artifact kind values")
    content_types: list[str] = Field(default_factory=list, description="Exact or wildcard MIME types")
    extensions: list[str] = Field(default_factory=list, description="File extensions including the leading dot")
    metadata_hints: dict[str, Any] = Field(default_factory=dict, description="Optional metadata keys/values")
    min_bytes: Optional[int] = Field(default=None, ge=0, description="Minimum artifact size")
    max_bytes: Optional[int] = Field(default=None, ge=0, description="Maximum artifact size")


class ArtifactPrepManifest(BaseModel):
    """Manifest attached to a tool to register it as an artifact preparation strategy."""

    strategy_id: str = Field(..., description="Stable strategy identifier")
    strategy_version: str = Field(..., description="Semantic strategy version")
    handles: list[ArtifactFeatureMatch] = Field(default_factory=list, description="Feature matchers")
    priority: int = Field(default=0, description="Tie-break priority; higher wins")
    cost_class: PrepCostClass = Field(default="cheap", description="Expected cost class")
    produces_chunk_kinds: list[ChunkKind] = Field(default_factory=list, description="Chunk kinds produced")
    fallback_chain: list[str] = Field(default_factory=list, description="Fallback strategy IDs")

