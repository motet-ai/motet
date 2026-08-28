"""
Motet - Artifact Types

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Type definitions for the Artifact Store, including kinds and metadata models.

Dependencies:
    - pydantic: Data validation
    - enum: Enumeration types

Usage:
    from motet.core.artifacts.types import ArtifactKind, ArtifactMetadata
"""

from enum import Enum
from typing import Any, Dict, Optional, List
from pydantic import BaseModel, Field


class ArtifactKind(str, Enum):
    """
    Discriminator for artifact types.
    """
    USER_UPLOAD = "user_upload"
    TOOL_ARTIFACT = "tool_artifact"
    # ADR-0061: full tool-call arguments offloaded when over the inline memory cap
    TOOL_ARGUMENTS = "tool_arguments"
    # ADR-0113: image produced by a model (image generation), distinct from user uploads/derivations
    GENERATED_IMAGE = "generated_image"
    DERIVED_TEXT = "derived_text"
    DERIVED_OCR = "derived_ocr"
    DERIVED_PAGE_IMAGE = "derived_page_image"
    DERIVED_EMBEDDED_IMAGE = "derived_embedded_image"
    DERIVED_IMAGE_THUMB = "derived_image_thumb"
    DERIVED_IMAGE_BASE = "derived_image_base"
    DERIVED_IMAGE_DETAIL = "derived_image_detail"
    DERIVED_IMAGE_ROI = "derived_image_roi"
    # ADR-0118: video derivation kinds (poster/keyframes as JPEG, transcript as text)
    DERIVED_VIDEO_POSTER = "derived_video_poster"
    DERIVED_VIDEO_KEYFRAME = "derived_video_keyframe"
    DERIVED_VIDEO_TRANSCRIPT = "derived_video_transcript"
    UNKNOWN = "unknown"


class ArtifactMetadata(BaseModel):
    """
    Metadata for stored artifacts.
    """
    id: str = Field(description="Unique artifact ID")
    kind: ArtifactKind = Field(default=ArtifactKind.UNKNOWN, description="Type of artifact")
    content_type: str = Field(description="MIME type of the payload")
    payload_format: str = Field(
        default="envelope",
        description=(
            "Payload storage format: 'envelope' = encrypted JSON wrapper; "
            "'raw' = range-addressable raw object (S3 SSE for encryption at rest)."
        ),
    )
    bytes: int = Field(description="Size of payload in bytes")
    checksum_sha256: str = Field(description="SHA256 checksum of payload")
    created_at: float = Field(description="Unix timestamp of creation")
    expires_at: Optional[float] = Field(default=None, description="Unix timestamp of expiration (if TTL set)")
    
    # Source linkage for derived artifacts
    source_artifact_id: Optional[str] = Field(default=None, description="ID of source artifact if derived")
    
    # Context/Scoping
    tenant_id: Optional[str] = None
    principal_id: Optional[str] = None
    motet_id: Optional[str] = None
    
    # Additional metadata
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Custom metadata bag")


