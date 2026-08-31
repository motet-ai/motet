"""
Motet - Command Data Classes

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-31

Description:
    Comprehensive command data classes for the Motet distributed framework.
    Provides standardized data structures for all distributed commands including
    model inference, tool execution, reasoning, planning, memory operations,
    and orchestration. Includes AgenticLoopData for agentic_loop pattern
    with recursive tool chaining and unified task-level streaming.

    Request context:
    - Model command payloads support `request_context` for identity/isolation/budgets that must
    travel with the request (separate from `model_settings`, which is reserved for model tuning).

Dependencies:
    - pydantic: Data validation and model definitions
    - datetime: Timestamp and time-based operations
    - typing: Type hints and annotations
    - Base command data and mixins
    - Command data registry

Usage:
    from motet.core.commands.command_data_classes import (
        ModelInferenceData, ToolExecutionData, AgentTurnData
    )
    
    # Create model inference data
    data = ModelInferenceData(
        messages=[Message(role="user", content="Hello")],
        model_settings={
            "provider": "openai",
            "model_name": "gpt-4o-mini",
            "temperature": 0.7,
            "max_tokens": 1000
        }
    )
    
    # Create tool execution data
    tool_data = ToolExecutionData(
        tool_name="web_search",
        parameters={"query": "AI news"}
    )

Notes:
    - Provides standardized data structures for all command types
    - Includes automatic Message serialization and deserialization
    - Supports model inference, streaming, and embedding operations
    - Includes tool execution, discovery, and listing operations
    - Supports reasoning, planning, and memory operations
    - Integrates with command data registry for centralized management
    - Includes comprehensive validation and error handling
"""


from typing import Dict, Any, Optional, List, Union, Literal
from enum import Enum
from datetime import datetime
from pydantic import BaseModel, Field, field_serializer, field_validator
from motet.core.commands.base_command_data import (
    BaseCommandData,
    MessageFieldMixin,
    unknown_command_data_keys,
)
from motet.core.commands.command_data_registry import command_data_registry
from motet.core.artifacts.preparation import ArtifactPrepHints
from motet.core.types import CanonicalToolSchema, OutputContract, RequestContext, SkillRef


# Import enum for tool discovery
from motet.core.tools.distributed_discovery import ToolDiscoveryContext


# Identity / model / schedule inheritance keys. Copied onto the runtime stack
# in tool_execution and into schedule_context so schedule_command / agent_turn
# see agent, surface, roles, and model settings (ADR-0083).
SCHEDULE_CONTEXT_KEYS: tuple = (
    "agent_id",
    "conversation_primary_agent_id",
    "surface_id",
    "principal_roles",
    "role",
    "enable_thinking",
    "reasoning_effort",
    "model_profile_name",
    "model_provider",
    "model_name",
    # Propagate API/gateway memory policy so workers honor caller Config
    # (e.g. MOTET_MEMORY_AGENT_SCOPE_MODE) instead of only worker boot env.
    "memory_agent_scope_mode",
)

# Artifact RAG authorization keys from chat / workflow agent_turn context.
# Must live in distributed_context.metadata so core.search_artifacts
# (_authorized_scope) and RagContextProvider can honor broader scopes
# (ADR-0122 Phase 7). Not needed on the runtime stack attrs used by
# schedule_command — keep separate from SCHEDULE_CONTEXT_KEYS.
ARTIFACT_RAG_CONTEXT_KEYS: tuple = (
    "allow_broader_artifact_rag_scope",
    "artifact_rag_scope",
    "artifact_ids",
    "artifact_tags",
    "artifact_collection_id",
)

# Agent ToolFilter snapshot for meta-tools (core.tools_search / core.tool_call).
# Must reach nested tool_execution metadata so generic dispatch and disclosure
# enforce the same exclude/prefix/category/no_workflows gates that shaped the
# shortlist (ADR-0128 meta-tool progressive disclosure).
TOOL_FILTER_CONTEXT_KEYS: tuple = ("tool_filter_metadata",)

# Everything that must survive into child distributed_context.metadata when
# spawning tool_execution / nested agent turns. Use this (not SCHEDULE alone)
# anywhere that rebuilds a metadata dict for motet.join/do/gather — explicit
# metadata= replaces parent metadata rather than merging.
HANDOFF_CONTEXT_KEYS: tuple = (
    "handoff_depth",
    "handoff_path",
    "handoffs",
)

DELEGATED_CONTEXT_KEYS: tuple = (
    SCHEDULE_CONTEXT_KEYS
    + ARTIFACT_RAG_CONTEXT_KEYS
    + TOOL_FILTER_CONTEXT_KEYS
    + HANDOFF_CONTEXT_KEYS
)


# ========================================
# Utility Functions for Serialization
# ========================================
# Note: Message deserialization is now handled automatically by:
# - BaseCommandData for 'conversation_history' field
# - MessageFieldMixin for 'messages' field
# No utility functions needed for most cases!


# Model Commands
class ModelInferenceData(MessageFieldMixin, BaseCommandData):
    """
    Data payload for model inference operations.
    
    For local inference, set provider="local" in model_settings:
        model_settings = {
            "provider": "local",  # Triggers local inference
            "model_name": "phi-4-mini"  # Resolved via model registry
        }
    
    For API-based inference:
        model_settings = {
            "provider": "openai",  # or "anthropic", "google"
            "model_name": "gpt-4",
            "temperature": 0.7,  # Optional, defaults to 0.2
            "max_tokens": 1000  # Optional, defaults to 8000
        }
    
    For native function calling:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather for a location",
                    "parameters": {...}
                }
            }
        ]
    """
    messages: List[Any] = Field(
        default_factory=list,
        description="Conversation messages for the model. Each item should be {role, content} (Message-like dict/object).",
    )
    model_settings: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Model configuration (e.g., provider, model_name, temperature, max_tokens).",
    )
    request_context: Optional[RequestContext] = Field(
        default=None,
        description="Request-scoped context (identity/isolation/budgets; see motet.core.types.RequestContext)",
    )
    stream: bool = Field(
        default=False,
        description="If True, stream tokens/events rather than returning a single final response.",
    )
    tools: Optional[List[Union[Dict[str, Any], CanonicalToolSchema]]] = Field(
        default=None,
        description="Optional tool schemas for native function calling. Supports legacy provider formats (dict) and canonical schemas (CanonicalToolSchema).",
    )
    skill_refs: Optional[List[SkillRef]] = Field(
        default=None,
        description="Skills applied for this inference call (observability only; adapters do not reinterpret).",
    )
    output_contract: Optional[OutputContract] = Field(
        default=None,
        description="Canonical structured-output contract (e.g. Json_schema for grammar-constrained decoding). Adapters that support it constrain generation; others degrade to unconstrained output.",
    )

    @field_validator("request_context", mode="before")
    @classmethod
    def _coerce_request_context(cls, v: Any) -> Optional[RequestContext]:
        """Auto-coerce dict to RequestContext for backwards compatibility."""
        if v is None:
            return None
        if isinstance(v, RequestContext):
            return v
        if isinstance(v, dict):
            return RequestContext(**v)
        raise ValueError(f"request_context must be RequestContext or dict, got {type(v).__name__}")

    @field_validator("output_contract", mode="before")
    @classmethod
    def _coerce_output_contract(cls, v: Any) -> Optional[OutputContract]:
        """Auto-coerce dict to OutputContract for backwards compatibility."""
        if v is None:
            return None
        if isinstance(v, OutputContract):
            return v
        if isinstance(v, dict):
            return OutputContract(**v)
        raise ValueError(f"output_contract must be OutputContract or dict, got {type(v).__name__}")

    @field_validator("skill_refs", mode="before")
    @classmethod
    def _coerce_skill_refs(cls, v: Any) -> Optional[List[SkillRef]]:
        if v is None:
            return None
        if not isinstance(v, list):
            raise ValueError("skill_refs must be a list or None")
        out: List[SkillRef] = []
        for item in v:
            if isinstance(item, SkillRef):
                out.append(item)
            elif isinstance(item, dict):
                out.append(SkillRef(**item))
            else:
                raise ValueError(f"skill_refs items must be SkillRef or dict, got {type(item).__name__}")
        return out


