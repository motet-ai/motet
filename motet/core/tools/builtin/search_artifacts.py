"""
Motet - Artifact Search Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Provides an agent-facing artifact RAG search tool for. The tool
    lets agentic workflows perform deliberate follow-up retrieval against the
    same scoped artifact chunk backend used by prepare_context.

Dependencies:
    - pydantic for tool parameter schema validation
    - ToolRegistry for built-in tool registration
    - core.rag_retrieve_context for scoped artifact chunk retrieval

Usage:
    Tool call: core.search_artifacts({"query": "What does the PDF say about refunds?"})

Notes:
    - Conversation scope is the default and safest retrieval mode.
    - Broader principal/motet scopes require deterministic execution metadata
      set by the caller/UI; the model cannot broaden scope by parameters alone.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from ..registry import ToolRegistry


class SearchArtifactsParams(BaseModel):
    """Parameters for searching indexed artifact chunks."""

    query: str = Field(..., description="Natural-language question or search query for artifact content")
    scope: Literal["conversation", "principal", "motet"] = Field(
        default="conversation",
        description="Requested retrieval scope. Broader scopes require deterministic caller authorization.",
    )
    artifact_ids: Optional[List[str]] = Field(
        default=None,
        description="Optional source artifact IDs to restrict retrieval to",
    )
    artifact_tags: Optional[List[str]] = Field(
        default=None,
        description="Optional artifact tags that narrow retrieval within the resolved scope",
    )
    top_k: Optional[int] = Field(
        default=None,
        ge=1,
        le=20,
        description="Maximum chunks to return. Defaults to artifact RAG configuration.",
    )
    token_budget: Optional[int] = Field(
        default=None,
        ge=1,
        le=12000,
        description="Approximate token budget for returned artifact context.",
    )


def _get_motet_context_optional() -> Any:
    """Return current MotetContext if the tool runs inside tool_execution."""

    try:
        from motet.core.commands.decorator import get_motet_context

        return get_motet_context()
    except Exception:
        return None


def _authorized_scope(requested_scope: str, motet: Any) -> str:
    """Conservatively resolve requested retrieval scope from deterministic metadata."""

    scope = requested_scope if requested_scope in {"conversation", "principal", "motet"} else "conversation"
    if scope == "conversation":
        return "conversation"

    metadata = getattr(motet, "metadata", None) or {}
    if not isinstance(metadata, dict):
        return "conversation"

    explicit_scope = str(metadata.get("artifact_rag_scope") or metadata.get("retrieval_scope") or "").lower()
    allow_broader = metadata.get("allow_broader_artifact_rag_scope") is True
    if allow_broader or explicit_scope == scope:
        return scope
    return "conversation"


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute scoped artifact RAG retrieval for an agent follow-up search."""

    parsed = SearchArtifactsParams(**(params or {}))
    motet = _get_motet_context_optional()
    if motet is None:
        return {
            "status": "error",
            "error": "Motet context is required to search scoped artifacts",
            "chunks": [],
            "context_text": "",
        }

    from motet.core.commands.command_data_classes import RagRetrieveContextData
    from motet.core.commands.builtin.rag import rag_retrieve_context

    resolved_scope = _authorized_scope(parsed.scope, motet)
    result = motet.do(
        rag_retrieve_context,
        data=RagRetrieveContextData(
            query_text=parsed.query,
            scope=resolved_scope,
            conversation_id=getattr(motet, "conversation_id", None),
            role="user",
            artifact_ids=parsed.artifact_ids,
            artifact_tags=parsed.artifact_tags,
            top_k=parsed.top_k,
            token_budget=parsed.token_budget,
        ),
    )
    if not isinstance(result, dict):
        result = {"context_text": str(result), "chunks": []}

    return {
        "status": "ok",
        "requested_scope": parsed.scope,
        "resolved_scope": resolved_scope,
        "scope_downgraded": parsed.scope != resolved_scope,
        "query": parsed.query,
        "chunks": result.get("chunks", []),
        "chunk_count": result.get("chunk_count", len(result.get("chunks", []) or [])),
        "context_text": result.get("context_text", ""),
        "token_budget": result.get("token_budget"),
        "hybrid_enabled": result.get("hybrid_enabled"),
    }


def _format_observation(result: Dict[str, Any]) -> str:
    status = result.get("status", "unknown")
    chunk_count = int(result.get("chunk_count") or 0)
    scope = result.get("resolved_scope") or result.get("requested_scope") or "conversation"
    return f"search_artifacts(status={status}, scope={scope}, chunks={chunk_count})"


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.search_artifacts",
        description=(
            "Search indexed artifact/document chunks for evidence when prepared context is insufficient. "
            "Use for follow-up questions about uploaded files, PDFs, reports, attachments, or document citations. "
            "When searching within a specific attached artifact, pass artifact_ids with the source_artifact_id "
            "from attachment metadata. Default scope is the current conversation; broader scopes are only "
            "honored when caller policy allows them."
        ),
        func=run,
        tool_schema=SearchArtifactsParams,
        category="artifacts",
        contextualize_observation=True,
        observation_formatter=_format_observation,
        default_timeout_seconds=20.0,
        suggested_max_calls=3,
        cost_class="medium",
        keywords=[
            "artifact",
            "artifacts",
            "attachment",
            "attachments",
            "document",
            "documents",
            "file",
            "files",
            "pdf",
            "rag",
            "search artifacts",
            "source citation",
        ],
        required_capabilities=["tool_execution", "vector_operations", "embeddings"],
    )


__all__ = ["SearchArtifactsParams", "register", "run"]
