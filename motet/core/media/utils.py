"""
Motet - Upload Utilities

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Common utility functions for upload processing and artifact handling.
    Provides payload normalization, type checking, and other shared helpers
    used across derivation commands and services.

Dependencies:
    - json: JSON serialization
    - typing: Type hints

Usage:
    from motet.core.media.utils import normalize_to_bytes
    
    # Ensure payload is bytes regardless of input type
    payload_bytes = normalize_to_bytes(artifact_payload)

Notes:
    - normalize_to_bytes handles dict/str/bytes uniformly
    - Useful for artifact processing where payload type may vary
"""

from typing import Union
import json


def normalize_to_bytes(payload: Union[bytes, str, dict]) -> bytes:
    """
    Ensure payload is bytes (convert dict/str if needed).
    
    This is a common pattern in artifact/derivation processing where
    payloads may come from different sources with different types.
    
    Args:
        payload: Input payload (bytes, str, or dict)
        
    Returns:
        Normalized payload as bytes
        
    Raises:
        TypeError: If payload type cannot be converted to bytes
        
    Examples:
        >>> normalize_to_bytes(b"hello")
        b"hello"
        
        >>> normalize_to_bytes("hello")
        b"hello"
        
        >>> normalize_to_bytes({"key": "value"})
        b'{"key": "value"}'
    """
    if isinstance(payload, bytes):
        return payload
    
    if isinstance(payload, dict):
        return json.dumps(payload).encode("utf-8")
    
    if isinstance(payload, str):
        return payload.encode("utf-8")
    
    raise TypeError(
        f"Cannot convert {type(payload).__name__} to bytes. "
        f"Expected bytes, str, or dict."
    )


def extract_artifact_id_from_result(result: dict) -> str:
    """
    Safely extract artifact_id from ADR-0029 command response.
    
    Args:
        result: Command execution result (ADR-0029 format)
        
    Returns:
        Artifact ID string
        
    Raises:
        ValueError: If artifact_id cannot be extracted
        
    Examples:
        >>> result = {
        ...     "status": "success",
        ...     "result": {"data": {"artifact_id": "abc-123"}}
        ... }
        >>> extract_artifact_id_from_result(result)
        'abc-123'
    """
    try:
        artifact_id = result["result"]["data"]["artifact_id"]
        if not artifact_id:
            raise ValueError("artifact_id is empty")
        return artifact_id
    except (KeyError, TypeError) as e:
        raise ValueError(
            f"Cannot extract artifact_id from result. "
            f"Expected ADR-0029 format with result.data.artifact_id. "
            f"Error: {e}"
        ) from e