class ModelStreamData(MessageFieldMixin, BaseCommandData):
    """Data payload for model streaming operations."""
    messages: List[Any] = Field(
        default_factory=list,
        description="Conversation messages for the model. Each item should be {role, content} (Message-like dict/object).",
    )
    stream_key: str = Field(
        default="",
        description="Redis stream key to write streaming tokens/events to.",
    )
    tools: Optional[List[Union[Dict[str, Any], CanonicalToolSchema]]] = Field(
        default=None,
        description="Optional tool schemas for native function calling. Supports legacy provider formats (dict) and canonical schemas (CanonicalToolSchema).",
    )
    model_settings: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Model configuration (e.g., provider, model_name, temperature, max_tokens).",
    )
    request_context: Optional[RequestContext] = Field(
        default=None,
        description="Request-scoped context (identity/isolation/budgets; see motet.core.types.RequestContext)",
    )
    skill_refs: Optional[List[SkillRef]] = Field(
        default=None,
        description="Skills applied for this streaming call (observability only; adapters do not reinterpret).",
    )
    output_contract: Optional[OutputContract] = Field(
        default=None,
        description="Canonical structured-output contract (e.g. Json_schema for grammar-constrained decoding). Adapters that support it constrain generation; others degrade to unconstrained output.",
    )

    @field_validator("request_context", mode="before")
    @classmethod
    def _coerce_request_context(cls, v: Any) -> Optional[RequestContext]:
        """Auto-coerce dict to RequestContext for backwards compatibility."""
        if v is None:
            return None
        if isinstance(v, RequestContext):
            return v
        if isinstance(v, dict):
            return RequestContext(**v)
        raise ValueError(f"request_context must be RequestContext or dict, got {type(v).__name__}")

    @field_validator("output_contract", mode="before")
    @classmethod
    def _coerce_output_contract_stream(cls, v: Any) -> Optional[OutputContract]:
        """Auto-coerce dict to OutputContract for backwards compatibility."""
        if v is None:
            return None
        if isinstance(v, OutputContract):
            return v
        if isinstance(v, dict):
            return OutputContract(**v)
        raise ValueError(f"output_contract must be OutputContract or dict, got {type(v).__name__}")

    @field_validator("skill_refs", mode="before")
    @classmethod
    def _coerce_skill_refs_stream(cls, v: Any) -> Optional[List[SkillRef]]:
        if v is None:
            return None
        if not isinstance(v, list):
            raise ValueError("skill_refs must be a list or None")
        out: List[SkillRef] = []
        for item in v:
            if isinstance(item, SkillRef):
                out.append(item)
            elif isinstance(item, dict):
                out.append(SkillRef(**item))
            else:
                raise ValueError(f"skill_refs items must be SkillRef or dict, got {type(item).__name__}")
        return out


class EmbeddingData(BaseCommandData):
    """Data payload for embedding operations."""
    texts: List[str] = Field(
        default_factory=list,
        description="List of input texts to embed.",
    )
    model: Optional[str] = Field(
        default=None,
        description="Optional embedding model identifier (provider-specific).",
    )


class ImageGenerationData(BaseCommandData):
    """
    Data payload for image generation operations.

    Generated images are stored as artifacts (ArtifactKind.GENERATED_IMAGE) and returned
    as canonical artifact-backed MediaParts so they reuse derivations, retention, RAG, and
    the multimodal-input path.
    """
    prompt: str = Field(
        default="",
        description="Text prompt describing the image(s) to generate.",
    )
    model_settings: Dict[str, Any] = Field(
        default_factory=dict,
        description="Provider/model settings (provider, model_name, response_format, etc.).",
    )
    n: int = Field(
        default=1,
        ge=1,
        le=10,
        description="Number of images to generate.",
    )
    size: Optional[str] = Field(
        default=None,
        description="Requested size as WIDTHxHEIGHT or 'auto' (provider-dependent).",
    )
    quality: Optional[str] = Field(
        default=None,
        description="Provider-specific quality hint (e.g. 'high').",
    )
    background: Optional[str] = Field(
        default=None,
        description="Provider-specific background hint (e.g. 'transparent').",
    )
    input_image_artifact_ids: Optional[List[str]] = Field(
        default=None,
        description="Optional source artifact IDs for edit/variation operations.",
    )
    trigger_derivations: bool = Field(
        default=True,
        description="If True, dispatch image derivations (thumb/base/detail) for each generated image.",
    )
    filename: Optional[str] = Field(
        default=None,
        description="Optional base filename to attach to stored generated images.",
    )
    ttl_seconds: Optional[int] = Field(
        default=None,
        description="Optional TTL (seconds) for stored generated-image artifacts.",
    )
    request_context: Optional[RequestContext] = Field(
        default=None,
        description="Request-scoped identity/isolation/budgets.",
    )


# Tool Commands
class ToolExecutionData(BaseCommandData):
    """Data payload for tool execution operations."""
    tool_name: str = Field(
        default="",
        description="Registered tool name to execute (e.g., 'web_search', 'schedule_task').",
    )
    parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Tool parameters object to pass to the tool (must match the tool schema).",
    )
    tool_call_id: Optional[str] = Field(
        default=None,
        description="Optional upstream tool call identifier for traceability (e.g., OpenAI tool_call id).",
    )
    # Batching metadata for multi-tool-call assistant messages (ADR-0061)
    tool_call_group_id: Optional[str] = Field(
        default=None,
        description="Optional batch/group ID for grouped tool calls from the same assistant message.",
    )
    tool_call_index: Optional[int] = Field(
        default=None,
        description="Optional index of this tool call within the group (0..N-1).",
    )
    # Stream key for tool execution events (for real-time UI updates)
    stream_key: Optional[str] = Field(
        default=None,
        description="Optional Redis stream key for streaming tool execution events to the frontend.",
    )
    # conversation_history inherited from BaseCommandData with automatic Message conversion


