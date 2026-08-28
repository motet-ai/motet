"""
Motet - Redis Artifact Store

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Redis-backed implementation of the Artifact Store.
    Uses a dedicated encrypted payload store for encryption-at-rest
    and serves as the default ArtifactStore backend for ToolArtifacts
    and User Uploads.

Dependencies:
    - uuid: ID generation
    - json: Serialization
    - motet.core.distributed.redis_manager: Unified Redis access
    - motet.core.security.encrypted_payload_store: Envelope-encrypted payload storage
    - motet.core.config: Artifact store policy (encryption required, max size, TTL)

Usage:
    store = RedisArtifactStore()
    id = store.put({"data": "big payload"}, ttl_seconds=3600)
    data = store.get(id)
"""

import base64
import hashlib
import json
import time
import uuid
from typing import Any, Dict, List, Optional, Union, cast
import structlog

from .protocol import ArtifactStoreProtocol
from .types import ArtifactKind, ArtifactMetadata
from ..config import Config
from ..distributed.redis_manager import get_sync_redis_client
from ..distributed.tenant_keys import (
    delete_candidate_keys,
    first_existing_key,
    tenant_key,
    zrevrange_ids_with_fallback,
)
from ..security.encrypted_payload_store import IsolationContext, get_sync_encrypted_payload_store

logger = structlog.get_logger(__name__)

