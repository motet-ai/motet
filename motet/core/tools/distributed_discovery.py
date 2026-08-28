"""
Motet - Distributed Tool Discovery

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Embedding-first tool discovery service.

    Production agentic routing uses FunctionDiscoveryVectorStore + the main
    model_stream call. This module exposes a slim ToolDiscoveryService that
    returns ranked ToolCandidate lists via semantic/embedding search only —
    no native function-calling short-circuit and no keyword fallback.

Dependencies:
    - pydantic: Data validation and model definitions
    - structlog: Structured logging
    - FunctionDiscoveryVectorStore
    - Tool registry (optional tool_info enrichment)

Usage:
    from motet.core.tools.distributed_discovery import (
        ToolDiscoveryService,
        ToolDiscoveryContext,
        ToolCandidate,
    )

    discovery = ToolDiscoveryService(
        tool_registry=motet.tools,
        function_discovery_store=motet.function_discovery_store,
    )
    candidates = discovery.discover_tools(
        content=query,
        context_type=ToolDiscoveryContext.DIRECT_QUERY,
        max_tools=5,
    )

Notes:
    - Missing or empty vector store yields no candidates.
    - Shared types ToolDiscoveryContext / ToolCandidate remain the public contract.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel
import structlog

from ..types import Message
from ..registry import RegistryProtocol
from .registry import RegisteredTool

logger = structlog.get_logger(__name__)


class ToolDiscoveryContext(Enum):
    """Context types for tool discovery to determine the best strategy."""
    USER_PROMPT = "user_prompt"          # Direct user request (ReAct style)
    REASONING_TRACE = "reasoning_trace"  # Reasoning investigation (CoT style)
    DIRECT_QUERY = "direct_query"        # Direct strategy tool detection
    REACT_TOOL_CALL = "react_tool_call"  # ReAct strategy tool execution
    HYBRID = "hybrid"                    # Combine both approaches


class ToolCandidate(BaseModel):
    """Unified representation of a discovered tool candidate."""
    name: str
    parameters: Dict[str, Any]
    confidence: float
    reasoning: str
    discovery_method: str
    tool_info: Optional[RegisteredTool] = None


class ToolDiscoveryService:
    """
    Embedding-only tool discovery via FunctionDiscoveryVectorStore (ADR-0051 / ADR-0074).

    Returns ranked ToolCandidate lists for callers such as the tool_discovery
    command. Does not invoke an LLM or native function calling.
    """

    def __init__(
        self,
        tool_registry: Optional[RegistryProtocol[Any]] = None,
        function_discovery_store: Optional[Any] = None,
    ):
        """
        Initialize tool discovery service.

        Args:
            tool_registry: Registry-like instance implementing RegistryProtocol
            function_discovery_store: Optional FunctionDiscoveryVectorStore for
                semantic search (ADR-0051). If None, tries motet context.
        """
        self.tool_registry = tool_registry
        self.function_discovery_store = function_discovery_store

        if not self.tool_registry:
            try:
                from .registry import registry as default_registry
                self.tool_registry = default_registry
            except ImportError:
                self.tool_registry = None

        if not self.function_discovery_store:
            try:
                from motet.core.commands.decorator import get_motet_context
                motet = get_motet_context()
                self.function_discovery_store = getattr(
                    motet, "function_discovery_store", None
                )
            except (RuntimeError, AttributeError):
                self.function_discovery_store = None

    def discover_tools(
        self,
        content: str,
        context_type: ToolDiscoveryContext = ToolDiscoveryContext.USER_PROMPT,
        max_tools: int = 5,
        conversation_history: Optional[List[Message]] = None,
        reasoning_task: Optional[Any] = None,
        **_: Any,
    ) -> List[ToolCandidate]:
        """
        Discover tools via embedding/semantic search only.

        Args:
            content: Natural-language query to match against indexed tools
            context_type: Discovery context (retained for API compatibility)
            max_tools: Maximum number of ToolCandidate results
            conversation_history: Optional history passed to the vector store
            reasoning_task: Unused; kept for call-site compatibility

        Returns:
            Ranked ToolCandidate list, or [] when the store is missing/empty.
        """
        del reasoning_task  # API compatibility only

        store = self.function_discovery_store
        if store is None:
            logger.info(
                "tool_discovery_no_store",
                query=content,
                context_type=getattr(context_type, "value", str(context_type)),
                note="FunctionDiscoveryVectorStore unavailable; returning no candidates",
            )
            return []

        try:
            semantic_results = store.search_functions(
                query=content,
                top_k=max_tools,
                conversation_history=conversation_history,
                search_types=["tool", "workflow"],
            )
        except Exception as exc:
            logger.error(
                "tool_discovery_search_failed",
                query=content,
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
            )
            return []

        if not isinstance(semantic_results, list):
            logger.error(
                "tool_discovery_unexpected_result_type",
                query=content,
                result_type=type(semantic_results).__name__,
                note="search_functions returned non-list; using empty list",
            )
            return []

        if not semantic_results:
            logger.info(
                "tool_discovery_no_results",
                query=content,
                context_type=getattr(context_type, "value", str(context_type)),
            )
            return []

        candidates: List[ToolCandidate] = []
        for result in semantic_results:
            if not isinstance(result, dict):
                continue

            result_type = result.get("type") or "tool"
            # Prefer tools; workflows may appear in the index but tool_discovery
            # serializes tool-shaped candidates. Skip non-tool hits for this API.
            if result_type not in ("tool", "workflow"):
                continue

            name = result.get("name") or result.get("workflow_id")
            if not isinstance(name, str) or not name:
                continue

            score_raw = result.get("similarity_score", result.get("score", 0.0))
            try:
                confidence = float(score_raw) if score_raw is not None else 0.0
            except (TypeError, ValueError):
                confidence = 0.0

            metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
            description = ""
            if isinstance(metadata, dict):
                description = str(metadata.get("description") or "")

            tool_info: Optional[Any] = None
            if self.tool_registry is not None:
                try:
                    tool_info = self.tool_registry.get(name)
                except Exception:
                    tool_info = None

            reasoning_parts = [
                f"Semantic match for {name}",
            ]
            if description:
                reasoning_parts.append(description)
            if context_type is not None:
                reasoning_parts.append(
                    f"context={getattr(context_type, 'value', str(context_type))}"
                )

            candidates.append(
                ToolCandidate(
                    name=name,
                    parameters={},
                    confidence=confidence,
                    reasoning="; ".join(reasoning_parts),
                    discovery_method="embedding",
                    tool_info=tool_info,
                )
            )

            if len(candidates) >= max_tools:
                break

        logger.info(
            "tool_discovery_completed",
            query=content,
            candidate_count=len(candidates),
            max_tools=max_tools,
            context_type=getattr(context_type, "value", str(context_type)),
            discovery_method="embedding",
        )
        return candidates


__all__ = ["ToolDiscoveryService", "ToolDiscoveryContext", "ToolCandidate"]
