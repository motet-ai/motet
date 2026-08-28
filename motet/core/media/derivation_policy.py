"""
Motet - Derivation Policy and Selection Logic

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Policy logic for artifact derivation including:
    - Derivation eligibility determination (which derivations to trigger)
    - Image derivation selection (base/detail/thumb based on task context)
    - ArtifactKind mappings for derivation types
    
    Provides generic, keyword-based selection without requiring LLM analysis.

Dependencies:
    - motet.core.artifacts.types: ArtifactKind enum

Usage:
    from motet.core.media.derivation_policy import (
        get_eligible_derivations,
        select_image_derivation,
        get_derivation_kind,
    )
    
    # Determine which derivations to trigger
    eligible = get_eligible_derivations("application/pdf", "user_upload")
    # Returns: ["text"]
    
    # Select image derivation based on query
    derivation_name = select_image_derivation(user_message="What does this text say?")
    # Returns: "detail" for text extraction tasks
    
    # Map derivation name to ArtifactKind
    kind = get_derivation_kind("base")
    # Returns: ArtifactKind.DERIVED_IMAGE_BASE
"""

from typing import Optional, List
from ..artifacts.types import ArtifactKind


# Keywords that suggest text extraction or OCR tasks
TEXT_EXTRACTION_KEYWORDS = [
    "read", "extract", "text", "ocr", "what does this say",
    "what is written", "transcribe", "copy", "quote",
    "invoice", "receipt", "document", "table", "spreadsheet",
    "code", "error", "log", "screenshot", "ui", "interface",
    "label", "caption", "heading", "paragraph", "sentence",
]


def should_escalate_to_detail(message: Optional[str] = None, task_hints: Optional[List[str]] = None) -> bool:
    """
    Determine if task requires high-resolution detail derivation.
    
    Args:
        message: User message text (optional)
        task_hints: List of task hint strings (optional)
        
    Returns:
        True if task likely requires detail derivation (text extraction, OCR, etc.)
    """
    if not message and not task_hints:
        return False
    
    # Combine message and hints for analysis
    text_to_analyze = ""
    if message:
        text_to_analyze += message.lower() + " "
    if task_hints:
        text_to_analyze += " ".join(task_hints).lower() + " "
    
    # Check for text extraction keywords
    for keyword in TEXT_EXTRACTION_KEYWORDS:
        if keyword in text_to_analyze:
            return True
    
    return False


def select_image_derivation(
    message: Optional[str] = None,
    task_hints: Optional[List[str]] = None,
    default: str = "base",
) -> str:
    """
    Select which image derivation to use based on task context.
    
    Args:
        message: User message text (optional, for intent detection)
        task_hints: List of task hint strings (optional)
        default: Default derivation name if no escalation needed (default: "base")
        
    Returns:
        Derivation name: "thumb", "base", or "detail"
        
    Notes:
        - Default: "base" (good balance of quality and cost)
        - Escalates to "detail" for text extraction tasks
        - "thumb" is typically only used for UI previews, not LLM input
    """
    # Thumb is never selected for LLM input (too low quality)
    if default == "thumb":
        default = "base"
    
    # Check if task requires detail
    if should_escalate_to_detail(message, task_hints):
        return "detail"
    
    return default


def get_default_derivation_name() -> str:
    """
    Get the default derivation name for image processing.
    
    Returns:
        "base" - the default derivation for LLM vision input
    """
    return "base"


# ========================================
# Derivation Eligibility Strategy
# ========================================

def get_eligible_derivations(
    content_type: str,
    kind: Optional[str] = None,
    include_text_for_json: bool = True,
) -> List[str]:
    """
    Determine which derivation types are eligible for a given artifact.
    
    This centralizes the content-type-based derivation eligibility logic
    so it can be reused across upload handlers, CLI tools, and background jobs.
    
    Args:
        content_type: MIME type of the artifact
        kind: Artifact kind (optional, may influence eligibility in future)
        include_text_for_json: If True, treat application/json as text-derivation-eligible
        
    Returns:
        List of eligible derivation types such as ["text"], ["image"], ["embedded_images"], or combinations.
        
    Examples:
        >>> get_eligible_derivations("application/pdf")
        ["text"]
        
        >>> get_eligible_derivations("image/png")
        ["image"]
        
        >>> get_eligible_derivations("application/octet-stream")
        []
    """
    eligible = []
    
    # Text derivation eligibility
    if is_text_derivation_eligible(content_type, include_text_for_json):
        eligible.append("text")
    
    # Image derivation eligibility
    if content_type.startswith("image/"):
        eligible.append("image")

    # Embedded media extraction for OOXML office documents.
    if is_embedded_image_derivation_eligible(content_type):
        eligible.append("embedded_images")

    # Video derivation (ADR-0118): poster, keyframes, optional transcript.
    if content_type.startswith("video/"):
        eligible.append("video")
    
    return eligible


def is_embedded_image_derivation_eligible(content_type: str) -> bool:
    """
    Determine if a document can contain embedded images worth extracting as derived artifacts.

    Args:
        content_type: MIME type to check

    Returns:
        True for supported OOXML office documents.
    """

    return content_type in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }


def is_text_derivation_eligible(
    content_type: str,
    include_json: bool = True,
) -> bool:
    """
    Determine if artifact is eligible for text extraction derivation.
    
    Centralized logic for text derivation eligibility used across:
    - create_artifact command (decides whether to dispatch derive_upload_text)
    - Derivation service (validates derivation requests)
    - Future CLI/batch processing tools
    
    Args:
        content_type: MIME type to check
        include_json: If True, treat application/json as eligible
        
    Returns:
        True if text extraction should be attempted
        
    Examples:
        >>> is_text_derivation_eligible("application/pdf")
        True
        
        >>> is_text_derivation_eligible("image/png")
        False
        
        >>> is_text_derivation_eligible("application/json", include_json=True)
        True
    """
    return (
        content_type == "application/pdf"
        or "wordprocessing" in content_type
        or "presentationml" in content_type
        or "opendocument.text" in content_type
        or content_type in {"application/rtf", "text/rtf"}
        or "spreadsheet" in content_type
        or content_type.startswith("text/")
        or (include_json and content_type == "application/json")
    )


# ========================================
# ArtifactKind Mappings (Centralized)
# ========================================

# Map derivation names to ArtifactKind enum values
# Used across derivation commands and prepare_context
DERIVATION_KIND_MAP = {
    "thumb": ArtifactKind.DERIVED_IMAGE_THUMB,
    "base": ArtifactKind.DERIVED_IMAGE_BASE,
    "detail": ArtifactKind.DERIVED_IMAGE_DETAIL,
}


def get_derivation_kind(derivation_name: str) -> Optional[ArtifactKind]:
    """
    Map derivation name to ArtifactKind enum.
    
    Centralizes the mapping so it's consistent across:
    - derivation_service.py (when storing derived artifacts)
    - orchestration.py prepare_context (when looking up derived artifacts)
    - Any future derivation-related logic
    
    Args:
        derivation_name: Derivation identifier ("thumb", "base", "detail")
        
    Returns:
        Corresponding ArtifactKind or None if not a valid derivation name
        
    Examples:
        >>> get_derivation_kind("base")
        <ArtifactKind.DERIVED_IMAGE_BASE: 'derived_image_base'>
        
        >>> get_derivation_kind("unknown")
        None
    """
    return DERIVATION_KIND_MAP.get(derivation_name)
