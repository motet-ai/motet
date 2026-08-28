"""
Motet - Artifact Preparation Executor

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Executes artifact preparation plans through deterministic strategy
    implementations. The executor validates that the selected strategy matches
    the plan and returns canonical ArtifactPrepResult objects for indexing.

Dependencies:
    - preparation strategy protocol and result models

Usage:
    result = ArtifactPrepExecutor().execute(selection.strategy, selection.plan, context)

Notes:
    - Agent planner execution is outside this executor; the executor only runs
      already selected deterministic strategy implementations.
"""

from __future__ import annotations

import structlog

from .models import ArtifactPrepPlan, ArtifactPrepResult
from .strategy import ArtifactPrepContext, ArtifactPrepStrategy

logger = structlog.get_logger(__name__)


class ArtifactPrepExecutor:
    """Execute selected artifact preparation strategies."""

    def execute(
        self,
        *,
        strategy: ArtifactPrepStrategy,
        plan: ArtifactPrepPlan,
        context: ArtifactPrepContext,
    ) -> ArtifactPrepResult:
        """Run a selected strategy and return its canonical result."""

        if strategy.manifest.strategy_id != plan.strategy_id:
            raise ValueError(
                f"Plan strategy {plan.strategy_id} does not match executor strategy {strategy.manifest.strategy_id}"
            )
        try:
            logger.info(
                "artifact_prep_execute_started",
                strategy_id=plan.strategy_id,
                strategy_version=plan.strategy_version,
                source_artifact_id=plan.source_artifact_id,
            )
            result = strategy.prepare(plan, context)
            if "fallback_text_strategy" in (plan.diagnostics or []):
                patched_chunks = [
                    ch.model_copy(update={"prep_state": "prep_partial", "confidence": min(float(ch.confidence), 0.6)})
                    for ch in result.chunks
                ]
                result = result.model_copy(
                    update={
                        "prep_state": "prep_partial",
                        "chunks": patched_chunks,
                        "diagnostics": list(
                            dict.fromkeys([*(result.diagnostics or []), "fallback_text_strategy"])
                        ),
                    }
                )
            logger.info(
                "artifact_prep_execute_completed",
                strategy_id=plan.strategy_id,
                prep_state=result.prep_state,
                chunk_count=len(result.chunks),
            )
            return result
        except Exception as e:
            logger.error(
                "artifact_prep_execute_failed",
                strategy_id=plan.strategy_id,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise RuntimeError(f"Artifact preparation failed for strategy {plan.strategy_id}: {e}") from e

