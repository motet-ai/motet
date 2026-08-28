"""
Motet - Artifact Preparation Strategy Protocol

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-08

Description:
    Defines the small runtime protocol used by built-in and tool-registered
    artifact preparation strategies. The selector emits declarative plans, and
    the executor invokes deterministic strategy implementations to produce
    PreparedArtifactChunk records.

Dependencies:
    - typing Protocol for structural strategy implementations
    - pydantic preparation models for plan/result contracts

Usage:
    class TextPreparationStrategy:
        manifest = ArtifactPrepManifest(...)
        def prepare(self, plan, context): ...

Notes:
    - Bundle-shipped strategies are still registered as tools; this protocol is
      the internal execution shape used after selection.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from .models import ArtifactPayloadInfo, ArtifactPrepHints, ArtifactPrepManifest, ArtifactPrepPlan, ArtifactPrepResult


class ArtifactPrepContext(BaseModel):
    """Execution context for artifact preparation strategies."""

    artifact: Any = Field(..., description="ArtifactMetadata-like object")
    payload: Any = Field(default=None, description="Artifact payload")
    payload_info: ArtifactPayloadInfo = Field(..., description="Prepared payload summary")
    source_artifact_id: str = Field(
        default="",
        description="Canonical source artifact ID for chunk provenance (may differ from artifact.id when prepping derived text)",
    )
    artifact_tags: list[str] = Field(default_factory=list, description="Tags copied from merged source/derived metadata")
    tenant_id: str = Field(..., description="Tenant ID")
    principal_id: str = Field(..., description="Principal ID")
    motet_id: str = Field(..., description="Motet ID")
    conversation_id: str = Field(default="", description="Conversation ID")
    role: str = Field(default="user", description="Policy role")
    hints: ArtifactPrepHints = Field(default_factory=ArtifactPrepHints, description="Caller preparation hints")
    config: dict[str, Any] = Field(default_factory=dict, description="Strategy-affecting configuration")

    model_config = {"arbitrary_types_allowed": True}


class ArtifactPrepStrategy(Protocol):
    """Runtime protocol implemented by artifact preparation strategies."""

    manifest: ArtifactPrepManifest

    def plan(self, context: ArtifactPrepContext) -> ArtifactPrepPlan:
        """Return a deterministic plan for the artifact context."""

        ...  # Protocol stub (implementations provide bodies)

    def prepare(self, plan: ArtifactPrepPlan, context: ArtifactPrepContext) -> ArtifactPrepResult:
        """Execute a deterministic preparation plan."""

        ...  # Protocol stub (implementations provide bodies)

