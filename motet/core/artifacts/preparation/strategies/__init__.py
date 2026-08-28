"""
Motet - Built-In Artifact Preparation Strategies

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Exports the built-in artifact preparation strategies used by the
    selector and executor. These strategies cover plain text, JSON/tool output,
    structure-aware DOCX, and office documents through the canonical
    PreparedArtifactChunk contract.

Dependencies:
    - Strategy modules for text, JSON, DOCX, and office-document preparation

Usage:
    from motet.core.artifacts.preparation.strategies import builtin_strategies

Notes:
    - Bundle-shipped strategies are discovered through the tool registry; this
      module only covers core built-ins.
"""

from __future__ import annotations

from ..strategy import ArtifactPrepStrategy
from .docx import DOCX_STRUCTURED_STRATEGY_ID, DocxStructuredPreparationStrategy
from .json import JSON_STRATEGY_ID, JsonPreparationStrategy
from .office import OFFICE_STRATEGY_ID, OfficeDocumentPreparationStrategy
from .text import TEXT_STRATEGY_ID, TextPreparationStrategy, chunk_text_to_prepared_chunks
from .video import VIDEO_STRATEGY_ID, VideoPreparationStrategy


_BUILTIN_PREP_REGISTERED = False


def _builtin_prep_tool_stub(_params: dict) -> dict:  # type: ignore[type-arg]
    raise RuntimeError(
        "Built-in artifact preparation strategies execute via core.prepare_artifact_index, not direct tool calls."
    )


def ensure_builtin_prep_tools_registered() -> None:
    """Register built-in prep strategies on the tool registry; idempotent."""

    global _BUILTIN_PREP_REGISTERED
    if _BUILTIN_PREP_REGISTERED:
        return
    from motet.core.tools.registry import registry as tool_registry

    # NOTE: the video strategy only consumes already-derived artifacts (it never
    # runs ffmpeg), so MEDIA_PROCESSING is intentionally NOT required here —
    # adding it would make ffmpeg-less workers ineligible for all prep work.
    caps = ["file_operations", "vector_operations", "embeddings"]
    for strat in builtin_strategies():
        strategy_id = strat.manifest.strategy_id
        if tool_registry.get(strategy_id) is not None:
            continue
        tool_registry.register(
            name=strategy_id,
            description=f"Built-in artifact preparation strategy ({strategy_id}).",
            func=_builtin_prep_tool_stub,
            prep_manifest=strat.manifest,
            required_capabilities=caps,
            expose_to_agents=False,
            category="artifact_preparation",
        )
    _BUILTIN_PREP_REGISTERED = True


def builtin_strategies() -> list[ArtifactPrepStrategy]:
    """Return built-in preparation strategy instances."""

    return [
        JsonPreparationStrategy(),
        DocxStructuredPreparationStrategy(),
        OfficeDocumentPreparationStrategy(),
        TextPreparationStrategy(),
        VideoPreparationStrategy(),
    ]


__all__ = [
    "DOCX_STRUCTURED_STRATEGY_ID",
    "DocxStructuredPreparationStrategy",
    "JSON_STRATEGY_ID",
    "JsonPreparationStrategy",
    "OFFICE_STRATEGY_ID",
    "OfficeDocumentPreparationStrategy",
    "TEXT_STRATEGY_ID",
    "TextPreparationStrategy",
    "VIDEO_STRATEGY_ID",
    "VideoPreparationStrategy",
    "builtin_strategies",
    "chunk_text_to_prepared_chunks",
    "ensure_builtin_prep_tools_registered",
]

