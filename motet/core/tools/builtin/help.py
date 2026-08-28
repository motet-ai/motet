"""
Motet - Help Tool (Internal Operations Router)

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Built-in "help" tool for the Motet distributed framework.
    This tool is designed to be both user-focused and LLM-focused: it answers
    "what should I call next?" by searching internal, first-party registries:

    - Tool registry (built-in tools + MCP proxy tools)
    - Distributed command registry (CommandTypeRegistry)
    - Workflow registry (WorkflowRegistry)

    Uses FunctionDiscoveryVectorStore for semantic search across all
    three registries. This provides consistent hybrid search (BM25 + vector)
    behavior for accurate intent matching.

    It returns ranked recommendations and suggested next steps without executing
    any tools or commands. This reduces the tendency to use external web search
    for questions that should be answered by system introspection (e.g., "how do
    I delete a schedule in this system?").

Dependencies:
    - re: Tokenization for schedule intent detection
    - pydantic: Parameter validation and schema generation
    - typing: Type hints and annotations
    - motet.core.tools.function_discovery_vector_store.FunctionDiscoveryVectorStore: semantic search
    - motet.core.tools.registry.ToolRegistry: tool registry access
    - motet.core.workflow.WorkflowRegistry: workflow registry access

Usage:
    # Ask for the correct internal operation
    help: delete a schedule

    # Programmatic use
    from motet.core.tools.builtin.help import run
    out = run(registry, {"query": "how do I delete a schedule?"})

Notes:
    - This tool uses semantic search (FunctionDiscoveryVectorStore) exclusively.
    - No token-based fallback - consistent search path for all queries.
    - Includes targeted heuristics for common system operations (e.g. schedules).
    - Requires FunctionDiscoveryVectorStore to be initialized before use.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field
import structlog

from ..protocol import ok, err
from ..registry import ToolRegistry

logger = structlog.get_logger(__name__)


class HelpParams(BaseModel):
    """Parameters for the `help` tool (internal operations router)."""

    mode: Literal["system", "human"] = Field(
        default="system",
        description=(
            "Output mode. "
            "'system' returns structured, machine-actionable recommendations and next-step tool call suggestions (default). "
            "'human' returns a concise user-facing explanation plus the top recommended next steps."
        ),
        examples=["system", "human"],
    )
    query: str = Field(
        ...,
        description=(
            "User question about how to do something in THIS system. "
            "Example: 'delete a schedule', 'list scheduled commands', 'schedule a workflow'."
        ),
        examples=["delete a schedule", "how do I create a recurring schedule?", "schedule a workflow every 2 minutes"],
    )
    limit: int = Field(default=10, ge=1, le=50, description="Maximum number of recommendations to return.")
    include_tools: bool = Field(default=True, description="Include tool recommendations (built-ins + MCP tools).")
    include_commands: bool = Field(default=True, description="Include command recommendations (distributed command types).")
    include_workflows: bool = Field(default=True, description="Include workflow recommendations (WorkflowRegistry entries).")


def _tokenize(text: str) -> List[str]:
    """Extract tokens from text for intent detection (not for ranking - that uses vector search)."""
    return [t for t in re.findall(r"[a-zA-Z0-9_\-]+", (text or "").lower()) if t]


def _is_internal_ops_query(tokens: List[str]) -> bool:
    """Check if this query is about internal system operations (used for policy hints)."""
    internal_markers = {
        "schedule",
        "scheduled",
        "cron",
        "interval",
        "command",
        "commands",
        "workflow",
        "workflows",
        "tool",
        "tools",
        "principal_id",
        "tenant_id",
        "conversation_id",
        "motet",
        "imf",
    }
    return any(t in internal_markers for t in tokens)


def _schedule_intent(tokens: List[str]) -> Optional[str]:
    """
    Very small set of targeted heuristics for schedule-related routing.
    Returns one of: list|create|delete|cancel|suspend|resume|describe|unknown
    """
    if not any(t in {"schedule", "scheduled", "schedules"} for t in tokens):
        return None

    if any(t in {"list", "show", "view"} for t in tokens):
        return "list"
    if any(t in {"create", "make", "add", "new"} for t in tokens):
        return "create"
    if any(t in {"delete", "remove"} for t in tokens):
        return "delete"
    if any(t in {"cancel", "stop"} for t in tokens):
        return "cancel"
    if any(t in {"pause", "suspend"} for t in tokens):
        return "suspend"
    if any(t in {"resume", "unpause"} for t in tokens):
        return "resume"
    if any(t in {"describe", "details", "info"} for t in tokens):
        return "describe"
    return "unknown"


def _get_vector_store() -> Any:
    """
    Get the FunctionDiscoveryVectorStore for semantic search from worker context.
    
    The vector store is pre-initialized during worker startup and includes tools,
    workflows, and distributed commands. This ensures fast access without
    duplicate initialization or indexing overhead.
    
    Returns:
        FunctionDiscoveryVectorStore instance from worker context
        
    Raises:
        RuntimeError: If vector store is not available in worker context
    """
    from ...workers.invoker_context import get_worker_context
    
    worker_context = get_worker_context()
    if not worker_context:
        raise RuntimeError(
            "Worker context not available. The help tool requires execution within a worker context."
        )
    
    store = worker_context.get("function_discovery_store")
    if store is None:
        raise RuntimeError(
            "FunctionDiscoveryVectorStore not found in worker context. "
            "The vector store should be initialized during worker startup."
        )
    
    if not store.is_initialized():
        raise RuntimeError(
            "FunctionDiscoveryVectorStore is not initialized. "
            "The vector store should be indexed during worker startup."
        )
    
    return store


def _search_with_vector_store(
    *,
    query: str,
    limit: int,
    include_tools: bool,
    include_commands: bool,
    include_workflows: bool,
) -> List[Dict[str, Any]]:
    """
    Search tools, commands, and workflows using FunctionDiscoveryVectorStore.
    
    Returns ranked recommendations with similarity scores.
    """
    store = _get_vector_store()
    
    # Get more results than needed to allow filtering
    raw_results = store.search_functions(query, top_k=limit * 2, enable_boosting=True)
    
    tokens = _tokenize(query)
    internal_ops = _is_internal_ops_query(tokens)
    schedule_intent = _schedule_intent(tokens)
    
    recs: List[Dict[str, Any]] = []
    
    for item in raw_results:
        item_type = item.get("type")
        
        # Filter by include flags
        if item_type == "tool" and not include_tools:
            continue
        if item_type == "command" and not include_commands:
            continue
        if item_type == "workflow" and not include_workflows:
            continue
        
        score = item.get("similarity_score", 0.0)
        name = item.get("name", "")
        
        # Apply post-search adjustments for internal ops
        if internal_ops and item_type == "tool":
            # Penalize web search tools for internal operations
            if name in ("web_search", "core.web_search") or name.startswith("mcp.web-search."):
                score *= 0.1  # Heavy penalty
            # Boost system-category tools
            if item.get("metadata", {}).get("category", "").lower() == "system":
                score *= 1.2
        
        # Apply schedule-specific boosts
        if schedule_intent and item_type == "tool":
            if name in ("scheduled_commands_list", "core.scheduled_commands_list") and schedule_intent in {"list", "describe", "unknown"}:
                score *= 1.5
            if name in ("schedule_command", "core.schedule_command") and schedule_intent in {"create", "unknown"}:
                score *= 1.5
            if name in ("manage_schedule", "core.manage_schedule") and schedule_intent in {"delete", "cancel", "suspend", "resume", "unknown"}:
                score *= 1.5
        
        # Build recommendation entry
        rec: Dict[str, Any] = {
            "kind": item_type,
            "score": round(score, 4),
        }
        
        if item_type == "tool":
            rec["name"] = name
            rec["category"] = item.get("metadata", {}).get("category", "general")
            # Descriptions are indexed for search but not stored to keep responses compact.
            # Use tool_describe for full details.
        elif item_type == "command":
            rec["command_type"] = item.get("command_type") or name
            rec["description"] = item.get("description", "")
        elif item_type == "workflow":
            rec["workflow_id"] = item.get("workflow_id", "")
            rec["name"] = item.get("metadata", {}).get("name", "") or name
            # Workflow descriptions indexed for search but not stored to keep responses compact.
        
        recs.append(rec)
    
    # Re-sort after adjustments
    recs.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    
    return recs[:limit]


def _build_next_steps(
    *,
    query: str,
    top_results: List[Dict[str, Any]],
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """
    Dynamically generate next-step suggestions from semantic search results.
    
    Uses the vector search ranking to determine what tools/commands/workflows
    to suggest, rather than hardcoded heuristics. Adds procedural guidance hints
    based on detected intent patterns.
    """
    tokens = _tokenize(query)
    steps: List[Dict[str, Any]] = []
    
    # Detect intent patterns for procedural guidance
    is_destructive = any(t in {"delete", "remove", "cancel", "stop", "suspend", "pause"} for t in tokens)
    is_inspect = any(t in {"list", "show", "view", "get", "describe", "info", "status"} for t in tokens)
    is_create = any(t in {"create", "add", "new", "schedule", "make", "set", "configure"} for t in tokens)
    is_modify = any(t in {"update", "modify", "edit", "change", "rename"} for t in tokens)
    
    # Guidance: For destructive/modify actions, suggest listing first to get IDs
    if (is_destructive or is_modify) and not is_inspect:
        # Find a listing tool in results that might help identify the target
        list_tools = [
            r for r in top_results 
            if any(kw in r.get("name", "").lower() for kw in ["list", "search", "get_"])
        ]
        if list_tools:
            steps.append({
                "kind": "guidance",
                "step": 1,
                "action": "identify_target",
                "tool_name": list_tools[0].get("name"),
                "why": "First, list/find the item you want to operate on to get its ID.",
            })
    
    # Add top vector search results as primary suggestions
    seen_names = set()
    for result in top_results:
        name = result.get("name") or result.get("command_type") or result.get("workflow_id")
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        
        if len([s for s in steps if s.get("kind") == "tool_call_suggestion"]) >= limit:
            break
        
        step: Dict[str, Any] = {
            "kind": "tool_call_suggestion",
            "score": result.get("score", 0),
        }
        
        kind = result.get("kind")
        if kind == "tool":
            step["tool_name"] = name
            step["category"] = result.get("category", "general")
            step["hint"] = "Use tool_describe for full parameter schema."
        elif kind == "command":
            step["command_type"] = result.get("command_type")
            step["hint"] = "Use command_describe to see full data schema before executing."
        elif kind == "workflow":
            step["workflow_id"] = result.get("workflow_id")
            step["workflow_name"] = result.get("name", "")
            step["hint"] = f"Callable as workflow_{result.get('workflow_id')} tool."
        
        steps.append(step)
    
    # Guidance: For create/configure actions, suggest describe tools to get schemas
    if is_create and not is_inspect:
        # Check if any command was suggested - if so, remind to use command_describe
        has_command = any(s.get("command_type") for s in steps)
        if has_command:
            steps.append({
                "kind": "guidance",
                "step": "before_create",
                "action": "get_schema",
                "tool_name": "core.command_describe",
                "why": "Get exact schema for command_data before creating - don't guess structure.",
            })
    
    # Guidance: For modify actions, note that some things may be immutable
    if is_modify:
        steps.append({
            "kind": "guidance",
            "step": "note",
            "action": "check_mutability",
            "why": "Some system objects (e.g., schedules) are immutable. To change attributes, delete and recreate.",
        })
    
    return steps


def _humanize_next_steps(steps: List[Dict[str, Any]]) -> List[str]:
    """Format next steps for human-readable output."""
    out: List[str] = []
    for step in steps:
        kind = step.get("kind")
        
        if kind == "guidance":
            why = step.get("why", "")
            tool_name = step.get("tool_name")
            if tool_name:
                out.append(f"💡 {why} (use `{tool_name}`)")
            else:
                out.append(f"💡 {why}")
        
        elif kind == "tool_call_suggestion":
            tool_name = step.get("tool_name")
            command_type = step.get("command_type")
            workflow_id = step.get("workflow_id")
            hint = step.get("hint", "")
            
            if tool_name:
                out.append(f"→ Tool: `{tool_name}` - {hint}")
            elif command_type:
                out.append(f"→ Command: `{command_type}` - {hint}")
            elif workflow_id:
                out.append(f"→ Workflow: `{workflow_id}` - {hint}")
    
    return out


def _human_summary(*, query: str, internal_ops: bool, next_steps: List[Dict[str, Any]], recs: List[Dict[str, Any]]) -> str:
    """
    Create a short, user-facing summary.
    This should stay concise and defer to structured next_steps for exact params.
    """
    lines: List[str] = []
    lines.append(f"Request: {query}")

    if internal_ops:
        lines.append("This looks like an internal system operation. We'll use built-in discovery/management tools (not web search).")

    top = recs[:3]
    if top:
        lines.append("Top matches:")
        for it in top:
            kind = it.get("kind")
            if kind == "tool":
                lines.append(f"- Tool `{it.get('name')}`: {it.get('description', '')}")
            elif kind == "command":
                lines.append(f"- Command `{it.get('command_type')}`: {it.get('description', '')}")
            elif kind == "workflow":
                lines.append(f"- Workflow `{it.get('workflow_id')}`: {it.get('name', '')}")

    human_steps = _humanize_next_steps(next_steps)
    if human_steps:
        lines.append("Suggested next steps:")
        lines.extend([f"- {s}" for s in human_steps[:4]])

    return "\n".join(lines).strip()


def run(registry: ToolRegistry, params: Dict[str, Any]) -> Dict[str, Any]:
    """Internal help router using semantic search (synchronous for Celery workers - ADR-0033)."""
    try:
        p = HelpParams(**(params or {}))
    except Exception as exc:
        return err(f"validation error: {exc}")

    query = (p.query or "").strip()
    if not query:
        return err("query is required")

    tokens = _tokenize(query)
    internal_ops = _is_internal_ops_query(tokens)

    # Use semantic search (FunctionDiscoveryVectorStore) for ranking
    try:
        top_recs = _search_with_vector_store(
            query=query,
            limit=p.limit,
            include_tools=p.include_tools,
            include_commands=p.include_commands,
            include_workflows=p.include_workflows,
        )
    except Exception as exc:
        logger.error(
            "help_search_failed",
            query=query,
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return err(f"semantic search failed: {exc}")

    next_steps = _build_next_steps(query=query, top_results=top_recs, limit=3)

    # Mode-specific response shaping (best practice: machine vs human output contracts).
    if p.mode == "human":
        human = {
            "mode": "human",
            "query": query,
            "summary": _human_summary(query=query, internal_ops=internal_ops, next_steps=next_steps, recs=top_recs),
            "recommended_next_steps": next_steps,
            # Keep a tiny amount of structured ranking for transparency (but not overwhelming).
            "top_recommendations": top_recs[: min(5, len(top_recs))],
        }
        return ok(human)

    # Default: system mode (compact, machine-actionable).
    system: Dict[str, Any] = {
        "mode": "system",
        "query": query,
        "tokens": tokens,
        "internal_ops_query": internal_ops,
        "search_method": "semantic",  # Indicate that semantic search was used
        "policy": {
            "note": (
                "For internal system operations, prefer first-party discovery (help/tools_search/tool_describe/commands_list/command_describe) "
                "over external web search."
            )
        },
        "recommendations": top_recs,
        "next_steps": next_steps,
    }

    # Explicitly discourage web search tools for internal ops.
    if internal_ops:
        system["anti_patterns"] = [
            {
                "pattern": "Do not use external web search to discover internal operations",
                "why": "Web results are generic and usually irrelevant; this system exposes first-party tools/commands for these operations.",
            }
        ]

    return ok(system)


def _parse(line: str, trig: str) -> Dict[str, Any]:
    rest = line[len(trig) :].strip()
    if not rest:
        return {}

    # Support key=value tokens (mode=, limit=, include_tools=, etc.)
    params: Dict[str, Any] = {}
    # If user writes: help: limit=5 delete schedule
    tokens = rest.split()
    non_kv: List[str] = []
    for tok in tokens:
        if "=" in tok:
            k, v = tok.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if v.lower() in {"true", "false"}:
                params[k] = v.lower() == "true"
            else:
                try:
                    params[k] = int(v)
                except Exception:
                    params[k] = v
        else:
            non_kv.append(tok)

    if "query" not in params:
        params["query"] = " ".join(non_kv).strip() if non_kv else rest
    return params


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.help",
        description=(
            "Internal help router for THIS system. Use when you don't know which tool/command/workflow to call next, "
            "or when answering 'how do I...?' questions about system operations. "
            "Examples: 'how do I delete a schedule?', 'how do I connect to Google?', 'how do I list my tasks?', "
            "'what tool should I use for...?', 'how can I schedule a command?'. "
            "Searches internal registries (tools, commands, workflows) via semantic search and returns ranked recommendations. "
            "Best practice: use mode='system' (default) for machine-actionable routing; use mode='human' for user-facing guidance. "
            "ALWAYS prefer this over external web search for internal system operations (scheduling, OAuth, memory, tools, workflows)."
        ),
        func=lambda p, _r=registry: run(_r, p),
        tool_schema=HelpParams,
        triggers=["help:", "help ", "how do i ", "how to ", "what tool ", "what command ", "how can i "],
        parse_params=_parse,
        category="system",
        contextualize_observation=False,
        default_timeout_seconds=15.0,  # Semantic search can take 7+ seconds
        suggested_max_calls=1,
        cost_class="low",
        keywords=[
            "help", "how do i", "how to", "how can i", "what tool", "what command", "which tool",
            "schedule", "oauth", "login", "logout", "connect", "disconnect", "memory", "workflow",
            "list", "delete", "create", "manage", "configure", "system", "internal"
        ],
        presentation={
            "user_facing": False,
            "content_kind": "router",
            "modes": ["system", "human"],
        },
    )


__all__ = ["register"]