class ToolListData(BaseCommandData):
    """Data payload for tool listing operations."""
    include_mcp_tools: bool = Field(
        default=True,
        description="If True, include MCP-provided tools in the listing.",
    )
    include_builtin_tools: bool = Field(
        default=True,
        description="If True, include built-in (native) tools in the listing.",
    )
    filter_by_capability: Optional[str] = Field(
        default=None,
        description="Optional capability filter (only include tools requiring/using a specific capability).",
    )


class ToolDiscoveryData(BaseCommandData):
    """Data payload for tool discovery operations."""
    content: str = Field(
        default="",
        description="Text content to analyze for tool discovery (the user request or relevant context).",
    )
    context_type: ToolDiscoveryContext = Field(
        default=ToolDiscoveryContext.USER_PROMPT,
        description="Context type for discovery (e.g., user_prompt, system_prompt).",
    )
    max_tools: int = Field(
        default=3,
        description="Maximum number of tool candidates to return.",
    )
    # conversation_history inherited from BaseCommandData with automatic Message conversion
    reasoning_task_data: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional reasoning task data to condition discovery on (free-form).",
    )
    discovery_type: str = Field(
        default="all",
        description="Discovery mode: 'all', 'available', or 'capabilities'.",
    )
    filter_criteria: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional structured filters for discovery (free-form key/value).",
    )
    
    def model_dump(self, **kwargs):
        """Custom model dump to ensure enum serialization."""
        data = super().model_dump(**kwargs)
        # Ensure context_type is serialized as string value
        if 'context_type' in data and hasattr(data['context_type'], 'value'):
            data['context_type'] = data['context_type'].value
        return data


class AgentListData(BaseCommandData):
    """Data payload for agent listing operations."""
    principal_roles: List[str] = Field(
        default_factory=list,
        description="Principal roles used to filter visible agents. Empty means no role filter.",
    )


# Memory Commands
class MemoryStoreData(BaseCommandData):
    """Data payload for memory storage operations."""
    content: str = Field(
        default="",
        description="Content to store in memory (plain text).",
    )
    type: str = Field(default="note", description="Memory type (e.g., 'conversation_turn', 'note', 'tool_invocation')")
    metadata: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Optional metadata associated with the memory item (free-form key/value).",
    )
    tags: Optional[List[str]] = Field(
        default_factory=list,
        description="Optional tags to attach to the memory item (for later lookup/filtering).",
    )
    scope_type: Optional[str] = Field(
        default=None,
        description="Memory scope: 'global', 'principal', 'conversation', or 'task'. Omit for default (conversation when in conversation context).",
    )
    long_term: Optional[bool] = Field(
        default=None,
        description=(
            "If True, the memory item is indexed into the LTM vector store for "
            "semantic recall. If None, determined by memory-type "
            "heuristics (e.g. 'summary' always indexes, 'assistant_response' "
            "checks config)."
        ),
    )


class MemoryVectorIndexData(BaseCommandData):
    """Data payload for async LTM vector indexing after KV write."""

    memory_id: str = Field(
        ...,
        description="Memory item id to load from the KV store and upsert into the vector index.",
    )


class MemorySearchData(BaseCommandData):
    """Data for semantic LTM vector search (query embedding runs on workers)."""

    query: str = Field(..., description="Natural language search query.")
    top_k: int = Field(default=5, ge=1, le=100, description="Maximum number of results to return.")
    tags: Optional[List[str]] = Field(
        default=None,
        description="Optional tag filters (OR semantics, vector-store specific).",
    )


class MemoryConsolidationData(BaseCommandData):
    """Data payload for memory consolidation operations."""
    conversation_id: Optional[str] = Field(
        default=None,
        description="Optional conversation ID to consolidate within (if applicable).",
    )
    max_items: int = Field(
        default=100,
        description="Maximum number of memory items to consider during consolidation.",
    )


class MemoryTagData(BaseCommandData):
    """Data payload for memory tagging operations."""
    memory_id: str = Field(
        default="",
        description="Memory item identifier to modify tags for (single id).",
    )
    memory_ids: Optional[List[str]] = Field(
        default=None,
        description="Optional list of memory IDs for bulk tag operations.",
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Tags to add/remove/replace depending on operation.",
    )
    operation: str = Field(
        default="add",
        description="Tag operation: 'add', 'remove', or 'replace'.",
    )
    filter_tag: Optional[str] = Field(
        default=None,
        description=(
            "Optional tag to filter which memory items to retag. Combined with "
            "conversation_id, only items matching both are retagged."
        ),
    )
    conversation_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional conversation scope for bulk retag when memory_ids is omitted. "
            "Combined with filter_tag, only items matching both are retagged."
        ),
    )


class MemoryForgetData(BaseCommandData):
    """Data payload for targeted memory deletion (KV + vector)."""

    memory_ids: Optional[List[str]] = Field(
        default=None,
        description="Memory IDs to delete. Required unless conversation_id or filter_tag is set.",
    )
    conversation_id: Optional[str] = Field(
        default=None,
        description=(
            "Delete memories stamped with this conversation. Combined with "
            "filter_tag, only items matching both are deleted."
        ),
    )
    filter_tag: Optional[str] = Field(
        default=None,
        description=(
            "Delete memories that already have this tag. Combined with "
            "conversation_id, only items matching both are deleted."
        ),
    )


class MemoryRecallData(BaseCommandData):
    """Data payload for memory recall operations."""
    query: str = Field(
        default="",
        description="Natural language query for recalling memories.",
    )
    tags: Optional[List[str]] = Field(
        default_factory=list,
        description="Optional tag filter (only recall memories containing these tags).",
    )
    limit: int = Field(
        default=10,
        description="Maximum number of recalled memory items to return.",
    )
    mode: Literal["hybrid", "semantic", "recent"] = Field(
        default="hybrid",
        description="Recall strategy: 'hybrid' (default), 'semantic' (vector-only), or 'recent' (KV/recency-oriented).",
    )
    min_relevance: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum keyword relevance for hybrid recall (query coverage, head-biased). "
            "Ignored for semantic/recent modes."
        ),
    )
    conversation_id: Optional[str] = Field(
        default=None,
        description="Optional conversation scope. Omit to search across the principal.",
    )


# Conversation commands (ADR-0072, ADR-0083)
class ListConversationsData(BaseCommandData):
    """Data payload for listing conversations (tenant/principal from motet context), with agent/surface scoping."""
    limit: int = Field(default=100, description="Maximum number of conversations to return.")
    agent_id: Optional[str] = Field(
        default=None,
        description="Filter by agent (e.g. core.default). Omitted defaults to core.default.",
    )
    surface_id: Optional[str] = Field(
        default=None,
        description="Filter by surface (e.g. demo_chat, ops_dashboard). Omitted returns all surfaces for agent.",
    )


class GetConversationData(BaseCommandData):
    """Data payload for getting one conversation's details and history."""
    conversation_id: str = Field(..., description="Conversation ID to fetch.")


