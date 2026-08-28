"""
Motet - Artifact Preparation Routing Helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Shared deterministic rules for choosing whether to prepare/index from a
    source artifact payload versus a derived plain-text artifact. Uses the same selector as the executor so routing stays aligned
    with manifest matchers.

Dependencies:
    - ArtifactPrepSelector for hot-path strategy selection
    - ArtifactPrepContext for consistent feature inputs

Usage:
    if should_prepare_source_instead_of_derived(selector, source_ctx, derived_ctx):
        prepare_meta, payload = source_meta, source_payload

Notes:
    - When source selection fails (no strategy), keep the derived path.
"""

from __future__ import annotations

from .selector import ArtifactPrepSelector
from .strategy import ArtifactPrepContext


def should_prepare_source_instead_of_derived(
    selector: ArtifactPrepSelector,
    *,
    source_context: ArtifactPrepContext,
    derived_context: ArtifactPrepContext,
) -> bool:
    """Return True when the source payload selects a non-default strategy and derived is text-like."""

    try:
        source_strategy_id = selector.select(source_context).plan.strategy_id
    except ValueError:
        return False
    try:
        derived_strategy_id = selector.select(derived_context).plan.strategy_id
    except ValueError:
        return source_strategy_id != "text_default"
    if source_strategy_id == "text_default":
        return False
    return derived_strategy_id == "text_default" or source_strategy_id != derived_strategy_id
