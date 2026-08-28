"""
Motet - Scoped Artifact Store

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Wrapper for ArtifactStore that pre-binds isolation context (tenant_id,
    principal_id, motet_id) to eliminate repetitive parameter passing.
    
    Used primarily via MotetContext.artifact_store property to provide
    a convenient, scoped artifact store for distributed commands.

Dependencies:
    - motet.core.artifacts.protocol: ArtifactStoreProtocol
    - motet.core.artifacts.types: ArtifactKind, ArtifactMetadata

Usage:
    # Via MotetContext (recommended):
    from motet.core.commands.decorator import get_motet_context
    
    motet = get_motet_context()
    meta = motet.artifact_store.get_metadata(artifact_id)  # No isolation params!
    
    # Direct instantiation (less common):
    from motet.core.artifacts.scoped_store import ScopedArtifactStore
    from motet.core.artifacts import get_artifact_store
    
    scoped = ScopedArtifactStore(
        store=get_artifact_store(),
        tenant_id="tenant-123",
        principal_id="user-456",
        motet_id="default"
    )
    meta = scoped.get_metadata(artifact_id)

Notes:
    - All isolation parameters (tenant_id, principal_id, motet_id) are pre-bound
    - Methods delegate to underlying store with isolation context automatically applied
    - Includes convenience methods like find_derived for common query patterns
"""

from typing import Any, Dict, List, Optional, Union
from .protocol import ArtifactStoreProtocol
from .types import ArtifactKind, ArtifactMetadata


