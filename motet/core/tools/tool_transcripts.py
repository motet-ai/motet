"""
Motet - Tool Transcript Models

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Defines the data models for Tool Invocations, which serve as the canonical
    provider-neutral record of a tool execution. These records are stored in
    MemoryManager and used to reconstruct schema-correct tool transcripts for
    LLM context. Also provides dedupe_tool_invocations() for shared deduplication
    by tool_call_id (used by finalize_turn and transcript_service).

    Oversized tool arguments are offloaded to ArtifactKind.TOOL_ARGUMENTS
    (``arguments_artifact_id``); ``arguments_json`` holds a capped valid-JSON
    preview suitable for memory, not for provider replay until hydrated.

Dependencies:
    - pydantic: Data validation and serialization
    - enum: Status enumerations
    - datetime: Time tracking

Usage:
    from motet.core.tools.tool_transcripts import ToolInvocation, ToolInvocationStatus

    invocation = ToolInvocation(
        tool_name="web_search",
        tool_call_id="call_123",
        status=ToolInvocationStatus.STARTED,
        arguments_json='{"query": "AI"}'
    )
"""

from enum import Enum
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

class ToolInvocationStatus(str, Enum):
    """Lifecycle status of a tool invocation."""
    STARTED = "started"
    SUCCESS = "success"
    ERROR = "error"
    AUTH_REQUIRED = "auth_required"

class ToolInvocation(BaseModel):
    """
    Canonical record of a tool execution.
    
    Stored in MemoryManager as a 'tool_invocation' item.
    Separates metadata (this record) from raw payloads (ToolArtifact).
    """
    # Identity
    tool_name: str
    tool_call_id: str
    provider: str = "builtin"  # builtin, mcp, memory, etc.
    
    # Causality
    task_id: Optional[str] = None
    command_id: Optional[str] = None
    parent_command_id: Optional[str] = None
    conversation_id: Optional[str] = None
    
    # Isolation
    tenant_id: Optional[str] = None
    principal_id: Optional[str] = None
    motet_id: Optional[str] = None
    
    # Inputs
    arguments_json: str = Field(
        default="{}",
        description=(
            "Inline arguments JSON for memory (full when under cap; valid-JSON "
            "offload preview when arguments_artifact_id is set)."
        ),
    )
    arguments_hash: Optional[str] = Field(
        default=None,
        description="SHA-256 hex digest of the full unmodified arguments JSON.",
    )
    arguments_truncated: bool = Field(
        default=False,
        description="True when full arguments were offloaded to arguments_artifact_id.",
    )
    arguments_artifact_id: Optional[str] = Field(
        default=None,
        description=(
            "ArtifactStore id for full unmodified arguments JSON "
            "(ArtifactKind.TOOL_ARGUMENTS). Distinct from result artifact_id."
        ),
    )
    
    # Batching Support (for multi-tool assistant messages)
    tool_call_group_id: Optional[str] = None
    tool_call_index: Optional[int] = None
    
    # Lifecycle
    status: ToolInvocationStatus = ToolInvocationStatus.STARTED
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    execution_duration_ms: Optional[int] = None
    
    # Outputs
    error_summary: Optional[str] = None
    preview_observation: Optional[str] = None  # Short summary for debugging/UI
    artifact_id: Optional[str] = None  # Reference to full payload in ArtifactStore (ADR-0061)
    # ADR-0113: artifact-backed media (e.g. generated images) the tool produced, as
    # serialized MediaPart dicts (each with its own artifact_id). Distinct from
    # ``artifact_id`` above, which references the tool's raw JSON result. Captured so
    # rehydrated transcripts can resurface generated media for the chat surface/UI.
    result_media: Optional[List[Dict[str, Any]]] = None

    # Versioning
    schema_version: str = "1.0"


def dedupe_tool_invocations(invocations: List[ToolInvocation]) -> Tuple[List[ToolInvocation], List[str]]:
    """
    Deduplicate tool invocations by tool_call_id, preferring final status over STARTED.

    Returns:
        (final_invocations, started_only_ids): Final-status invocations for transcript use,
        and tool_call_ids that had only STARTED records (for logging).
    """
    inv_by_id: Dict[str, ToolInvocation] = {}
    for inv in invocations:
        existing = inv_by_id.get(inv.tool_call_id)
        if not existing:
            inv_by_id[inv.tool_call_id] = inv
            continue
        if inv.status != ToolInvocationStatus.STARTED and existing.status == ToolInvocationStatus.STARTED:
            inv_by_id[inv.tool_call_id] = inv
            continue
        if inv.status != ToolInvocationStatus.STARTED and existing.status != ToolInvocationStatus.STARTED:
            inv_time = inv.completed_at or inv.started_at
            ex_time = existing.completed_at or existing.started_at
            if inv_time and ex_time and inv_time > ex_time:
                inv_by_id[inv.tool_call_id] = inv
            elif inv_time and not ex_time:
                inv_by_id[inv.tool_call_id] = inv
    final_invs = [i for i in inv_by_id.values() if i.status != ToolInvocationStatus.STARTED]
    started_only_ids = [tid for tid, inv in inv_by_id.items() if inv.status == ToolInvocationStatus.STARTED]
    return (final_invs, started_only_ids)