class ClearConversationData(BaseCommandData):
    """Data payload for clearing a conversation (registry + memory/vector)."""
    conversation_id: str = Field(
        ...,
        description=(
            "Conversation ID to clear. Isolated descendants of this conversation "
            "(spawn children and isolate_conversation steps) are cleared too."
        ),
    )


class RegisterConversationData(BaseCommandData):
    """Data payload for registering or touching a conversation in the registry. Scope is set on create only."""
    conversation_id: str = Field(..., description="Conversation ID to register or touch.")
    title: Optional[str] = Field(default=None, description="Optional display title.")
    agent_id: Optional[str] = Field(
        default=None,
        description="Agent that owns this conversation (e.g. core.default). Set on create only.",
    )
    surface_id: Optional[str] = Field(
        default=None,
        description="Surface/channel where conversation occurred (e.g. demo_chat, ops_dashboard). Set on create only.",
    )


class UpdateConversationTitleData(BaseCommandData):
    """Data payload for updating a conversation's display title (rename)."""
    conversation_id: str = Field(..., description="Conversation ID to rename.")
    title: str = Field(..., description="New display title.")


# Orchestration Commands
class MemoryResetData(BaseCommandData):
    """Data payload for memory reset operations."""
    reset_working_memory: bool = Field(
        default=True,
        description="If True, clear working memory for the current context/session.",
    )
    reset_conversation_memory: bool = Field(
        default=False,
        description="If True, clear stored conversation memory/history for the current conversation.",
    )


class WorkerLifecycleAction(str, Enum):
    """Supported worker lifecycle actions."""

    START = "start"
    STOP = "stop"
    RESTART = "restart"


class WorkerLifecycleData(BaseCommandData):
    """Data payload for worker lifecycle actions."""

    worker_id: str = Field(
        ...,
        description="Target worker ID (e.g., cloud_worker1).",
        json_schema_extra={"example": "cloud_worker1"},
    )
    action: WorkerLifecycleAction = Field(
        ...,
        description="Lifecycle action to perform.",
        json_schema_extra={"example": "restart"},
    )
    timeout_seconds: int = Field(
        default=30,
        description="Timeout in seconds for stop operations.",
        json_schema_extra={"example": 30},
    )
    requested_by: Optional[str] = Field(
        default=None,
        description="Principal ID or service account requesting the action.",
        json_schema_extra={"example": "admin-user"},
    )


class PrepareContextData(MessageFieldMixin, BaseCommandData):
    """Data payload for context preparation operations."""
    messages: List[Any] = Field(
        default_factory=list,
        description="Messages to prepare context for (Message-like dicts/objects).",
    )
    include_memory_recall: bool = Field(
        default=True,
        description="If True, include memory recall results during context preparation.",
    )
    max_context_tokens: Optional[int] = Field(
        default=None,
        description="Optional max token budget for the prepared context (if supported).",
    )
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional execution/UI context used by context providers for deterministic policy decisions.",
    )
    analysis_metadata: Optional[Any] = Field(
        default=None,
        description=(
            "Conversation analysis result for this turn (ConversationAnalysisResult "
            "or a matching dict). None when the analysis slot is off or failed."
        ),
    )

    @field_validator("analysis_metadata", mode="before")
    @classmethod
    def _coerce_analysis_metadata(cls, v: Any) -> Any:
        if v is None or not isinstance(v, dict):
            return v
        from motet.core.orchestration.turn.hook_models import parse_analysis_result

        parsed = parse_analysis_result(v)
        return parsed if parsed is not None else v


class DeriveUploadTextData(BaseCommandData):
    """Data payload for deriving extracted text from an uploaded artifact."""
    source_artifact_id: str = Field(..., description="Artifact ID of the uploaded source file")
    model_provider: Optional[str] = Field(
        default=None,
        description="Optional preferred model provider for derivations (e.g., OCR). If unset, defaults apply.",
    )
    model_name: Optional[str] = Field(
        default=None,
        description="Optional preferred model name for derivations (e.g., OCR). If unset, defaults apply.",
    )
    model_profile_name: Optional[str] = Field(
        default=None,
        description="Optional model profile name for routing/policy overrides.",
    )


class PrepareArtifactIndexData(BaseCommandData):
    """Data payload for preparing and indexing an artifact into artifact RAG."""

    source_artifact_id: str = Field(..., description="Original source artifact ID")
    derived_artifact_id: Optional[str] = Field(
        default=None,
        description="Optional derived artifact ID to prepare instead of the source payload",
    )
    strategy_id: Optional[str] = Field(default=None, description="Optional explicit preparation strategy override")
    force_reindex: bool = Field(
        default=False,
        description="If True, replace existing chunks for this source artifact before indexing",
    )


class RagRetrieveContextData(BaseCommandData):
    """Data payload for retrieving citation-ready artifact RAG context."""

    query_text: str = Field(..., description="User query text used for semantic artifact retrieval")
    scope: Literal["conversation", "principal", "motet"] = Field(
        default="conversation",
        description="Retrieval scope; conversation is the default and strictest mode",
    )
    conversation_id: Optional[str] = Field(
        default=None,
        description="Conversation ID required when scope is conversation",
    )
    role: str = Field(default="user", description="Policy role used for artifact chunk filtering")
    artifact_ids: Optional[List[str]] = Field(
        default=None,
        description="Optional source artifact include-list",
    )
    artifact_tags: Optional[List[str]] = Field(
        default=None,
        description="Optional artifact tags that narrow retrieval within the selected scope",
    )
    top_k: Optional[int] = Field(default=None, ge=1, le=50, description="Maximum chunks to retrieve")
    similarity_threshold: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Minimum normalized similarity score",
    )
    token_budget: Optional[int] = Field(
        default=None,
        ge=1,
        description="Approximate token budget for returned artifact context",
    )
    hybrid_enabled: Optional[bool] = Field(
        default=None,
        description="Enable application-layer vector/keyword hybrid retrieval",
    )
    vector_weight: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Relative vector similarity weight for hybrid scoring",
    )
    lexical_weight: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Relative keyword/phrase match weight for hybrid scoring",
    )
    candidate_multiplier: Optional[int] = Field(
        default=None,
        ge=1,
        le=20,
        description="Over-fetch multiplier used before hybrid reranking",
    )
    position_ordered: Optional[bool] = Field(
        default=None,
        description=(
            "When True with artifact_ids, return chunks in source position/timestamp order "
            "instead of similarity ranking."
        ),
    )


class DeriveUploadImageData(BaseCommandData):
    """Data payload for deriving image artifacts (thumb, base, detail) from an uploaded image."""
    source_artifact_id: str = Field(..., description="Artifact ID of the uploaded source image")
    derivation_names: Optional[List[str]] = Field(
        default=None,
        description="List of derivation names to generate (default: ['thumb', 'base']). Options: 'thumb', 'base', 'detail'"
    )
    force_regenerate: bool = Field(
        default=False,
        description="If True, regenerate derivations even if they already exist"
    )