class RedisArtifactStore(ArtifactStoreProtocol):
    """
    Redis-backed artifact store (MVP).
    
    Features:
    - JSON serialization for structured data
    - Redis TTL support for retention
    - Encryption-at-rest (ADR-0056) using envelope encryption (no plaintext fallback)
    - Metadata indexing via Redis Sets/Lists (MVP implementation for list())
    """
    
    def __init__(self, service_name: str = "artifact_store"):
        self.service_name = service_name
        self._prefix = "art:"
        self._index_prefix = "idx:art:"
        self._payload_store = get_sync_encrypted_payload_store(service_name)
        self._cfg = Config()

    def _logical_key(self, artifact_id: str) -> str:
        return f"{self._prefix}{artifact_id}"

    def _make_key(self, artifact_id: str, tenant_id: Optional[str] = None) -> str:
        logical = self._logical_key(artifact_id)
        if tenant_id:
            return tenant_key(tenant_id, logical)
        return logical

    def _item_keys(self, artifact_id: str, tenant_id: str) -> tuple[str, ...]:
        return (tenant_key(tenant_id, self._logical_key(artifact_id)),)

    def _index_logical(self, tenant_id: str, suffix: str = "") -> str:
        return f"{self._index_prefix}tenant:{tenant_id}{suffix}"

    def _index_key(self, tenant_id: str, suffix: str = "") -> str:
        return tenant_key(tenant_id, self._index_logical(tenant_id, suffix))

    def _index_keys(self, tenant_id: str, suffix: str = "") -> tuple[str, ...]:
        return (tenant_key(tenant_id, self._index_logical(tenant_id, suffix)),)

    def put(
        self,
        payload: Union[Dict[str, Any], str, bytes],
        content_type: str = "application/json",
        metadata: Optional[Dict[str, Any]] = None,
        ttl_seconds: Optional[int] = None,
        # Classification
        kind: Union[ArtifactKind, str] = ArtifactKind.UNKNOWN,
        source_artifact_id: Optional[str] = None,
        # Isolation
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> str:
        try:
            if not bool(getattr(self._cfg, "artifact_store_encryption", True)):
                raise ValueError("artifact_store_encryption is disabled; refusing plaintext artifact storage")

            if not tenant_id:
                raise ValueError("tenant_id is required for encrypted artifact storage")

            artifact_id = str(uuid.uuid4())
            key = self._make_key(artifact_id, tenant_id)
            
            # Normalize kind
            kind_str = kind.value if isinstance(kind, ArtifactKind) else str(kind)

            # Compute bytes + checksum for integrity and observability (ADR-0061).
            raw_bytes: bytes
            if isinstance(payload, bytes):
                raw_bytes = payload
            elif isinstance(payload, str):
                raw_bytes = payload.encode("utf-8", errors="ignore")
            else:
                # Dict payload: compute stable-ish bytes for checksum.
                raw_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode(
                    "utf-8", errors="ignore"
                )
            payload_bytes = len(raw_bytes)
            max_bytes = self._max_payload_bytes(content_type)
            if payload_bytes > max_bytes:
                raise ValueError(f"artifact payload too large: {payload_bytes} bytes > {max_bytes}")

            checksum_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            created_at = time.time()
            
            effective_ttl = int(ttl_seconds) if ttl_seconds and ttl_seconds > 0 else None
            expires_at = created_at + effective_ttl if effective_ttl is not None else None

            # Encode payload into JSON-safe representation for encrypted envelope.
            wrapper_payload: Any = payload
            payload_is_base64 = False
            if isinstance(payload, bytes):
                wrapper_payload = base64.b64encode(payload).decode("ascii")
                payload_is_base64 = True

            # Wrap payload with metadata (stored inside encrypted envelope)
            wrapper = {
                "id": artifact_id,
                "payload": wrapper_payload,
                "payload_is_base64": payload_is_base64,
                "content_type": content_type,
                "bytes": payload_bytes,
                "checksum_sha256": checksum_sha256,
                "created_at": created_at,
                "expires_at": expires_at,
                "kind": kind_str,
                "source_artifact_id": source_artifact_id,
                "metadata": metadata or {},
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "motet_id": motet_id,
            }
            
            isolation = IsolationContext(
                tenant_id=tenant_id,
                principal_id=principal_id,
                motet_id=motet_id,
            )

            payload_bytes_json = json.dumps(wrapper, ensure_ascii=False, separators=(",", ":"), default=str).encode(
                "utf-8", errors="ignore"
            )

            from ..security.encryption_contexts import EncryptionContext

            # Store payload
            self._payload_store.put_bytes(
                key=key,
                payload=payload_bytes_json,
                isolation=isolation,
                context=EncryptionContext.TOOL_ARTIFACT.value,
                ttl_seconds=effective_ttl,
                plaintext_fields={
                    "id": artifact_id,
                    "content_type": content_type,
                    "bytes": payload_bytes,
                    "checksum_sha256": checksum_sha256,
                    "created_at": created_at,
                    "kind": kind_str,
                    "source_artifact_id": source_artifact_id,
                },
            )
            
            # Update indexes for list()
            # MVP: We use simple Redis sets/lists for indexing. 
            # Note: Redis sets don't expire automatically when the key expires, so this leaks index entries.
            # Real production implementation should use RediSearch or manually clean up indexes.
            # For this implementation, we'll index by tenant_id (primary isolation) + kind.
            
            sync_client = get_sync_redis_client(self.service_name)
            
            # Global tenant index (sorted by time)
            tenant_idx_key = self._index_key(tenant_id)
            sync_client.zadd(tenant_idx_key, {artifact_id: created_at})
            
            # Tenant + Kind index
            if kind_str:
                kind_idx_key = self._index_key(tenant_id, f":kind:{kind_str}")
                sync_client.zadd(kind_idx_key, {artifact_id: created_at})

            # Tenant + Source + Kind index (ADR-0062)
            # Enables correct and efficient lookup of derived artifacts given a source_artifact_id.
            # Without this, list(kind=..., source_artifact_id=..., limit=1) can miss matches because
            # filtering happens after a tenant+kind zrevrange.
            if source_artifact_id and kind_str:
                source_kind_idx_key = self._index_key(
                    tenant_id, f":source:{source_artifact_id}:kind:{kind_str}"
                )
                sync_client.zadd(source_kind_idx_key, {artifact_id: created_at})
                
            return artifact_id
            
        except Exception as e:
            logger.error("artifact_store_put_failed", error=str(e))
            raise

    def get(
        self,
        artifact_id: str,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> Optional[Any]:
        try:
            wrapper = self._get_wrapper(artifact_id, tenant_id, principal_id, motet_id)
            if not wrapper:
                return None
                
            payload_value = wrapper.get("payload")
            if wrapper.get("payload_is_base64") is True and isinstance(payload_value, str):
                return base64.b64decode(payload_value)
            return payload_value
            
        except Exception as e:
            logger.error("artifact_store_get_failed", artifact_id=artifact_id, error=str(e))
            return None

    def get_range(
        self,
        artifact_id: str,
        start: int,
        end: int,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> Optional[bytes]:
        try:
            payload = self.get(
                artifact_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
                motet_id=motet_id,
            )
            if payload is None:
                return None
            from .range_utils import artifact_payload_to_bytes, slice_payload_bytes

            data = artifact_payload_to_bytes(payload)
            if start < 0 or end < start or start >= len(data):
                return b""
            end = min(end, len(data) - 1)
            return slice_payload_bytes(data, start, end)
        except Exception as e:
            logger.error("artifact_store_get_range_failed", artifact_id=artifact_id, error=str(e))
            return None

    def get_metadata(
        self,
        artifact_id: str,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> Optional[ArtifactMetadata]:
        try:
            wrapper = self._get_wrapper(artifact_id, tenant_id, principal_id, motet_id)
            if not wrapper:
                return None
                
            return self._wrapper_to_metadata(wrapper)
        except Exception as e:
            logger.error("artifact_store_get_metadata_failed", artifact_id=artifact_id, error=str(e))
            return None

    def update_metadata(
        self,
        artifact_id: str,
        metadata_patch: Dict[str, Any],
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> Optional[ArtifactMetadata]:
        """Merge metadata_patch into an artifact wrapper without changing payload bytes."""

        try:
            if not isinstance(metadata_patch, dict):
                raise ValueError("metadata_patch must be a dict")
            wrapper = self._get_wrapper(artifact_id, tenant_id, principal_id, motet_id)
            if not wrapper:
                return None
            md = wrapper.get("metadata")
            if not isinstance(md, dict):
                md = {}
            md.update(metadata_patch)
            wrapper["metadata"] = md

            key = self._make_key(artifact_id, str(wrapper.get("tenant_id") or tenant_id or "") or None)
            ttl_seconds: Optional[int] = None
            try:
                sync_client = get_sync_redis_client(self.service_name)
                existing = first_existing_key(
                    sync_client,
                    self._item_keys(artifact_id, str(wrapper.get("tenant_id") or tenant_id or "")),
                )
                ttl_key = existing or key
                ttl = int(cast(Any, sync_client.ttl(ttl_key)))
                if ttl > 0:
                    ttl_seconds = ttl
            except Exception:
                ttl_seconds = None

            isolation = IsolationContext(
                tenant_id=str(wrapper.get("tenant_id") or tenant_id or ""),
                principal_id=wrapper.get("principal_id") or principal_id,
                motet_id=wrapper.get("motet_id") or motet_id,
            )
            from ..security.encryption_contexts import EncryptionContext

            payload_bytes_json = json.dumps(wrapper, ensure_ascii=False, separators=(",", ":"), default=str).encode(
                "utf-8", errors="ignore"
            )
            self._payload_store.put_bytes(
                key=key,
                payload=payload_bytes_json,
                isolation=isolation,
                context=EncryptionContext.TOOL_ARTIFACT.value,
                ttl_seconds=ttl_seconds,
                plaintext_fields={
                    "id": wrapper.get("id"),
                    "content_type": wrapper.get("content_type"),
                    "bytes": wrapper.get("bytes"),
                    "checksum_sha256": wrapper.get("checksum_sha256"),
                    "created_at": wrapper.get("created_at"),
                    "kind": wrapper.get("kind"),
                    "source_artifact_id": wrapper.get("source_artifact_id"),
                },
            )
            return self._wrapper_to_metadata(wrapper)
        except Exception as e:
            logger.error("artifact_store_update_metadata_failed", artifact_id=artifact_id, error=str(e), exc_info=True)
            return None

    def list(
        self,
        kind: Optional[Union[ArtifactKind, str]] = None,
        conversation_id: Optional[str] = None,
        source_artifact_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> List[ArtifactMetadata]:
        try:
            if not tenant_id:
                # Listing requires at least tenant context
                return []
                
            sync_client = get_sync_redis_client(self.service_name)
            
            # Determine which index to query
            kind_str: Optional[str] = None
            if kind:
                kind_str = kind.value if isinstance(kind, ArtifactKind) else str(kind)

            # If a source_artifact_id filter is provided and kind is known, use the dedicated index.
            # This is the correct way to find derived artifacts for a source.
            if source_artifact_id and kind_str:
                idx_keys = self._index_keys(
                    tenant_id, f":source:{source_artifact_id}:kind:{kind_str}"
                )
            elif kind_str:
                idx_keys = self._index_keys(tenant_id, f":kind:{kind_str}")
            else:
                idx_keys = self._index_keys(tenant_id)
                
            # Get IDs (reverse chronological)
            artifact_ids = zrevrange_ids_with_fallback(
                sync_client, idx_keys, start=offset, end=offset + limit - 1
            )
            
            results = []
            for aid in artifact_ids:
                if isinstance(aid, bytes):
                    aid = aid.decode("utf-8")
                    
                # Fetch metadata for each
                # We can optimize this by implementing a multi-get in encrypted_payload_store
                # For now, we loop (N+1 query problem, but batch size is small)
                meta = self.get_metadata(aid, tenant_id, principal_id, motet_id)
                if meta:
                    # Filter by conversation_id if requested (requires opening the envelope/metadata)
                    if conversation_id:
                        conv_id = meta.metadata.get("conversation_id")
                        if conv_id != conversation_id:
                            continue
                    
                    # Filter by source_artifact_id if requested (for finding derived artifacts)
                    if source_artifact_id:
                        if meta.source_artifact_id != source_artifact_id:
                            continue
                    
                    # Filter by principal if provided (though encryption layer does this too)
                    if principal_id and meta.principal_id and meta.principal_id != principal_id:
                        continue
                        
                    results.append(meta)
                    
            return results
            
        except Exception as e:
            logger.error("artifact_store_list_failed", error=str(e))
            return []

    def delete(
        self,
        artifact_id: str,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> bool:
        try:
            # Verify access before deletion using plaintext isolation fields
            sync_client = get_sync_redis_client(self.service_name)
            from ..security.redis_decode_helpers import normalize_redis_str_mapping

            existing = None
            if tenant_id:
                existing = first_existing_key(sync_client, self._item_keys(artifact_id, tenant_id))
            key = existing or self._make_key(artifact_id, tenant_id)
            data = normalize_redis_str_mapping(cast(Any, sync_client.hgetall(key)) or {})
            if not data:
                # Also try to clean up indexes if data is gone
                self._cleanup_indexes(artifact_id, tenant_id)
                return False

            stored_tenant = data.get("tenant_id")
            stored_principal = data.get("principal_id")
            stored_motet = data.get("motet_id")
            stored_kind = data.get("kind")
            
            if tenant_id and (not stored_tenant or tenant_id != stored_tenant):
                logger.warning("artifact_delete_denied_tenant_mismatch", artifact_id=artifact_id)
                return False
            if principal_id and (not stored_principal or principal_id != stored_principal):
                logger.warning("artifact_delete_denied_principal_mismatch", artifact_id=artifact_id)
                return False
            if motet_id and (not stored_motet or motet_id != stored_motet):
                logger.warning("artifact_delete_denied_motet_mismatch", artifact_id=artifact_id)
                return False
            
            # Delete data (both names during cutover)
            if tenant_id:
                deleted = bool(delete_candidate_keys(sync_client, self._item_keys(artifact_id, tenant_id)))
            else:
                deleted = bool(sync_client.delete(key))
            
            # Cleanup indexes
            if deleted and stored_tenant:
                self._cleanup_indexes(artifact_id, stored_tenant, stored_kind)
                
            return deleted
        except Exception as e:
            logger.error("artifact_store_delete_failed", artifact_id=artifact_id, error=str(e))
            return False

    def _max_payload_bytes(self, content_type: str) -> int:
        if str(content_type or "").startswith("video/"):
            return int(getattr(self._cfg, "artifact_max_video_bytes", 536_870_912) or 536_870_912)
        return int(getattr(self._cfg, "artifact_store_max_bytes", 25_000_000) or 25_000_000)

    def _get_wrapper(
        self,
        artifact_id: str,
        tenant_id: Optional[str],
        principal_id: Optional[str],
        motet_id: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        if not bool(getattr(self._cfg, "artifact_store_encryption", True)):
            raise ValueError("artifact_store_encryption is disabled; refusing plaintext artifact reads")

        if not tenant_id:
            raise ValueError("tenant_id is required for encrypted artifact retrieval")

        isolation = IsolationContext(
            tenant_id=tenant_id,
            principal_id=principal_id,
            motet_id=motet_id,
        )

        from ..security.encryption_contexts import EncryptionContext

        raw = None
        for key in self._item_keys(artifact_id, tenant_id):
            raw = self._payload_store.get_bytes(
                key=key,
                isolation=isolation,
                context=EncryptionContext.TOOL_ARTIFACT.value,
            )
            if raw:
                break
        if not raw:
            return None
            
        return json.loads(raw.decode("utf-8", errors="ignore"))

    def _wrapper_to_metadata(self, wrapper: Dict[str, Any]) -> ArtifactMetadata:
        # Convert string kind back to enum if possible
        kind_val = wrapper.get("kind", ArtifactKind.UNKNOWN)
        try:
            kind = ArtifactKind(kind_val)
        except ValueError:
            kind = ArtifactKind.UNKNOWN

        return ArtifactMetadata(
            id=wrapper["id"],
            kind=kind,
            content_type=wrapper.get("content_type", "application/octet-stream"),
            bytes=wrapper.get("bytes", 0),
            checksum_sha256=wrapper.get("checksum_sha256", ""),
            created_at=wrapper.get("created_at", 0.0),
            expires_at=wrapper.get("expires_at"),
            source_artifact_id=wrapper.get("source_artifact_id"),
            tenant_id=wrapper.get("tenant_id"),
            principal_id=wrapper.get("principal_id"),
            motet_id=wrapper.get("motet_id"),
            metadata=wrapper.get("metadata", {}),
        )

    def _cleanup_indexes(self, artifact_id: str, tenant_id: Optional[str], kind: Optional[str] = None):
        if not tenant_id:
            return
            
        sync_client = get_sync_redis_client(self.service_name)
        
        # Remove from tenant index (both names during cutover)
        for idx_key in self._index_keys(tenant_id):
            sync_client.zrem(idx_key, artifact_id)
        
        # Remove from kind index
        if kind:
            for kind_idx_key in self._index_keys(tenant_id, f":kind:{kind}"):
                sync_client.zrem(kind_idx_key, artifact_id)

            # Best-effort cleanup for tenant+source+kind indexes.
            # We read the plaintext fields (if present) to find source_artifact_id.
            try:
                existing = first_existing_key(sync_client, self._item_keys(artifact_id, tenant_id))
                key = existing or self._make_key(artifact_id, tenant_id)
                from ..security.redis_decode_helpers import normalize_redis_str_mapping

                data = normalize_redis_str_mapping(cast(Any, sync_client.hgetall(key)) or {})
                source_id = data.get("source_artifact_id")
                if source_id:
                    for source_kind_idx_key in self._index_keys(
                        tenant_id, f":source:{source_id}:kind:{kind}"
                    ):
                        sync_client.zrem(source_kind_idx_key, artifact_id)
            except Exception:
                # Index cleanup is best-effort (MVP); avoid failing deletes due to index issues.
                pass


_global_store: Optional[ArtifactStoreProtocol] = None


def get_artifact_store() -> ArtifactStoreProtocol:
    """
    Get the singleton artifact store instance.

    Notes:
        - Kept as a simple singleton to match existing tool-artifact usage patterns.
        - Backends can be swapped (e.g., S3/SeaweedFS) behind the protocol.
    """
    global _global_store
    if _global_store is None:
        cfg = Config()
        backend = str(getattr(cfg, "artifact_store_backend", "redis") or "redis").lower()
        if backend == "redis":
            _global_store = RedisArtifactStore()
        elif backend == "s3":
            from .s3_artifact_store import S3ArtifactStore

            _global_store = S3ArtifactStore(config=cfg)
        else:
            raise ValueError(f"Unsupported artifact_store_backend: {backend}")
    return _global_store
