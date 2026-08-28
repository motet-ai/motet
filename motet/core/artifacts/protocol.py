"""
Motet - Artifact Store Protocol

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Defines the protocol for Artifact Stores, which manage large, raw, or binary
    payloads (like tool outputs) outside of the hot conversational memory path.

    See and for architectural details.

Dependencies:
    - typing: Protocol definitions
      -.types: ArtifactKind, ArtifactMetadata

Usage:
    class MyStore(ArtifactStoreProtocol):
        def put(self, payload: Any, kind: str, ...) -> str: ...
        def get(self, artifact_id: str) -> Optional[Any]: ...
"""

from typing import Any, Dict, Optional, Protocol, Union, List
from .types import ArtifactKind, ArtifactMetadata


class ArtifactStoreProtocol(Protocol):
    """
    Protocol for storing and retrieving large/raw artifacts.
    """
    
    def put(
        self,
        payload: Union[Dict[str, Any], str, bytes],
        content_type: str = "application/json",
        metadata: Optional[Dict[str, Any]] = None,
        ttl_seconds: Optional[int] = None,
        # Classification (ADR-0062)
        kind: Union[ArtifactKind, str] = ArtifactKind.UNKNOWN,
        source_artifact_id: Optional[str] = None,
        # Isolation fields
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> str:
        """
        Store an artifact and return its ID.
        """
        ...

    def get(
        self,
        artifact_id: str,
        # Context for access control and isolation (ADR-0027, ADR-0058)
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> Optional[Any]:
        """
        Retrieve an artifact by ID.
        Returns the parsed payload (Dict) or raw data (str/bytes) depending on content_type.
        """
        ...

    def get_range(
        self,
        artifact_id: str,
        start: int,
        end: int,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> Optional[bytes]:
        """
        Return payload bytes for the inclusive range [start, end] (ADR-0118).

        Implementations may use native ranged reads where available; encrypted
        envelope stores may fetch and slice the decrypted payload.
        """
        ...
    
    def get_metadata(
        self,
        artifact_id: str,
        # Context for access control and isolation
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> Optional[ArtifactMetadata]:
        """
        Retrieve just the metadata for an artifact.
        """
        ...

    def update_metadata(
        self,
        artifact_id: str,
        metadata_patch: Dict[str, Any],
        # Context for access control and isolation
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> Optional[ArtifactMetadata]:
        """Merge metadata_patch into artifact metadata and return updated metadata."""
        ...

    def list(
        self,
        # Filters
        kind: Optional[Union[ArtifactKind, str]] = None,
        conversation_id: Optional[str] = None,
        source_artifact_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        # Context for access control and isolation
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> List[ArtifactMetadata]:
        """
        List artifacts matching criteria (metadata only).
        
        Args:
            source_artifact_id: Filter to find derived artifacts for a given source.
        """
        ...

    def delete(
        self,
        artifact_id: str,
        # Context for access control and isolation (ADR-0027, ADR-0058)
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> bool:
        """Delete an artifact."""
        ...
