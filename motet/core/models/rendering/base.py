"""
Motet - Model Message Rendering (Provider Adapters)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Canonical multimodal rendering layer for model messages.

    This package sits between orchestration (which decides WHAT to include) and
    provider SDKs (which require specific request schemas).

    The core design is:
    - Orchestration produces provider-agnostic message intent via `Message.content_parts`
    (text + artifact references).
    - Renderers materialize artifact-backed media parts into a canonical, provider-agnostic form
    (e.g., `MediaPart.base64_data` for images).
    - Provider adapters then translate the canonical rendered messages into provider wire schemas.

    This mirrors the existing tool transcript rendering pattern in
    `motet.core.tools.rendering`, but is scoped to general multimodal messages.

Dependencies:
    - typing: Protocols and type hints
    - dataclasses: Context container for rendering inputs
    - motet.core.types: Message and ContentPart models
    - motet.core.artifacts.protocol: Artifact store access (fail-closed by caller)

Usage:
    from motet.core.models.rendering import get_renderer
    from motet.core.models.rendering.base import RenderingContext

    renderer = get_renderer("canonical")
    rendered_messages = renderer.render(messages, context=RenderingContext(...))

Notes:
    - Renderers should NOT decide inclusion policy (budgets, allowlists). They should
      only map the already-selected content into provider schemas and enforce provider
      hard limits (e.g. max bytes per image) as a safety belt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from ...artifacts.protocol import ArtifactStoreProtocol
from ...types import Message


@dataclass(frozen=True)
class RenderingContext:
    """
    Context provided to renderers so they can safely fetch artifacts and apply provider limits.

    Orchestration is responsible for:
    - deciding what to include
    - enforcing budgets/policy

    Renderers may still enforce provider hard limits as a safety belt.
    """

    provider: str
    model_name: str
    # Isolation context for artifact fetches (fail-closed if missing)
    tenant_id: Optional[str]
    principal_id: Optional[str]
    motet_id: Optional[str]
    artifact_store: ArtifactStoreProtocol
    # Optional rendering budgets (defaults are provider-specific)
    max_images: int = 8
    max_image_bytes: int = 4 * 1024 * 1024  # 4MB


class ProviderMessageRenderer(Protocol):
    """Protocol for canonical multimodal message rendering."""

    def render(self, messages: List[Message], *, context: RenderingContext) -> List[Message]:
        """
        Render a list of internal Message objects into canonical, provider-agnostic messages.

        Args:
            messages: Motet internal Message models
            context: RenderingContext with provider/model and safe artifact access

        Returns:
            List of canonical `Message` objects with artifact-backed media parts materialized.
        """
        ...


