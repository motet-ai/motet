"""
Motet - Upload Types

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Type definitions for user uploads and attachments in conversation memory.
    Implements "Two-Tier Upload Persistence".

Dependencies:
    - pydantic: Data validation

Usage:
    from motet.core.media.types import UploadAttachment
"""

from typing import Optional, Dict, List
from pydantic import BaseModel, Field


class UploadAttachment(BaseModel):
    """
    Reference to an uploaded artifact, stored in conversation memory turns.
    """
    artifact_id: str = Field(description="ID of the raw upload artifact")
    filename: str = Field(description="Original filename")
    content_type: str = Field(description="MIME type")
    bytes: int = Field(description="Size in bytes")
    
    # Derivation tracking
    derived_artifact_ids: Dict[str, str] = Field(
        default_factory=dict,
        description="Map of derived artifact kinds to their IDs (e.g. {'extracted_text': 'id...'})"
    )
    
    status: str = Field(
        default="ready",
        description="Processing status: pending|processing|ready|error|expired"
    )
    
    error: Optional[str] = None