class DeriveVideoVisualsData(BaseCommandData):
    """Data payload for deriving poster/keyframes from an uploaded video."""

    source_artifact_id: str = Field(..., description="Artifact ID of the uploaded source video")
    keyframe_strategy: str = Field(
        default="scene",
        description="Keyframe selection strategy: scene or interval",
        json_schema_extra={"example": "scene"},
    )
    max_keyframes: int = Field(default=12, le=60, description="Hard cap on extracted keyframes")
    force_regenerate: bool = Field(
        default=False,
        description="If True, regenerate derivations even if they already exist",
    )


class DeriveVideoTranscriptData(BaseCommandData):
    """Data payload for deriving a transcript from an uploaded video."""

    source_artifact_id: str = Field(..., description="Artifact ID of the uploaded source video")
    force_regenerate: bool = Field(
        default=False,
        description="If True, regenerate the transcript even if one already exists",
    )


class DeriveOfficeEmbeddedImagesData(BaseCommandData):
    """Data payload for extracting embedded images from an uploaded office document."""

    source_artifact_id: str = Field(..., description="Artifact ID of the uploaded DOCX/PPTX source document")
    image_derivation_names: Optional[List[str]] = Field(
        default_factory=lambda: ["thumb", "base"],
        description="Image derivations to generate for each embedded image artifact",
    )
    force_regenerate: bool = Field(
        default=False,
        description="If True, store embedded images even if prior extracted images exist for the source",
    )
    run_ocr: bool = Field(
        default=True,
        description="If True, dispatch OCR/indexing for each extracted embedded image",
    )
    model_provider: Optional[str] = Field(
        default=None,
        description="Optional preferred model provider for embedded image OCR.",
    )
    model_name: Optional[str] = Field(
        default=None,
        description="Optional preferred vision-capable model name for embedded image OCR.",
    )
    model_profile_name: Optional[str] = Field(
        default=None,
        description="Optional model profile name for routing/policy overrides.",
    )


class OCREmbeddedImageData(BaseCommandData):
    """Data payload for OCR/caption indexing of one embedded office image."""

    source_artifact_id: str = Field(..., description="Original office document artifact ID")
    image_artifact_id: str = Field(..., description="Embedded image artifact ID to OCR")
    content_type: str = Field(..., description="MIME type of the embedded image")
    model_provider: Optional[str] = Field(
        default=None,
        description="Optional preferred model provider for OCR.",
    )
    model_name: Optional[str] = Field(
        default=None,
        description="Optional preferred vision-capable model name for OCR.",
    )
    model_profile_name: Optional[str] = Field(
        default=None,
        description="Optional model profile name for routing/policy overrides.",
    )


class DerivePdfPageImagesData(BaseCommandData):
    """Data payload for deriving per-page images from an uploaded PDF."""

    source_artifact_id: str = Field(..., description="Artifact ID of the uploaded source PDF")
    dpi: int = Field(
        default=200,
        ge=72,
        le=600,
        description="Rasterization DPI when converting PDF pages to images. Higher = better OCR, slower/larger."
    )
    force_regenerate: bool = Field(
        default=False,
        description="If True, regenerate page images even if they already exist"
    )


class OCRImagePageData(BaseCommandData):
    """Data payload for OCR extraction from an image page using vision models."""
    image_artifact_id: str = Field(..., description="Artifact ID of the image to OCR")
    content_type: str = Field(..., description="MIME type of the image (e.g., image/png)")
    page_num: Optional[int] = Field(
        default=None,
        description="Page number for reference (if from PDF)"
    )
    source_artifact_id: Optional[str] = Field(
        default=None,
        description="Source artifact ID (e.g., PDF) this page came from"
    )
    model_provider: Optional[str] = Field(
        default=None,
        description="Optional preferred model provider for OCR (e.g., 'openai', 'anthropic').",
    )
    model_name: Optional[str] = Field(
        default=None,
        description="Optional preferred vision-capable model name for OCR.",
    )
    model_profile_name: Optional[str] = Field(
        default=None,
        description="Optional model profile name for routing/policy overrides.",
    )


class CreateArtifactData(BaseCommandData):
    """Data payload for creating an artifact and optionally triggering derivations."""

    payload: bytes = Field(..., description="Raw artifact bytes to store")
    content_type: str = Field(..., description="MIME type (e.g., application/pdf, image/png)")
    kind: str = Field(default="user_upload", description="Artifact kind (e.g., user_upload, tool_artifact)")
    filename: Optional[str] = Field(default=None, description="Original filename for metadata")
    conversation_id: Optional[str] = Field(default=None, description="Conversation ID to store in artifact metadata")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Additional artifact metadata to store")
    prep_hints: Optional[ArtifactPrepHints] = Field(
        default=None,
        description="Structured preparation hints to store with artifact metadata.",
    )
    ttl_seconds: Optional[int] = Field(
        default=None,
        description="Optional artifact time-to-live in seconds for transient artifacts such as tool outputs.",
    )
    trigger_derivations: bool = Field(
        default=True,
        description="If True, trigger derivations based on content_type and kind (text extraction, image derivations).",
    )
    image_derivation_names: Optional[List[str]] = Field(
        default=None,
        description="If set and content_type is image/*, derive these image derivations (default: ['thumb','base','detail']).",
    )
    include_text_derivation_for_json: bool = Field(
        default=True,
        description="If True, treat application/json as eligible for text derivation trigger.",
    )

    @field_serializer('payload')
    def serialize_payload(self, value: bytes) -> str:
        """Base64-encode bytes for JSON serialization."""
        import base64
        return base64.b64encode(value).decode('utf-8')

    @field_validator('payload', mode='before')
    @classmethod
    def decode_base64_payload(cls, v):
        """Handle base64-encoded strings from JSON deserialization."""
        if isinstance(v, str):
            # Assume base64-encoded string from JSON transport
            import base64
            try:
                return base64.b64decode(v)
            except Exception:
                # If base64 decode fails, try UTF-8 (for backward compatibility)
                return v.encode('utf-8')
        return v

    @field_validator("prep_hints", mode="before")
    @classmethod
    def _coerce_prep_hints(cls, value: Any) -> Any:
        if value is None or isinstance(value, ArtifactPrepHints):
            return value
        if isinstance(value, dict):
            return ArtifactPrepHints.model_validate(value)
        return value

