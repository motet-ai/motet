"""
Motet - Context Preparation Providers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-04

Description:
    Exposes the provider pipeline used by prepare_context. The package groups
    context preparation stages by responsibility so new providers, such as
    artifact RAG, can be added without expanding the command implementation.

Dependencies:
    - context.pipeline for provider ordering and execution
    - context.types for shared provider protocol and state

Usage:
    from motet.core.orchestration.context import run_context_pipeline

Notes:
    - This package is internal to the runtime orchestration layer.
"""

from __future__ import annotations

from .pipeline import DEFAULT_CONTEXT_PROVIDERS, run_context_pipeline
from .rag_context import RagContextProvider
from .types import ContextPipelineState, ContextProvider

__all__ = [
    "ContextPipelineState",
    "ContextProvider",
    "DEFAULT_CONTEXT_PROVIDERS",
    "RagContextProvider",
    "run_context_pipeline",
]
