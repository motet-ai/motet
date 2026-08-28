"""
Motet - Context Preparation Provider Pipeline

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Coordinates the ordered context preparation providers used by the
    prepare_context distributed command. The pipeline keeps provider ordering
    explicit so conversation history, memory recall, artifact context, and token
    budgeting can evolve independently.

Dependencies:
    - time for end-to-end pipeline timing
    - context provider modules for each preparation stage
    - context.types for provider protocol and shared state

Usage:
    state = run_context_pipeline(data=data, motet=motet, logger=logger)

Notes:
    - Providers execute in-process inside the distributed command for
      Phase 1. Future RAG or expensive providers can delegate to distributed
      commands internally without changing the command boundary.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Optional

from .artifact_context import ArtifactContextProvider
from .conversation_history import ConversationHistoryProvider
from .memory_context import MemoryRecallProvider
from .rag_context import RagContextProvider
from .token_budget import TokenBudgetProvider
from .types import ContextPipelineState, ContextProvider


DEFAULT_CONTEXT_PROVIDERS: tuple[ContextProvider, ...] = (
    ConversationHistoryProvider(),
    MemoryRecallProvider(),
    ArtifactContextProvider(),
    RagContextProvider(),
    TokenBudgetProvider(),
)


def run_context_pipeline(
    *,
    data: Any,
    motet: Any,
    logger: Any,
    providers: Optional[Iterable[ContextProvider]] = None,
) -> ContextPipelineState:
    """Run context preparation providers in deterministic order."""

    start = time.perf_counter()
    state = ContextPipelineState(messages=list(data.messages))

    for provider in providers or DEFAULT_CONTEXT_PROVIDERS:
        state = provider.apply(state, data=data, motet=motet, logger=logger)

    state.timings["total_s"] = round(time.perf_counter() - start, 3)
    state.context_info["timings"] = state.timings
    return state