class FinalizeTurnData(MessageFieldMixin, BaseCommandData):
    """Data payload for turn finalization operations."""
    messages: List[Any] = Field(
        default_factory=list,
        description="Conversation messages involved in the finalized turn (Message-like dicts/objects).",
    )
    assistant_response: str = Field(
        default="",
        description="Final assistant response text to store with the conversation/memory.",
    )
    agent_id: Optional[str] = Field(
        default=None,
        description=(
            "Qualified registry id for the agent that produced this turn (canonical transcript "
            "and assistant_response memory metadata). When omitted, resolved from execution context."
        ),
    )
    store_conversation: bool = Field(
        default=True,
        description="If True, store this turn in conversation history storage.",
    )
    update_memory: bool = Field(
        default=True,
        description="If True, update long-term memory based on this completed turn.",
    )
    root_turn: Optional[bool] = Field(
        default=None,
        description=(
            "Whether this turn belongs to the root (top-level) agent. "
            "False for sub-agent turns in panel/workflow executions. "
            "Propagated to transcript metadata for deterministic replay ordering."
        ),
    )
    root_agent_id: Optional[str] = Field(
        default=None,
        description=(
            "Qualified id of the **conversation primary** agent for transcript replay ordering "
            "(e.g. core.default from the chat UI). Stored on transcript metadata so nested "
            "workflow agents can be ordered before this agent. When omitted, legacy behavior "
            "uses the turn author id."
        ),
    )
    transcript_sequence: Optional[int] = Field(
        default=None,
        description="Pre-reserved transcript sequence number for deterministic ordering.",
    )
    pending_action_carry: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "unconsumed pending-action marker to re-attach to this turn's "
            "root assistant message (deferral carry-forward). Already incremented and "
            "cap-checked by the reader in agent_turn; ignored when this turn's response "
            "itself asks a new question (a fresh proposal wins over carry)."
        ),
    )
    thinking_text: Optional[str] = Field(
        default=None,
        description=(
            "Provider reasoning for this turn, stored for conversation reload. "
            "Omitted from next-turn model replay."
        ),
    )
    tool_summaries: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "Short tool name, status, and preview for conversation reload. "
            "Optional step is the loop iteration for sidebar Step N. "
            "Omitted from next-turn model replay. Not full tool payloads."
        ),
    )
    cost_usd: Optional[float] = Field(
        default=None,
        description=(
            "Estimated USD for this agent's priced model calls. "
            "Omitted when unpriced. Not next-turn model replay content."
        ),
    )
    spawn_children: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "Card pointers to isolated spawn conversations created this turn. "
            "Omitted from next-turn model replay."
        ),
    )


class AgentTurnData(MessageFieldMixin, BaseCommandData):
    """Data payload for registry-driven agent turn execution."""

    agent_id: Optional[str] = Field(
        default=None,
        description="Agent identifier (qualified or alias). None resolves to core.default.",
    )
    messages: List[Any] = Field(
        default_factory=list,
        description=(
            "Messages for the current turn. Must be a list of {role, content} items "
            '(Message-like dict/object), e.g. [{"role": "user", "content": "..."}].'
        ),
    )
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional additional execution context (e.g., UI state, user attributes, request metadata).",
    )
    output_contract: Optional[OutputContract] = Field(
        default=None,
        description=(
            "Per-call structured-output contract. Wins over the agent default. "
            "Constrains one finalize model call after the loop stops."
        ),
    )

    @field_validator("output_contract", mode="before")
    @classmethod
    def _coerce_turn_output_contract(cls, v: Any) -> Optional[OutputContract]:
        if v is None:
            return None
        if isinstance(v, OutputContract):
            return v
        if isinstance(v, dict):
            return OutputContract(**v)
        raise ValueError(f"output_contract must be OutputContract or dict, got {type(v).__name__}")


class PageContextData(MessageFieldMixin, BaseCommandData):
    """Data payload for page-context turn hook injection."""
    messages: List[Any] = Field(
        default_factory=list,
        description="Current turn messages (optional; used for context-sensitive hook logic).",
    )
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Request context containing page_context/current_page/page_state metadata.",
    )


# Workflow Commands
class WorkflowListData(BaseCommandData):
    """Data payload for workflow listing operations."""
    bundle_id: Optional[str] = Field(
        default=None,
        description="Optional bundle namespace filter (e.g. 'calculator' for calculator.* workflows).",
    )
    name_contains: Optional[str] = Field(
        default=None,
        description="Optional case-insensitive substring filter over workflow_id and description.",
    )
    include_steps: bool = Field(
        default=False,
        description="If True, include step payloads in each workflow entry.",
    )
    limit: Optional[int] = Field(
        default=100,
        description="Maximum number of workflows to return.",
    )
    offset: int = Field(
        default=0,
        description="Number of workflows to skip (for pagination).",
    )


class WorkflowExecutionData(BaseCommandData):
    """Data payload for workflow execution operations."""
    workflow_id: str = Field(default="", description="Workflow identifier (optional, depending on workflow source).")
    workflow_name: str = Field(default="", description="Human-readable workflow name.")
    workflow_steps: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Ordered list of workflow steps to execute (workflow-specific structure).",
    )
    max_parallel_steps: int = Field(default=3, description="Maximum number of workflow steps to run in parallel.")
    enable_parallel_execution: bool = Field(default=True, description="If True, allow parallel execution of independent steps.")
    retry_failed_steps: bool = Field(default=True, description="If True, retry failed steps up to max_step_retries.")
    max_step_retries: int = Field(default=2, description="Maximum retries per step when retry_failed_steps is True.")
    context: Dict[str, Any] = Field(
        default_factory=dict,
        description="Workflow context for parameter substitution and step templating (free-form key/value).",
    )
    description: str = Field(default="", description="Workflow description / summary.")
    handback_tools: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Client tool schemas for ownership=handback steps (non-agent workflow entry; issue #149).",
    )
    # conversation_history inherited from BaseCommandData with automatic Message conversion
    # reasoning_task inherited from BaseCommandData


class ResumeWorkflowData(BaseCommandData):
    """Tagged resume payload for a paused workflow run (issue #149)."""

    workflow_run_id: str = Field(
        default="",
        description="Paused workflow run id from the WorkflowCheckpoint.",
    )
    interaction_id: Optional[str] = Field(
        default=None,
        description="Optional pending interaction id used to resolve workflow_run_id via index.",
    )
    kind: str = Field(
        default="handback_tools",
        description=(
            "Resume kind: handback_tools | elicitation | confirmation | oauth | operator."
        ),
    )
    resume_epoch: Optional[int] = Field(
        default=None,
        description="Optional expected resume_epoch; claim rejects mismatch (idempotency).",
    )
    observations: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Handback observations: [{tool_call_id, content}, ...] for kind=handback_tools.",
    )
    answers: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Structured elicitation answers for kind=elicitation.",
    )
    decision: Optional[str] = Field(
        default=None,
        description="approve | reject for kind=confirmation.",
    )
    edited_parameters: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional edited tool parameters when confirming with edits.",
    )
    auth_status: Optional[str] = Field(
        default=None,
        description="completed | failed for kind=oauth.",
    )


class WorkflowRunsListData(BaseCommandData):
    """List checkpointed workflow runs for the current tenant/motet."""

    status: str = Field(
        default="paused",
        description="Filter: paused (default). Other values reserved for future expansion.",
    )
    limit: int = Field(default=50, description="Max runs to return.")
    offset: int = Field(default=0, description="Pagination offset.")


class WorkflowRunControlData(BaseCommandData):
    """Operator pause or cancel for a checkpointed workflow run."""

    workflow_run_id: str = Field(
        ...,
        description="Workflow run id to pause or cancel.",
    )
    action: str = Field(
        ...,
        description="pause | cancel",
    )
    reason: Optional[str] = Field(
        default=None,
        description="Optional operator reason recorded on the control signal / cancel.",
    )