class ScopedArtifactStore:
    """
    Artifact store wrapper with pre-bound isolation context.
    
    This eliminates the need to pass tenant_id/principal_id/motet_id on every call,
    making command code cleaner and reducing the risk of forgetting isolation parameters.
    
    All methods delegate to the underlying store with the pre-bound context.
    """
    
    def __init__(
        self,
        store: ArtifactStoreProtocol,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ):
        """
        Initialize scoped artifact store.
        
        Args:
            store: Underlying artifact store implementation
            tenant_id: Tenant ID for isolation
            principal_id: Principal ID for isolation
            motet_id: Motet ID for isolation
        """
        self._store = store
        self._tenant_id = tenant_id
        self._principal_id = principal_id
        self._motet_id = motet_id
    
    def put(
        self,
        payload: Union[Dict[str, Any], str, bytes],
        content_type: str = "application/json",
        metadata: Optional[Dict[str, Any]] = None,
        ttl_seconds: Optional[int] = None,
        kind: Union[ArtifactKind, str] = ArtifactKind.UNKNOWN,
        source_artifact_id: Optional[str] = None,
    ) -> str:
        """
        Store artifact payload with pre-bound isolation context.
        
        Args:
            payload: Artifact data (bytes, str, or dict)
            content_type: MIME type
            metadata: Additional metadata
            ttl_seconds: Time-to-live in seconds
            kind: Artifact kind
            source_artifact_id: Source artifact ID for derived artifacts
            
        Returns:
            Artifact ID
        """
        return self._store.put(
            payload=payload,
            content_type=content_type,
            metadata=metadata,
            ttl_seconds=ttl_seconds,
            kind=kind,
            source_artifact_id=source_artifact_id,
            tenant_id=self._tenant_id,
            principal_id=self._principal_id,
            motet_id=self._motet_id,
        )
    
    def get(self, artifact_id: str) -> Optional[Any]:
        """
        Retrieve artifact payload with pre-bound isolation context.
        
        Args:
            artifact_id: Artifact identifier
            
        Returns:
            Artifact payload or None if not found/not accessible
        """
        return self._store.get(
            artifact_id=artifact_id,
            tenant_id=self._tenant_id,
            principal_id=self._principal_id,
            motet_id=self._motet_id,
        )

    def get_range(self, artifact_id: str, start: int, end: int) -> Optional[bytes]:
        """Retrieve an inclusive byte range from the artifact payload (ADR-0118)."""

        return self._store.get_range(
            artifact_id=artifact_id,
            start=start,
            end=end,
            tenant_id=self._tenant_id,
            principal_id=self._principal_id,
            motet_id=self._motet_id,
        )
    
    def get_metadata(self, artifact_id: str) -> Optional[ArtifactMetadata]:
        """
        Retrieve artifact metadata with pre-bound isolation context.
        
        Args:
            artifact_id: Artifact identifier
            
        Returns:
            Artifact metadata or None if not found/not accessible
        """
        return self._store.get_metadata(
            artifact_id=artifact_id,
            tenant_id=self._tenant_id,
            principal_id=self._principal_id,
            motet_id=self._motet_id,
        )

    def update_metadata(
        self,
        artifact_id: str,
        metadata_patch: Dict[str, Any],
    ) -> Optional[ArtifactMetadata]:
        """
        Merge a metadata patch into an artifact with pre-bound isolation context.

        Args:
            artifact_id: Artifact identifier
            metadata_patch: Metadata fields to merge into the artifact wrapper

        Returns:
            Updated artifact metadata, or None if not found/not accessible
        """
        return self._store.update_metadata(
            artifact_id=artifact_id,
            metadata_patch=metadata_patch,
            tenant_id=self._tenant_id,
            principal_id=self._principal_id,
            motet_id=self._motet_id,
        )

    def list(
        self,
        kind: Optional[Union[ArtifactKind, str]] = None,
        conversation_id: Optional[str] = None,
        source_artifact_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ArtifactMetadata]:
        """
        List artifacts with pre-bound isolation context.
        
        Args:
            kind: Filter by artifact kind
            conversation_id: Filter by conversation ID
            source_artifact_id: Filter by source artifact ID (for derived artifacts)
            limit: Maximum number of results
            offset: Pagination offset
            
        Returns:
            List of artifact metadata
        """
        return self._store.list(
            kind=kind,
            conversation_id=conversation_id,
            source_artifact_id=source_artifact_id,
            limit=limit,
            offset=offset,
            tenant_id=self._tenant_id,
            principal_id=self._principal_id,
            motet_id=self._motet_id,
        )
    
    def delete(self, artifact_id: str) -> bool:
        """
        Delete artifact with pre-bound isolation context.
        
        Args:
            artifact_id: Artifact identifier
            
        Returns:
            True if deleted, False if not found/not accessible
        """
        return self._store.delete(
            artifact_id=artifact_id,
            tenant_id=self._tenant_id,
            principal_id=self._principal_id,
            motet_id=self._motet_id,
        )
    
    def find_derived(
        self,
        source_artifact_id: str,
        kind: ArtifactKind,
    ) -> Optional[ArtifactMetadata]:
        """
        Find existing derived artifact of specified kind.
        
        This is a convenience method for the common pattern of checking
        if a derivation already exists before generating it.
        
        Args:
            source_artifact_id: Source artifact ID
            kind: Derived artifact kind to search for
            
        Returns:
            First matching derived artifact or None if not found
            
        Examples:
            # Check if base image derivation exists
            existing = motet.artifact_store.find_derived(
                source_id,
                ArtifactKind.DERIVED_IMAGE_BASE
            )
            if existing:
                return existing.id  # Reuse existing
            else:
                # Generate new derivation
                ...
        """
        results = self.list(
            kind=kind,
            source_artifact_id=source_artifact_id,
            limit=1,
        )
        return results[0] if results else None
    
    @property
    def tenant_id(self) -> Optional[str]:
        """Get the bound tenant ID."""
        return self._tenant_id
    
    @property
    def principal_id(self) -> Optional[str]:
        """Get the bound principal ID."""
        return self._principal_id
    
    @property
    def motet_id(self) -> Optional[str]:
        """Get the bound motet ID."""
        return self._motet_id

