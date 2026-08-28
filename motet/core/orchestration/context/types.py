"""
Motet - Context Preparation Pipeline Types

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-04

Description:
    Defines shared types for the context preparation provider pipeline. These
    types keep the distributed prepare_context command thin while preserving a
    single mutable state object for providers that assemble conversation
    history, memory recall, artifacts, and token budgeting.

Dependencies:
    - dataclasses for lightweight pipeline state containers
    - typing for provider protocols and structured context metadata

Usage:
    state = ContextPipelineState(messages=list(data.messages))
    state = provider.apply(state, data=data, motet=motet, logger=logger)

Notes:
    - Providers run synchronously inside the prepare_context distributed command.
    - The pipeline state intentionally stores canonical Message-like objects
      until the command serializes them for its response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Protocol


@dataclass
class ContextPipelineState:
    """Mutable state passed between context preparation providers."""

    messages: List[Any]
    context_info: Dict[str, Any] = field(
        default_factory=lambda: {
            "memory_items": [],
            "vector_results": [],
            "token_count": 0,
        }
    )
    timings: Dict[str, float] = field(default_factory=dict)


class ContextProvider(Protocol):
    """Protocol for a single context preparation stage."""

    name: str

    def apply(
        self,
        state: ContextPipelineState,
        *,
        data: Any,
        motet: Any,
        logger: Any,
    ) -> ContextPipelineState:
        """Return the updated pipeline state after this provider runs."""

        ...