# Conversation Analysis Commands
class ConversationAnalysisData(MessageFieldMixin, BaseCommandData):
    """Data payload for conversation analysis operations."""
    analysis_type: str = Field(
        default="complexity",
        description="Analysis type to run (e.g., 'complexity', 'intent', 'sentiment').",
    )
    messages: List[Any] = Field(
        default_factory=list,
        description="Messages to analyze (Message-like dicts/objects).",
    )
    conversation_context: Optional[List[Any]] = Field(
        default=None,
        description="Optional additional conversation context messages (Message-like dicts/objects).",
    )
    analysis_model: Optional[str] = Field(
        default=None,
        description="Optional model name to use for analysis (provider-specific).",
    )
    analysis_dimensions: Optional[List[str]] = Field(
        default=None,
        description="Optional list of analysis dimensions to compute (analysis-type specific).",
    )
    
    @field_validator('conversation_context', mode='before')
    @classmethod
    def convert_conversation_context(cls, v):
        """Automatically convert message dicts to Message objects (conversation_context-specific)"""
        from motet.core.commands.base_command_data import _deserialize_messages
        return _deserialize_messages(v)


# Schedule Commands
class ScheduleData(BaseModel):
    """Data payload for schedule operations - simplified to avoid field conflicts."""
    
    # Target command to schedule
    target_command_type: str = Field(
        ...,
        description="Command type to schedule (must be a registered distributed command type).",
    )
    target_command_data: Dict[str, Any] = Field(
        ...,
        description="Command payload/data for the target command type (must match that command's schema).",
    )
    
    # Schedule metadata
    name: Optional[str] = Field(
        default=None,
        description="Optional human-readable schedule name.",
    )
    
    # Scheduling parameters
    schedule_type: str = Field(
        ...,
        description="Schedule type: 'immediate', 'delayed', 'recurring', or 'conditional'.",
    )
    scheduled_at: Optional[datetime] = Field(
        default=None,
        description="For delayed schedules: datetime at which to execute the command (UTC recommended).",
    )
    delay_seconds: Optional[int] = Field(
        default=None,
        description=(
            "For delayed schedules: relative delay from schedule-creation time in seconds. "
            "Alternative to scheduled_at; resolved to an absolute UTC scheduled_at at create time."
        ),
    )
    cron_expression: Optional[str] = Field(
        default=None,
        description="For recurring schedules: cron expression (e.g., '*/5 * * * *').",
    )
    interval_seconds: Optional[int] = Field(
        default=None,
        description="For recurring schedules: interval in seconds (alternative to cron_expression).",
    )
    condition_expression: Optional[str] = Field(
        default=None,
        description="For conditional schedules: expression that determines when to execute (implementation-dependent).",
    )
    
    # Command parameters
    timeout_seconds: int = Field(
        default=300,
        description="Timeout for the scheduled command execution (seconds).",
    )
    priority: int = Field(
        default=5,
        description="Command priority (higher values run sooner / are more important).",
    )
    max_retries: int = Field(
        default=3,
        description="Maximum retry attempts for the scheduled command execution.",
    )
    
    # Worker targeting
    target_worker_id: Optional[str] = Field(
        default=None,
        description="Optional specific worker_id to target for execution.",
    )
    preferred_worker_ids: List[str] = Field(
        default_factory=list,
        description="Optional list of preferred worker_ids (invoker will choose among these if possible).",
    )
    worker_affinity: Optional[str] = Field(
        default=None,
        description="Optional affinity key for consistent worker selection.",
    )
    avoid_worker_ids: List[str] = Field(
        default_factory=list,
        description="Optional list of worker_ids to avoid when routing the scheduled command.",
    )
    # Full execution context for the scheduled command run (agent_id, surface_id, principal_roles,
    # model_provider, model_name, model_profile_name, enable_thinking, reasoning_effort, etc.).
    # Written to the target command's distributed_context.metadata at schedule creation.
    schedule_context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Execution context dict stored on the target command envelope metadata. Carries model, chat, and surface context for scheduled runs.",
    )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert this ScheduleData instance to a dictionary."""
        return self.model_dump()


# Concurrency Commands (ADR-0023)
class GatherCommandData(BaseCommandData):
    """
    Data payload for parallel command execution using Celery group.
    
    GatherCommand (formerly GroupCommand) executes multiple commands in parallel across 
    distributed workers, aggregating results based on the specified strategy.
    
    Celery Concurrency Command Wrappers
    """
    commands: List[str] = Field(
        default_factory=list,
        description="List of serialized command transport JSON strings to execute in parallel.",
    )
    aggregation_strategy: str = Field(
        default="all_results",
        description="Result aggregation strategy (e.g., 'all_results', 'first_success', 'majority_vote').",
    )
    fail_fast: bool = Field(
        default=False,
        description="If True, stop on first failure (may return partial results depending on strategy).",
    )
    max_parallel: Optional[int] = Field(
        default=None,
        description="Optional limit on concurrent execution fan-out.",
    )


class DispatchCommandData(BaseCommandData):
    """
    Data payload for fire-and-forget parallel command execution.
    
    DispatchCommand dispatches multiple commands to workers without waiting for results.
    Useful for background tasks, bulk operations, or when results aren't needed.
    
    Celery Concurrency Command Wrappers
    """
    commands: List[str] = Field(
        default_factory=list,
        description="List of serialized command transport JSON strings to dispatch (fire-and-forget).",
    )
    max_parallel: Optional[int] = Field(
        default=None,
        description="Optional limit on concurrent dispatch fan-out.",
    )


class MapCommandData(BaseCommandData):
    """
    Data payload for batch processing with the same command type.
    
    MapCommand applies the same command template to multiple input sets,
    similar to Python's map function but distributed across workers.
    
    Celery Concurrency Command Wrappers
    """
    command_type: str = Field(
        ...,
        description="Type of command to instantiate for each input (e.g., 'core.tool_execution').",
    )
    command_template: Dict[str, Any] = Field(
        ...,
        description="Base command payload shared across all executions (merged with each entry in inputs).",
    )
    inputs: List[Dict[str, Any]] = Field(
        ...,
        description="List of per-execution input payload overrides to apply to the command_template.",
    )
    batch_size: Optional[int] = Field(
        default=None,
        description="Optional concurrency limit: process N inputs at a time.",
    )
    aggregation_strategy: str = Field(
        default="all_results",
        description="Result aggregation strategy (e.g., 'all_results', 'first_success', 'majority_vote').",
    )
    fail_fast: bool = Field(
        default=False,
        description="If True, stop processing on the first failure.",
    )


# ========================================
# Self-Registration of Core Data Classes
# ========================================
# This block runs automatically when the module is imported, registering
# all core command data classes with the global registry.
# No circular imports: command_data_registry doesn't import this module.

def _register_core_data_classes():
    """Register all core command data classes."""
    registry = command_data_registry
    
    # Model commands
    registry.register("core.model_inference", ModelInferenceData)
    registry.register("core.model_stream", ModelStreamData)
    registry.register("core.embedding_generation", EmbeddingData)
    registry.register("core.image_generation", ImageGenerationData)
    
    # Tool commands
    registry.register("core.tool_execution", ToolExecutionData)
    registry.register("core.tool_list", ToolListData)
    registry.register("core.tool_discovery", ToolDiscoveryData)
    registry.register("core.agent_list", AgentListData)
    
    # Note: AgentData is registered in react/agent_data.py (same pattern as AgenticLoopData)
    
    # Planning commands
    
    # Memory commands
    registry.register("core.memory_store", MemoryStoreData)
    registry.register("core.memory_vector_index", MemoryVectorIndexData)
    registry.register("core.memory_search", MemorySearchData)
    registry.register("core.memory_consolidation", MemoryConsolidationData)
    registry.register("core.memory_tag", MemoryTagData)
    registry.register("core.memory_forget", MemoryForgetData)
    registry.register("core.memory_recall", MemoryRecallData)
    
    # Orchestration commands
    registry.register("core.memory_reset", MemoryResetData)
    registry.register("core.prepare_context", PrepareContextData)
    registry.register("core.derive_upload_text", DeriveUploadTextData)
    registry.register("core.derive_upload_image", DeriveUploadImageData)
    registry.register("core.derive_video_visuals", DeriveVideoVisualsData)
    registry.register("core.derive_video_transcript", DeriveVideoTranscriptData)
    registry.register("core.derive_office_embedded_images", DeriveOfficeEmbeddedImagesData)
    registry.register("core.ocr_embedded_image", OCREmbeddedImageData)
    registry.register("core.derive_pdf_page_images", DerivePdfPageImagesData)
    registry.register("core.ocr_image_page", OCRImagePageData)
    registry.register("core.create_artifact", CreateArtifactData)
    registry.register("core.prepare_artifact_index", PrepareArtifactIndexData)
    registry.register("core.rag_retrieve_context", RagRetrieveContextData)
    registry.register("core.finalize_turn", FinalizeTurnData)
    registry.register("core.page_context", PageContextData)
    registry.register("core.agent_turn", AgentTurnData)
    
    # Workflow commands
    registry.register("core.workflow_list", WorkflowListData)
    registry.register("core.workflow_execution", WorkflowExecutionData)
    # registry.register("full_workflow", FullWorkflowData)  # REMOVED - ADR-0049
    
    # Conversation analysis commands
    registry.register("core.conversation_analysis", ConversationAnalysisData)
    
    # Schedule commands
    registry.register("core.schedule", ScheduleData)
    
    # Concurrency commands (ADR-0023)
    registry.register("core.gather", GatherCommandData)
    registry.register("core.dispatch", DispatchCommandData)
    registry.register("core.map", MapCommandData)


# Auto-register on module import
_register_core_data_classes()


# ========================================
# Backward Compatibility Functions
# ========================================
# These functions delegate to command_data_registry for backward compatibility.

def get_command_data_class(command_type: str) -> Optional[type]:
    """
    Get the command data class for a given command type.
    
    Delegates to command_data_registry.get() for unified lookup.
    If not found and command_type does not start with "core.", tries "core." + command_type
    so that bare names (e.g. "agent_turn") resolve to core-registered types ("core.agent_turn").
    
    Args:
        command_type: Type of command
        
    Returns:
        Command data class or None if not found
    """
    data_class = command_data_registry.get(command_type)
    if data_class is None and not command_type.startswith("core."):
        data_class = command_data_registry.get("core." + command_type)
    return data_class


def validate_command_data(command_type: str, command_data: Any) -> Optional[str]:
    """
    Validate a ``command_data`` payload against the registered data class.

    Returns None when the payload is usable, otherwise an actionable error string
    suitable for returning to an LLM or API caller.

    Unknown keys are rejected rather than ignored. Pydantic drops extras silently,
    which let malformed payloads (e.g. ``core.agent_turn`` with ``message`` instead
    of ``messages``) be accepted at schedule-creation time and then fail much later
    in a worker, once per firing, with an unrelated-looking provider error.

    Args:
        command_type: Target command type (bare names resolve against "core.")
        command_data: Payload the caller intends to send as the command's data model

    Returns:
        Error message string, or None when valid
    """
    if not isinstance(command_data, dict):
        return f"command_data must be an object, got {type(command_data).__name__}"

    data_class = get_command_data_class(command_type)
    if data_class is None:
        # Unregistered command types (e.g. bundle-provided) have no schema to check.
        return None

    unknown_keys = unknown_command_data_keys(data_class, command_data)
    if unknown_keys:
        known_keys = set(getattr(data_class, "model_fields", None) or {})
        # The overwhelmingly common mistake is a single-string turn input under a
        # misnamed key; point straight at the canonical shape so an LLM caller can
        # self-correct in one retry.
        hint = ""
        if "messages" in known_keys and any(
            key in ("message", "prompt", "input", "text") for key in unknown_keys
        ):
            hint = ' Did you mean "messages": [{"role": "user", "content": "..."}]?'
        return (
            f"unknown command_data field(s) for {command_type}: {', '.join(unknown_keys)}. "
            f"Valid fields: {', '.join(sorted(known_keys))}. "
            "Call command_describe for the full schema."
            f"{hint}"
        )

    try:
        data_class(**command_data)
    except Exception as exc:
        return (
            f"invalid command_data for {command_type}: {exc}. "
            "Call command_describe for the full schema."
        )
    return None


def create_command_data(command_type: str, **kwargs) -> BaseCommandData:
    """
    Create a command data instance for a given command type.
    
    Args:
        command_type: Type of command
        **kwargs: Arguments for the command data class
        
    Returns:
        Command data instance
        
    Raises:
        ValueError: If command type is not supported
    """
    data_class = get_command_data_class(command_type)
    if not data_class:
        raise ValueError(f"Unsupported command type: {command_type}")
    return data_class(**kwargs)


def get_all_command_data_classes() -> Dict[str, type]:
    """
    Get all available command data classes.
    
    Delegates to command_data_registry.get_all().
    
    Returns:
        Dictionary mapping command types to their data classes
    """
    return command_data_registry.get_all()


def get_command_types() -> List[str]:
    """
    Get all available command types.
    
    Delegates to command_data_registry.get_types().
    
    Returns:
        List of command type strings
    """
    return command_data_registry.get_types()


# Deprecated: COMMAND_DATA_CLASSES dict
# For backward compatibility, provide a property-like dict that delegates to registry
class _CommandDataClassesDict(dict):
    """Backward compatibility dict that delegates to command_data_registry."""
    
    def __getitem__(self, key):
        result = command_data_registry.get(key)
        if result is None:
            raise KeyError(key)
        return result
    
    def get(self, key, default=None):
        result = command_data_registry.get(key)
        return result if result is not None else default
    
    def keys(self):  # type: ignore[override]
        return command_data_registry.get_types()
    
    def values(self):
        return command_data_registry.get_all().values()
    
    def items(self):
        return command_data_registry.get_all().items()
    
    def __contains__(self, key):
        return command_data_registry.is_registered(key)
    
    def __len__(self):
        return len(command_data_registry.get_all())
    
    def copy(self):
        return command_data_registry.get_all()


COMMAND_DATA_CLASSES = _CommandDataClassesDict()