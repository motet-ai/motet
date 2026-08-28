"""
Motet - Media Package

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Description:
    Core logic for media file processing (user uploads, tool artifacts), attachments,
    and artifact derivation. Includes text extraction, image processing, derivation
    policies, utilities, and exceptions.
"""

from .types import UploadAttachment
from .exceptions import (
    DerivationError,
    UnsupportedContentTypeError,
    SourceArtifactNotFoundError,
    ArtifactPayloadMissingError,
    ImageProcessingError,
    TextExtractionError,
    OCRError,
    DerivationTimeoutError,
)
from .utils import normalize_to_bytes, extract_artifact_id_from_result
from .derivation_policy import (
    get_eligible_derivations,
    is_text_derivation_eligible,
    select_image_derivation,
    get_derivation_kind,
    DERIVATION_KIND_MAP,
)

__all__ = [
    # Types
    "UploadAttachment",
    # Exceptions
    "DerivationError",
    "UnsupportedContentTypeError",
    "SourceArtifactNotFoundError",
    "ArtifactPayloadMissingError",
    "ImageProcessingError",
    "TextExtractionError",
    "OCRError",
    "DerivationTimeoutError",
    # Utils
    "normalize_to_bytes",
    "extract_artifact_id_from_result",
    # Derivation policy
    "get_eligible_derivations",
    "is_text_derivation_eligible",
    "select_image_derivation",
    "get_derivation_kind",
    "DERIVATION_KIND_MAP",
]


