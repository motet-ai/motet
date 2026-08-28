"""
Motet - Derivation Service

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Service logic for deriving artifacts (text extraction, image resizing) from source uploads.
    Orchestrates storage retrieval, extraction/processing, and storage of derived artifacts.

Dependencies:
    - motet.core.artifacts: Storage
    - .extraction: Text extraction logic
    - .image_processing: Image processing logic

Usage:
    result = derive_text_artifact("source-id", "tenant-id")
    result = derive_image_artifacts("source-id", "tenant-id", derivation_names=["thumb", "base"])
"""

import structlog
from typing import Dict, Any, Optional, List

from ..artifacts import get_artifact_store, ArtifactKind
from .text_extraction import extract_text_from_bytes
from .image_processing import (
    is_image_content_type,
    generate_image_derivations,
)

logger = structlog.get_logger(__name__)

class DerivationError(Exception):
    pass

def derive_text_artifact(
    source_artifact_id: str,
    tenant_id: str,
    principal_id: Optional[str] = None,
    motet_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Derive text from a source artifact and store as a new artifact.
    Returns the new artifact metadata.
    
    Args:
        source_artifact_id: ID of source artifact
        tenant_id: Tenant ID for access control
        principal_id: Principal ID for access control (optional)
        motet_id: Motet ID for access control (optional)
    """
    store = get_artifact_store()
    
    # 1. Fetch source
    source_meta = store.get_metadata(
        source_artifact_id, 
        tenant_id=tenant_id, 
        principal_id=principal_id, 
        motet_id=motet_id
    )
    if not source_meta:
        raise DerivationError(f"Source artifact {source_artifact_id} not found")
        
    source_bytes = store.get(
        source_artifact_id, 
        tenant_id=tenant_id, 
        principal_id=principal_id, 
        motet_id=motet_id
    )
    if not source_bytes:
        raise DerivationError("Source artifact payload missing")
        
    # Handle dict payloads (json) by dumping to bytes
    if isinstance(source_bytes, dict):
        import json
        source_bytes = json.dumps(source_bytes).encode("utf-8")
    elif isinstance(source_bytes, str):
        source_bytes = source_bytes.encode("utf-8")
        
    # 2. Extract text (utilities-only; PDF OCR is orchestrated by commands in derive_upload_text)
    try:
        extracted_text = extract_text_from_bytes(source_bytes, source_meta.content_type)
    except Exception as e:
        raise DerivationError(f"Extraction failed: {e}")
        
    if not extracted_text:
        logger.info("extraction_yielded_empty_text", artifact_id=source_artifact_id)
        return {
            "status": "skipped", 
            "reason": "empty_text",
            "source_artifact_id": source_artifact_id  # Include for frontend event tracking
        }
        
    # 3. Store derived artifact
    derived_id = store.put(
        payload=extracted_text,
        content_type="text/plain",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id=source_artifact_id,
        metadata={
            "source_filename": source_meta.metadata.get("filename"),
            "derivation_method": "extraction_v1",
        },
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
        ttl_seconds=(
            int(source_meta.expires_at - source_meta.created_at)
            if source_meta.expires_at
            else None
        ),
    )
    
    # Note: Updating the 'derived_artifact_ids' map in the original upload *reference* (in Memory)
    # is the caller's responsibility (or done via event bus). 
    # Since MemoryItem is immutable-ish in many flows, usually we just query derived artifacts 
    # by `source_artifact_id` or the UI/Orchestrator resolves them at runtime.
    
    return {
        "status": "success",
        "id": derived_id,
        "kind": ArtifactKind.DERIVED_TEXT,
        "bytes": len(extracted_text)
    }


def derive_image_artifacts(
    source_artifact_id: str,
    tenant_id: str,
    principal_id: Optional[str] = None,
    motet_id: Optional[str] = None,
    derivation_names: Optional[List[str]] = None,
    force_regenerate: bool = False,
) -> Dict[str, Any]:
    """
    Derive image artifacts (thumb, base, detail) from a source image artifact.
    Returns metadata for all generated derivations.
    
    Args:
        source_artifact_id: ID of source image artifact
        tenant_id: Tenant ID for access control
        principal_id: Principal ID for access control (optional)
        motet_id: Motet ID for access control (optional)
        derivation_names: List of derivation names to generate (default: ["thumb", "base"])
        force_regenerate: If True, regenerate even if derivation already exists
        
    Returns:
        Dict with status and derived artifact IDs:
        {
            "status": "success",
            "derivations": {
                "thumb": {"id": "...", "bytes": int, ...},
                "base": {"id": "...", "bytes": int, ...},
                "detail": {"id": "...", "bytes": int, ...}  # if generated
            }
        }
        
    Raises:
        DerivationError: For expected derivation failures (unsupported type, processing errors)
    """
    store = get_artifact_store()
    
    # Default derivation names (eager generation)
    if derivation_names is None:
        derivation_names = ["thumb", "base"]
    
    # 1. Fetch source
    source_meta = store.get_metadata(
        source_artifact_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
    )
    if not source_meta:
        raise DerivationError(f"Source artifact {source_artifact_id} not found")
    
    # Verify it's an image
    if not is_image_content_type(source_meta.content_type):
        raise DerivationError(f"Source artifact is not an image: {source_meta.content_type}")
    
    source_bytes = store.get(
        source_artifact_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
    )
    if not source_bytes:
        raise DerivationError("Source artifact payload missing")
    
    # Handle dict/str payloads (shouldn't happen for images, but be safe)
    if isinstance(source_bytes, dict):
        import json
        source_bytes = json.dumps(source_bytes).encode("utf-8")
    elif isinstance(source_bytes, str):
        source_bytes = source_bytes.encode("utf-8")
    
    # 2. Check if derivations already exist (unless force_regenerate)
    existing_derivations = {}
    if not force_regenerate:
        for name in derivation_names:
            # Map derivation name to ArtifactKind
            kind_map = {
                "thumb": ArtifactKind.DERIVED_IMAGE_THUMB,
                "base": ArtifactKind.DERIVED_IMAGE_BASE,
                "detail": ArtifactKind.DERIVED_IMAGE_DETAIL,
            }
            kind = kind_map.get(name)
            if not kind:
                continue
            
            # Check if derivation already exists
            existing = store.list(
                kind=kind,
                source_artifact_id=source_artifact_id,
                limit=1,
                tenant_id=tenant_id,
                principal_id=principal_id,
                motet_id=motet_id,
            )
            if existing:
                existing_derivations[name] = existing[0]
                logger.debug(
                    "derivation_already_exists",
                    source_artifact_id=source_artifact_id,
                    derivation_name=name,
                    existing_id=existing[0].id,
                )
    
    # 3. Generate missing derivations
    generate_thumb = "thumb" in derivation_names and "thumb" not in existing_derivations
    generate_base = "base" in derivation_names and "base" not in existing_derivations
    generate_detail = "detail" in derivation_names and "detail" not in existing_derivations
    
    derivations_result = {}
    
    if generate_thumb or generate_base or generate_detail:
        try:
            image_derivations = generate_image_derivations(
                image_bytes=source_bytes,
                content_type=source_meta.content_type,
                generate_thumb=generate_thumb,
                generate_base=generate_base,
                generate_detail=generate_detail,
            )
        except Exception as e:
            raise DerivationError(f"Image processing failed: {e}")
        
        # 4. Store each generated derivation
        for name, (derived_bytes, metadata) in image_derivations.items():
            # Map derivation name to ArtifactKind
            kind_map = {
                "thumb": ArtifactKind.DERIVED_IMAGE_THUMB,
                "base": ArtifactKind.DERIVED_IMAGE_BASE,
                "detail": ArtifactKind.DERIVED_IMAGE_DETAIL,
            }
            kind = kind_map.get(name)
            if not kind:
                logger.warning("unknown_derivation_name", name=name)
                continue
            
            # Determine content type (preserve original if possible)
            content_type = source_meta.content_type
            if metadata.get("format") == "PNG":
                content_type = "image/png"
            elif metadata.get("format") == "WEBP":
                content_type = "image/webp"
            else:
                content_type = "image/jpeg"  # Default fallback
            
            derived_id = store.put(
                payload=derived_bytes,
                content_type=content_type,
                kind=kind,
                source_artifact_id=source_artifact_id,
                metadata={
                    "source_filename": source_meta.metadata.get("filename"),
                    "derivation_name": name,
                    "derivation_method": "resize_v1",
                    "max_side": metadata.get("max_side"),
                    "width": metadata.get("width"),
                    "height": metadata.get("height"),
                    "quality": metadata.get("quality"),
                    "original_width": metadata.get("original_width"),
                    "original_height": metadata.get("original_height"),
                },
                tenant_id=tenant_id,
                principal_id=principal_id,
                motet_id=motet_id,
                ttl_seconds=(
                    int(source_meta.expires_at - source_meta.created_at)
                    if source_meta.expires_at
                    else None
                ),
            )
            
            derivations_result[name] = {
                "id": derived_id,
                "kind": kind,
                "bytes": len(derived_bytes),
                "width": metadata.get("width"),
                "height": metadata.get("height"),
            }
            
            logger.info(
                "image_derivation_created",
                source_artifact_id=source_artifact_id,
                derivation_name=name,
                derived_id=derived_id,
                bytes=len(derived_bytes),
            )
    
    # 5. Include existing derivations in result
    for name, existing_meta in existing_derivations.items():
        derivations_result[name] = {
            "id": existing_meta.id,
            "kind": existing_meta.kind,
            "bytes": existing_meta.bytes,
            "width": existing_meta.metadata.get("width"),
            "height": existing_meta.metadata.get("height"),
        }
    
    return {
        "status": "success",
        "derivations": derivations_result,
    }


