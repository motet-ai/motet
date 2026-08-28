"""
Motet - Artifact Preparation Models

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Defines the canonical artifact preparation contract used by
    preparation strategies, RAG indexing, API status responses, and citation
    rendering. The models separate deterministic preparation from retrieval so
    text, JSON, and office-document artifacts can share one chunk contract.

Dependencies:
    - pydantic for strict validation and discriminated unions
    - typing for Literal and Annotated model definitions

Usage:
    manifest = ArtifactPrepManifest(strategy_id="text_default", strategy_version="1.0.0", ...)
    chunk = PreparedArtifactChunk(..., coordinates=TextCoord(byte_start=0, byte_end=100))

Notes:
    - Retrieval scores live on ArtifactChunkSearchResult in motet.core.rag.types.
    - Worker capability routing remains on RegisteredTool.required_capabilities;
      the preparation manifest intentionally does not duplicate that field.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field


ArtifactPrepState = Literal["prep_pending", "prep_running", "prep_complete", "prep_partial", "prep_failed"]
ArtifactIndexState = Literal["index_pending", "index_running", "index_complete", "index_failed", "indexing_disabled"]
PrepDecisionSource = Literal["dispatch", "planner"]
PrepCostClass = Literal["cheap", "moderate", "expensive"]
ArtifactModality = Literal["text", "code", "structured", "image", "audio", "video"]
ChunkKind = Literal[
    "text",
    "section",
    "table",
    "code_symbol",
    "json_object",
    "ocr_region",
    "caption",
    "transcript_segment",
    "video_scene",
]


class ArtifactFeatureMatch(BaseModel):
    """Matcher that declares which artifact features a preparation strategy handles."""

    kinds: list[str] = Field(default_factory=list, description="ArtifactKind values this strategy handles")
    content_types: list[str] = Field(default_factory=list, description="Exact or wildcard MIME types")
    extensions: list[str] = Field(default_factory=list, description="File extensions including the leading dot")
    metadata_hints: dict[str, Any] = Field(default_factory=dict, description="Optional metadata keys/values to match")
    min_bytes: Optional[int] = Field(default=None, ge=0, description="Minimum artifact size in bytes")
    max_bytes: Optional[int] = Field(default=None, ge=0, description="Maximum artifact size in bytes")


class ArtifactPrepManifest(BaseModel):
    """Manifest attached to a registered tool that marks it as a preparation strategy."""

    strategy_id: str = Field(..., description="Stable strategy identifier")
    strategy_version: str = Field(..., description="Semantic version that participates in cache keys")
    handles: list[ArtifactFeatureMatch] = Field(default_factory=list, description="Feature matchers this strategy supports")
    priority: int = Field(default=0, description="Tie-break priority; higher wins")
    cost_class: PrepCostClass = Field(default="cheap", description="Expected deterministic cost class")
    produces_chunk_kinds: list[ChunkKind] = Field(
        default_factory=list,
        description="Chunk kinds this strategy can emit",
    )
    fallback_chain: list[str] = Field(default_factory=list, description="Strategy IDs to try after partial/failure")


class ArtifactPrepHints(BaseModel):
    """Structured caller hints that influence preparation without using ad-hoc upload query parameters."""

    prep_strategy_id: Optional[str] = Field(default=None, description="Explicit strategy override")
    disable_strategies: list[str] = Field(default_factory=list, description="Strategy IDs disabled for this artifact")
    model_provider: Optional[str] = Field(default=None, description="Optional model provider hint for enrichment steps")
    model_name: Optional[str] = Field(default=None, description="Optional model name hint for enrichment steps")
    model_profile_name: Optional[str] = Field(default=None, description="Optional model profile hint for enrichment steps")
    extra: dict[str, Any] = Field(default_factory=dict, description="Additional strategy-specific hints")


class ArtifactPayloadInfo(BaseModel):
    """Small payload summary used during planning and dry-run selection."""

    content_type: str = Field(default="application/octet-stream", description="Artifact MIME type")
    extension: Optional[str] = Field(default=None, description="Filename extension including leading dot")
    bytes: int = Field(default=0, ge=0, description="Payload size in bytes")
    content_hash: Optional[str] = Field(default=None, description="Source payload SHA256 when known")
    filename: Optional[str] = Field(default=None, description="Original filename when known")


class ArtifactPrepStep(BaseModel):
    """Declarative step in an artifact preparation plan."""

    name: str = Field(..., description="Step name, such as extract_text or chunk_json")
    parameters: dict[str, Any] = Field(default_factory=dict, description="Step parameters")


class ArtifactPrepPlan(BaseModel):
    """Declarative preparation plan emitted by deterministic dispatch or the planner."""

    source_artifact_id: Optional[str] = Field(default=None, description="Source artifact ID when known")
    strategy_id: str = Field(..., description="Selected preparation strategy")
    strategy_version: str = Field(..., description="Selected strategy version")
    prep_decision_source: PrepDecisionSource = Field(default="dispatch", description="Selection source")
    steps: list[ArtifactPrepStep] = Field(default_factory=list, description="Steps the executor will run")
    expected_chunk_kinds: list[ChunkKind] = Field(default_factory=list, description="Chunk kinds expected from the run")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Selection confidence")
    diagnostics: list[str] = Field(default_factory=list, description="Planning diagnostics")
    canonical_config_hash: str = Field(default="", description="Hash of strategy-affecting configuration")
    planner_decision_hash: str = Field(default="", description="Planner decision hash for cold-path plans")


class TextCoord(BaseModel):
    """Coordinate for text-like chunks."""

    kind: Literal["text"] = "text"
    byte_start: int = Field(default=0, ge=0, description="UTF-8 byte offset where the chunk begins")
    byte_end: int = Field(default=0, ge=0, description="UTF-8 byte offset where the chunk ends")
    page_number: Optional[int] = Field(default=None, ge=1, description="Best-effort page number")
    heading_path: list[str] = Field(default_factory=list, description="Heading path for the chunk")
    extraction_method: Optional[str] = Field(default=None, description="Text extraction method")


class CodeCoord(BaseModel):
    """Coordinate for source-code chunks."""

    kind: Literal["code"] = "code"
    file_path: str = Field(..., description="Repository-relative file path")
    symbol_path: list[str] = Field(default_factory=list, description="Nested symbol path")
    line_start: int = Field(..., ge=1, description="One-based starting line")
    line_end: int = Field(..., ge=1, description="One-based ending line")
    language: str = Field(..., description="Programming language")


class JsonCoord(BaseModel):
    """Coordinate for JSON/API/tool-output chunks."""

    kind: Literal["json"] = "json"
    pointer: str = Field(default="", description="RFC 6901 JSON pointer")
    object_kind: Optional[str] = Field(default=None, description="Object classification such as tool_result")


class TableCoord(BaseModel):
    """Coordinate for tabular chunks."""

    kind: Literal["table"] = "table"
    workbook: Optional[str] = Field(default=None, description="Workbook or document name")
    sheet: Optional[str] = Field(default=None, description="Sheet or table name")
    range: str = Field(default="", description="Table/range identifier such as A1:D24")
    headers: list[str] = Field(default_factory=list, description="Detected table headers")
    page_number: Optional[int] = Field(default=None, ge=1, description="Page number for document tables")


class MediaCoord(BaseModel):
    """Coordinate for future media chunks."""

    kind: Literal["media"] = "media"
    page: Optional[int] = Field(default=None, ge=1, description="Document page number")
    region: Optional[tuple[float, float, float, float]] = Field(
        default=None,
        description="Normalized region as x, y, width, height",
    )
    timestamp_start: Optional[float] = Field(default=None, ge=0.0, description="Start timestamp in seconds")
    timestamp_end: Optional[float] = Field(default=None, ge=0.0, description="End timestamp in seconds")
    speaker: Optional[str] = Field(default=None, description="Speaker label")
    frame: Optional[int] = Field(default=None, ge=0, description="Frame number")


ChunkCoordinate = Annotated[
    Union[TextCoord, CodeCoord, JsonCoord, TableCoord, MediaCoord],
    Field(discriminator="kind"),
]


class PreparedArtifactChunk(BaseModel):
    """A prepared artifact chunk ready for embedding, indexing, retrieval, and citation."""

    source_artifact_id: str = Field(..., description="Original source artifact ID")
    derived_artifact_id: Optional[str] = Field(default=None, description="Derived artifact ID when one contributed")
    enrichment_artifact_ids: list[str] = Field(default_factory=list, description="Related enrichment artifacts")
    chunk_index: int = Field(..., ge=0, description="Zero-based chunk index for this source/strategy")
    chunk_kind: ChunkKind = Field(default="text", description="Chunk kind discriminator")
    content_text: str = Field(..., description="Text used for embedding and prompt context")
    structured_payload: Optional[dict[str, Any]] = Field(default=None, description="Optional structured source data")
    content_hash: str = Field(..., description="SHA256 hash of content_text and structured payload")
    coordinates: ChunkCoordinate = Field(..., description="Type-specific source coordinate")
    tenant_id: str = Field(..., description="Tenant isolation identifier")
    principal_id: str = Field(..., description="Principal isolation identifier")
    motet_id: str = Field(..., description="Motet/environment isolation identifier")
    role: str = Field(default="user", description="Policy role for retrieval filtering")
    conversation_id: str = Field(default="", description="Conversation scope identifier")
    content_type: str = Field(default="text/plain", description="Source artifact content type")
    filename: Optional[str] = Field(default=None, description="Original filename for citation display")
    artifact_tags: list[str] = Field(default_factory=list, description="Tags copied from source metadata")
    modality: ArtifactModality = Field(default="text", description="High-level artifact modality")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Preparation confidence")
    prep_strategy_id: str = Field(..., description="Preparation strategy that emitted this chunk")
    prep_strategy_version: str = Field(..., description="Preparation strategy version")
    prep_state: ArtifactPrepState = Field(default="prep_complete", description="Preparation state for this chunk")
    index_state: ArtifactIndexState = Field(default="index_pending", description="Indexing state for this chunk")
    chunk_cache_key: str = Field(default="", description="Content-addressable preparation cache key")
    created_at: float = Field(..., description="Source or derived artifact creation timestamp")
    expires_at: Optional[float] = Field(default=None, description="Expiration timestamp inherited from artifact TTL")


class ArtifactPrepResult(BaseModel):
    """Result returned by a preparation strategy executor."""

    plan: ArtifactPrepPlan = Field(..., description="Plan that produced this result")
    prep_state: ArtifactPrepState = Field(..., description="Preparation result state")
    chunks: list[PreparedArtifactChunk] = Field(default_factory=list, description="Prepared chunks")
    derived_artifact_ids: list[str] = Field(default_factory=list, description="Derived artifacts used or produced")
    enrichment_artifact_ids: list[str] = Field(default_factory=list, description="Enrichment artifacts produced")
    diagnostics: list[str] = Field(default_factory=list, description="Structured diagnostics for operators")
    chunk_cache_key: str = Field(default="", description="Cache key shared by produced chunks")


class DerivedSetStatus(BaseModel):
    """Status for one prepared/indexed derived set in the artifacts API."""

    strategy_id: str = Field(..., description="Preparation strategy ID")
    strategy_version: str = Field(..., description="Preparation strategy version")
    derived_artifact_ids: list[str] = Field(default_factory=list, description="Derived artifacts in this set")
    chunks_indexed: int = Field(default=0, ge=0, description="Indexed chunks for this strategy")
    prep_state: ArtifactPrepState = Field(default="prep_pending", description="Preparation state")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Preparation confidence")

