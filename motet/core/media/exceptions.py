"""
Motet - Upload and Derivation Exceptions

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Structured exception hierarchy for upload processing and artifact derivation.
    Provides clear error types for different failure modes to enable proper error
    handling and user-friendly error messages.

Dependencies:
    - None (pure exception definitions)

Usage:
    from motet.core.media.exceptions import (
        DerivationError,
        UnsupportedContentTypeError,
        SourceArtifactNotFoundError,
    )
    
    # Raise specific error types
    if not artifact:
        raise SourceArtifactNotFoundError(f"Artifact {artifact_id} not found")
    
    if content_type not in SUPPORTED_TYPES:
        raise UnsupportedContentTypeError(f"Cannot process {content_type}")

Notes:
    - All derivation errors inherit from DerivationError for easy catch-all handling
    - Specific error types enable targeted error handling and user messaging
    - Error messages should be user-friendly and actionable
"""


class DerivationError(Exception):
    """
    Base exception for all artifact derivation errors.
    
    Use this for catch-all error handling or when no more specific
    error type applies.
    """
    pass


class UnsupportedContentTypeError(DerivationError):
    """
    Content type is not supported for the requested derivation.
    
    Examples:
    - Attempting text extraction from an unsupported binary format
    - Attempting image derivation from a non-image artifact
    """
    pass


class SourceArtifactNotFoundError(DerivationError):
    """
    Source artifact does not exist or is not accessible.
    
    This can occur due to:
    - Artifact ID doesn't exist
    - Access control denied (wrong tenant/principal/motet)
    - Artifact expired (TTL elapsed)
    """
    pass


class ArtifactPayloadMissingError(DerivationError):
    """
    Source artifact metadata exists but payload is missing or corrupted.
    
    This indicates a data integrity issue that should be investigated.
    """
    pass


class ImageProcessingError(DerivationError):
    """
    Image resize, conversion, or processing failed.
    
    Examples:
    - Corrupted image file
    - Unsupported image format
    - PIL/Pillow processing error
    """
    pass


class TextExtractionError(DerivationError):
    """
    Text extraction from document failed.
    
    Examples:
    - Corrupted PDF/DOCX file
    - Password-protected document
    - Unsupported document format variant
    - Extraction library error
    """
    pass


class OCRError(DerivationError):
    """
    OCR processing failed.
    
    Examples:
    - Vision model API error
    - Image quality too low for OCR
    - Model returned invalid response
    """
    pass


class DerivationTimeoutError(DerivationError):
    """
    Derivation processing exceeded timeout threshold.
    
    This typically indicates:
    - Document is too large
    - Processing is too slow
    - Worker is overloaded
    """
    pass

