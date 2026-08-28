"""
Motet - S3-Compatible Artifact Store

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Implements the ArtifactStoreProtocol using S3-compatible object storage
    for encrypted artifact payloads and Redis/Valkey for lightweight metadata
    indexes. This keeps large payload bytes out of Redis while preserving the
    existing list and derived-artifact lookup behavior used by and
    artifact flows.

Dependencies:
    - boto3: S3-compatible client for AWS S3, SeaweedFS, and other compatible stores
    - motet.core.distributed.redis_manager: Metadata index storage
    - motet.core.security.encrypted_payload_store: Envelope encryption logic
    - motet.core.config: Backend configuration and retention policy

Usage:
    Configure MOTET_ARTIFACT_STORE_BACKEND=s3 and set
    MOTET_ARTIFACT_STORE_S3_BUCKET plus optional endpoint credentials for
    S3-compatible deployments (SeaweedFS locally, AWS S3 on EC2).

Notes:
    - Payload encryption remains application-level envelope encryption so the
      same ciphertext format works across AWS S3 and SeaweedFS.
    - Exception: streamable media (`video/*`) is stored as a raw,
      range-addressable object (`payload_format=raw`) so HTTP Range maps to
      native ranged GetObject; encryption at rest for these objects is S3 SSE
      (artifact_store_s3_sse / artifact_store_s3_sse_kms_key_id).
    - Metadata sidecar objects let the backend rehydrate Redis metadata/indexes
      when Redis expires or is flushed before the matching S3 payload expires.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
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
    hgetall_first,
    tenant_key,
    zrevrange_ids_with_fallback,
)
from ..security.encrypted_payload_store import IsolationContext, _EncryptedPayloadLogic
from ..security.encryption_contexts import EncryptionContext

logger = structlog.get_logger(__name__)


class S3ArtifactStore(ArtifactStoreProtocol):
    """
    S3-compatible artifact store with Redis-backed metadata indexes.

    Object storage carries encrypted payload wrappers. Redis stores only routing,
    integrity, and list-index metadata so existing callers can continue using
    ArtifactStoreProtocol.list().
    """

    def __init__(
        self,
        service_name: str = "artifact_store",
        *,
        s3_client: Any = None,
        redis_client: Any = None,
        config: Optional[Config] = None,
        encryption_service: Any = None,
    ) -> None:
        self.service_name = service_name
        self._cfg = config or Config()
        self._bucket = str(getattr(self._cfg, "artifact_store_s3_bucket", "") or "")
        if not self._bucket:
            raise ValueError("artifact_store_s3_bucket is required when artifact_store_backend=s3")

        raw_prefix = str(getattr(self._cfg, "artifact_store_s3_prefix", "artifacts") or "artifacts")
        self._prefix = raw_prefix.strip("/")
        self._meta_prefix = "meta:art:"
        self._index_prefix = "idx:art:"
        self._s3 = s3_client or self._make_s3_client()
        self._redis = redis_client or get_sync_redis_client(service_name)

        if encryption_service is None:
            from ..security.encryption_service import get_encryption_service

            encryption_service = get_encryption_service()
        self._encryption = _EncryptedPayloadLogic(encryption_service)

    def put(
        self,
        payload: Union[Dict[str, Any], str, bytes],
        content_type: str = "application/json",
        metadata: Optional[Dict[str, Any]] = None,
        ttl_seconds: Optional[int] = None,
        kind: Union[ArtifactKind, str] = ArtifactKind.UNKNOWN,
        source_artifact_id: Optional[str] = None,
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
            kind_str = kind.value if isinstance(kind, ArtifactKind) else str(kind)
            raw_bytes = self._payload_bytes(payload)
            payload_bytes = len(raw_bytes)
            max_bytes = self._max_payload_bytes(content_type)
            if payload_bytes > max_bytes:
                raise ValueError(f"artifact payload too large: {payload_bytes} bytes > {max_bytes}")

            checksum_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            created_at = time.time()
            effective_ttl = int(ttl_seconds) if ttl_seconds and ttl_seconds > 0 else None
            expires_at = created_at + effective_ttl if effective_ttl is not None else None

            if self._is_raw_eligible(content_type) and isinstance(payload, (bytes, bytearray)):
                # ADR-0118: streamable media is stored as a raw, range-addressable
                # object (encryption via S3 SSE) so HTTP Range maps to native
                # ranged GetObject without full-payload decryption.
                object_key = self._raw_object_key(tenant_id, artifact_id)
                self._s3.put_object(
                    Bucket=self._bucket,
                    Key=object_key,
                    Body=bytes(raw_bytes),
                    ContentType=content_type,
                    Metadata={
                        "artifact-id": artifact_id,
                        "tenant-id": tenant_id,
                        "kind": kind_str,
                        "checksum-sha256": checksum_sha256,
                    },
                    **self._sse_put_kwargs(),
                )
                metadata_mapping = self._metadata_mapping(
                    artifact_id=artifact_id,
                    object_key=object_key,
                    kind=kind_str,
                    content_type=content_type,
                    payload_bytes=payload_bytes,
                    checksum_sha256=checksum_sha256,
                    created_at=created_at,
                    expires_at=expires_at,
                    source_artifact_id=source_artifact_id,
                    metadata=metadata or {},
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    motet_id=motet_id,
                    payload_format="raw",
                )
                self._store_metadata_sidecar(metadata_mapping)
                self._store_metadata_mapping(metadata_mapping, ttl_seconds=effective_ttl)
                return artifact_id

            object_key = self._object_key(tenant_id, artifact_id)
            wrapper_payload: Any = payload
            payload_is_base64 = False
            if isinstance(payload, bytes):
                wrapper_payload = base64.b64encode(payload).decode("ascii")
                payload_is_base64 = True

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
            encrypted_mapping, _ = self._encryption.prepare_put(
                key=object_key,
                payload=json.dumps(wrapper, ensure_ascii=False, separators=(",", ":"), default=str).encode(
                    "utf-8", errors="ignore"
                ),
                isolation=isolation,
                context=EncryptionContext.TOOL_ARTIFACT.value,
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

            self._s3.put_object(
                Bucket=self._bucket,
                Key=object_key,
                Body=json.dumps(encrypted_mapping, separators=(",", ":"), default=str).encode("utf-8"),
                ContentType="application/json",
                Metadata={
                    "artifact-id": artifact_id,
                    "tenant-id": tenant_id,
                    "kind": kind_str,
                    "checksum-sha256": checksum_sha256,
                },
            )
            metadata_mapping = self._metadata_mapping(
                artifact_id=artifact_id,
                object_key=object_key,
                kind=kind_str,
                content_type=content_type,
                payload_bytes=payload_bytes,
                checksum_sha256=checksum_sha256,
                created_at=created_at,
                expires_at=expires_at,
                source_artifact_id=source_artifact_id,
                metadata=metadata or {},
                tenant_id=tenant_id,
                principal_id=principal_id,
                motet_id=motet_id,
            )
            self._store_metadata_sidecar(metadata_mapping)
            self._store_metadata_mapping(metadata_mapping, ttl_seconds=effective_ttl)
            return artifact_id
        except Exception as e:
            logger.error("s3_artifact_store_put_failed", error=str(e), error_type=type(e).__name__)
            raise

    def get(
        self,
        artifact_id: str,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> Optional[Any]:
        try:
            raw_meta = self._raw_format_metadata(artifact_id, tenant_id, principal_id, motet_id)
            if raw_meta is not None:
                object_key = str(raw_meta.get("object_key") or "")
                if not object_key:
                    return None
                response = self._s3.get_object(Bucket=self._bucket, Key=object_key)
                return response["Body"].read()

            wrapper = self._get_wrapper(artifact_id, tenant_id, principal_id, motet_id)
            if not wrapper:
                return None
            payload_value = wrapper.get("payload")
            if wrapper.get("payload_is_base64") is True and isinstance(payload_value, str):
                return base64.b64decode(payload_value)
            return payload_value
        except Exception as e:
            logger.error("s3_artifact_store_get_failed", artifact_id=artifact_id, error=str(e))
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
            if start < 0 or end < start:
                return b""

            raw_meta = self._raw_format_metadata(artifact_id, tenant_id, principal_id, motet_id)
            if raw_meta is not None:
                object_key = str(raw_meta.get("object_key") or "")
                if not object_key:
                    return None
                total = int(float(raw_meta.get("bytes") or 0))
                if total and start >= total:
                    return b""
                if total:
                    end = min(end, total - 1)
                # Native ranged GetObject — no full-object fetch (ADR-0118).
                response = self._s3.get_object(
                    Bucket=self._bucket,
                    Key=object_key,
                    Range=f"bytes={start}-{end}",
                )
                return response["Body"].read()

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
            if start >= len(data):
                return b""
            end = min(end, len(data) - 1)
            return slice_payload_bytes(data, start, end)
        except Exception as e:
            logger.error("s3_artifact_store_get_range_failed", artifact_id=artifact_id, error=str(e))
            return None

    def get_metadata(
        self,
        artifact_id: str,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> Optional[ArtifactMetadata]:
        try:
            meta = self._load_or_recover_metadata(artifact_id, tenant_id, principal_id, motet_id)
            if not meta:
                return None
            if not self._metadata_matches_context(meta, tenant_id, principal_id, motet_id):
                return None
            return self._metadata_to_model(meta)
        except Exception as e:
            logger.error("s3_artifact_store_get_metadata_failed", artifact_id=artifact_id, error=str(e))
            return None

    def update_metadata(
        self,
        artifact_id: str,
        metadata_patch: Dict[str, Any],
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> Optional[ArtifactMetadata]:
        """Merge metadata_patch into Redis/S3 metadata and the encrypted wrapper."""

        try:
            if not isinstance(metadata_patch, dict):
                raise ValueError("metadata_patch must be a dict")
            meta = self._load_or_recover_metadata(artifact_id, tenant_id, principal_id, motet_id)
            if not meta or not self._metadata_matches_context(meta, tenant_id, principal_id, motet_id):
                return None

            if str(meta.get("payload_format") or "envelope") == "raw":
                # Raw payloads (ADR-0118) keep metadata only in Redis + sidecar;
                # there is no encrypted wrapper to rewrite.
                try:
                    md = json.loads(str(meta.get("metadata") or "{}"))
                except Exception:
                    md = {}
                if not isinstance(md, dict):
                    md = {}
                md.update(metadata_patch)
                meta["metadata"] = json.dumps(md, ensure_ascii=False, separators=(",", ":"), default=str)
                ttl_seconds = self._remaining_ttl_seconds(meta)
                if ttl_seconds is None:
                    return None
                self._store_metadata_sidecar({str(k): str(v) for k, v in meta.items()})
                self._store_metadata_mapping({str(k): str(v) for k, v in meta.items()}, ttl_seconds=ttl_seconds)
                return self._metadata_to_model(meta)

            wrapper = self._get_wrapper(artifact_id, tenant_id, principal_id, motet_id)
            if not wrapper:
                return None

            md = wrapper.get("metadata")
            if not isinstance(md, dict):
                md = {}
            md.update(metadata_patch)
            wrapper["metadata"] = md
            meta["metadata"] = json.dumps(md, ensure_ascii=False, separators=(",", ":"), default=str)

            object_key = str(meta.get("object_key") or self._object_key(str(meta["tenant_id"]), artifact_id))
            isolation = IsolationContext(
                tenant_id=str(meta.get("tenant_id") or tenant_id or ""),
                principal_id=str(meta.get("principal_id") or principal_id or "") or None,
                motet_id=str(meta.get("motet_id") or motet_id or "") or None,
            )
            encrypted_mapping, _ = self._encryption.prepare_put(
                key=object_key,
                payload=json.dumps(wrapper, ensure_ascii=False, separators=(",", ":"), default=str).encode(
                    "utf-8", errors="ignore"
                ),
                isolation=isolation,
                context=EncryptionContext.TOOL_ARTIFACT.value,
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
            self._s3.put_object(
                Bucket=self._bucket,
                Key=object_key,
                Body=json.dumps(encrypted_mapping, separators=(",", ":"), default=str).encode("utf-8"),
                ContentType="application/json",
                Metadata={
                    "artifact-id": artifact_id,
                    "tenant-id": str(meta.get("tenant_id") or ""),
                    "kind": str(meta.get("kind") or ""),
                    "checksum-sha256": str(meta.get("checksum_sha256") or ""),
                },
            )

            ttl_seconds = self._remaining_ttl_seconds(meta)
            if ttl_seconds is None:
                return None
            self._store_metadata_sidecar({str(k): str(v) for k, v in meta.items()})
            self._store_metadata_mapping({str(k): str(v) for k, v in meta.items()}, ttl_seconds=ttl_seconds)
            return self._metadata_to_model(meta)
        except Exception as e:
            logger.error("s3_artifact_store_update_metadata_failed", artifact_id=artifact_id, error=str(e), exc_info=True)
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
                return []
            kind_str = kind.value if isinstance(kind, ArtifactKind) else str(kind) if kind else None
            if source_artifact_id and kind_str:
                idx_keys = self._index_keys(
                    tenant_id, f":source:{source_artifact_id}:kind:{kind_str}"
                )
            elif kind_str:
                idx_keys = self._index_keys(tenant_id, f":kind:{kind_str}")
            else:
                idx_keys = self._index_keys(tenant_id)

            artifact_ids = zrevrange_ids_with_fallback(
                self._redis, idx_keys, start=offset, end=offset + limit - 1
            )
            results: List[ArtifactMetadata] = []
            for aid in artifact_ids:
                if isinstance(aid, bytes):
                    aid = aid.decode("utf-8")
                meta = self.get_metadata(str(aid), tenant_id, principal_id, motet_id)
                if not meta:
                    continue
                if conversation_id and meta.metadata.get("conversation_id") != conversation_id:
                    continue
                if source_artifact_id and meta.source_artifact_id != source_artifact_id:
                    continue
                results.append(meta)
            return results
        except Exception as e:
            logger.error("s3_artifact_store_list_failed", error=str(e))
            return []

    def delete(
        self,
        artifact_id: str,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> bool:
        try:
            meta = self._load_or_recover_metadata(artifact_id, tenant_id, principal_id, motet_id)
            if not meta or not self._metadata_matches_context(meta, tenant_id, principal_id, motet_id):
                return False
            object_key = str(meta.get("object_key") or "")
            if object_key:
                self._s3.delete_object(Bucket=self._bucket, Key=object_key)
                self._s3.delete_object(Bucket=self._bucket, Key=self._metadata_object_key(object_key))
            stored_tenant = str(meta.get("tenant_id") or tenant_id or "")
            if stored_tenant:
                delete_candidate_keys(self._redis, self._meta_keys(artifact_id, stored_tenant))
            else:
                self._redis.delete(self._meta_key(artifact_id))
            self._cleanup_indexes(artifact_id, meta.get("tenant_id"), meta.get("kind"), meta.get("source_artifact_id"))
            return True
        except Exception as e:
            logger.error("s3_artifact_store_delete_failed", artifact_id=artifact_id, error=str(e))
            return False

    def _make_s3_client(self) -> Any:
        try:
            boto3 = importlib.import_module("boto3")
            botocore_config = importlib.import_module("botocore.config")
        except ImportError as e:
            raise RuntimeError("boto3 is required for artifact_store_backend=s3") from e

        kwargs: Dict[str, Any] = {
            "service_name": "s3",
            "region_name": getattr(self._cfg, "artifact_store_s3_region", None),
            "endpoint_url": getattr(self._cfg, "artifact_store_s3_endpoint_url", None),
            "use_ssl": bool(getattr(self._cfg, "artifact_store_s3_use_ssl", True)),
        }
        access_key = getattr(self._cfg, "artifact_store_s3_access_key_id", None)
        secret_key = getattr(self._cfg, "artifact_store_s3_secret_access_key", None)
        session_token = getattr(self._cfg, "artifact_store_s3_session_token", None)
        if access_key:
            kwargs["aws_access_key_id"] = access_key
        if secret_key:
            kwargs["aws_secret_access_key"] = secret_key
        if session_token:
            kwargs["aws_session_token"] = session_token
        if bool(getattr(self._cfg, "artifact_store_s3_force_path_style", False)):
            kwargs["config"] = botocore_config.Config(s3={"addressing_style": "path"})
        return boto3.client(**{k: v for k, v in kwargs.items() if v is not None})

    def _object_key(self, tenant_id: str, artifact_id: str) -> str:
        tenant_part = str(tenant_id).strip("/") or "unknown-tenant"
        return f"{self._prefix}/tenant/{tenant_part}/{artifact_id}.json"

    def _raw_object_key(self, tenant_id: str, artifact_id: str) -> str:
        tenant_part = str(tenant_id).strip("/") or "unknown-tenant"
        return f"{self._prefix}/tenant/{tenant_part}/{artifact_id}.bin"

    def _is_raw_eligible(self, content_type: str) -> bool:
        if not bool(getattr(self._cfg, "artifact_store_s3_raw_video_payloads", True)):
            return False
        return str(content_type or "").startswith("video/")

    def _sse_put_kwargs(self) -> Dict[str, Any]:
        sse = str(getattr(self._cfg, "artifact_store_s3_sse", "") or "").strip()
        if not sse:
            return {}
        kwargs: Dict[str, Any] = {"ServerSideEncryption": sse}
        kms_key_id = str(getattr(self._cfg, "artifact_store_s3_sse_kms_key_id", "") or "").strip()
        if sse == "aws:kms" and kms_key_id:
            kwargs["SSEKMSKeyId"] = kms_key_id
        return kwargs

    def _raw_format_metadata(
        self,
        artifact_id: str,
        tenant_id: Optional[str],
        principal_id: Optional[str],
        motet_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Return the metadata mapping when the artifact uses raw payload format, else None."""

        meta = self._load_or_recover_metadata(artifact_id, tenant_id, principal_id, motet_id)
        if not meta or not self._metadata_matches_context(meta, tenant_id, principal_id, motet_id):
            return None
        if str(meta.get("payload_format") or "envelope") != "raw":
            return None
        return meta

    def _metadata_object_key(self, object_key: str) -> str:
        if object_key.endswith(".json"):
            return f"{object_key[:-5]}.metadata.json"
        return f"{object_key}.metadata.json"

    def _logical_meta_key(self, artifact_id: str) -> str:
        return f"{self._meta_prefix}{artifact_id}"

    def _meta_key(self, artifact_id: str, tenant_id: Optional[str] = None) -> str:
        logical = self._logical_meta_key(artifact_id)
        if tenant_id:
            return tenant_key(tenant_id, logical)
        return logical

    def _meta_keys(self, artifact_id: str, tenant_id: str) -> tuple[str, ...]:
        return (tenant_key(tenant_id, self._logical_meta_key(artifact_id)),)

    def _index_logical(self, tenant_id: str, suffix: str = "") -> str:
        return f"{self._index_prefix}tenant:{tenant_id}{suffix}"

    def _index_key(self, tenant_id: str, suffix: str = "") -> str:
        return tenant_key(tenant_id, self._index_logical(tenant_id, suffix))

    def _index_keys(self, tenant_id: str, suffix: str = "") -> tuple[str, ...]:
        return (tenant_key(tenant_id, self._index_logical(tenant_id, suffix)),)

    def _payload_bytes(self, payload: Union[Dict[str, Any], str, bytes]) -> bytes:
        if isinstance(payload, bytes):
            return payload
        if isinstance(payload, str):
            return payload.encode("utf-8", errors="ignore")
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8", errors="ignore")

    def _metadata_mapping(
        self,
        *,
        artifact_id: str,
        object_key: str,
        kind: str,
        content_type: str,
        payload_bytes: int,
        checksum_sha256: str,
        created_at: float,
        expires_at: Optional[float],
        source_artifact_id: Optional[str],
        metadata: Dict[str, Any],
        tenant_id: str,
        principal_id: Optional[str],
        motet_id: Optional[str],
        payload_format: str = "envelope",
    ) -> Dict[str, str]:
        return {
            "id": artifact_id,
            "object_key": object_key,
            "metadata_object_key": self._metadata_object_key(object_key),
            "payload_format": payload_format,
            "kind": kind,
            "content_type": content_type,
            "bytes": str(payload_bytes),
            "checksum_sha256": checksum_sha256,
            "created_at": str(created_at),
            "expires_at": "" if expires_at is None else str(expires_at),
            "source_artifact_id": source_artifact_id or "",
            "metadata": json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), default=str),
            "tenant_id": tenant_id,
            "principal_id": principal_id or "",
            "motet_id": motet_id or "",
        }

    def _store_metadata_sidecar(self, mapping: Dict[str, str]) -> None:
        metadata_object_key = str(mapping.get("metadata_object_key") or "")
        if not metadata_object_key:
            raise ValueError("metadata object key is required")
        self._s3.put_object(
            Bucket=self._bucket,
            Key=metadata_object_key,
            Body=json.dumps(mapping, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"),
            ContentType="application/json",
            Metadata={
                "artifact-id": str(mapping.get("id") or ""),
                "tenant-id": str(mapping.get("tenant_id") or ""),
                "kind": str(mapping.get("kind") or ""),
            },
        )

    def _store_metadata_mapping(self, mapping: Dict[str, str], *, ttl_seconds: Optional[int]) -> None:
        artifact_id = str(mapping["id"])
        tenant_id = str(mapping["tenant_id"])
        kind = str(mapping["kind"])
        created_at = float(mapping.get("created_at") or 0.0)
        source_artifact_id = str(mapping.get("source_artifact_id") or "") or None

        meta_key = self._meta_key(artifact_id, tenant_id)
        self._redis.hset(meta_key, mapping=mapping)
        if ttl_seconds and ttl_seconds > 0:
            self._redis.expire(meta_key, int(ttl_seconds))

        tenant_idx_key = self._index_key(tenant_id)
        self._redis.zadd(tenant_idx_key, {artifact_id: created_at})
        if ttl_seconds and ttl_seconds > 0:
            self._redis.expire(tenant_idx_key, int(ttl_seconds))

        kind_idx_key = self._index_key(tenant_id, f":kind:{kind}")
        self._redis.zadd(kind_idx_key, {artifact_id: created_at})
        if ttl_seconds and ttl_seconds > 0:
            self._redis.expire(kind_idx_key, int(ttl_seconds))

        if source_artifact_id:
            source_kind_idx_key = self._index_key(
                tenant_id, f":source:{source_artifact_id}:kind:{kind}"
            )
            self._redis.zadd(source_kind_idx_key, {artifact_id: created_at})
            if ttl_seconds and ttl_seconds > 0:
                self._redis.expire(source_kind_idx_key, int(ttl_seconds))

    def _load_metadata(
        self, artifact_id: str, tenant_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        if tenant_id:
            raw = cast(Any, hgetall_first(self._redis, self._meta_keys(artifact_id, tenant_id))) or {}
        else:
            raw = cast(Any, self._redis.hgetall(self._meta_key(artifact_id))) or {}
        if not raw:
            return None
        normalized: Dict[str, Any] = {}
        for key, value in raw.items():
            k = key.decode("utf-8") if isinstance(key, bytes) else str(key)
            v = value.decode("utf-8") if isinstance(value, bytes) else value
            normalized[k] = v
        return normalized

    def _load_or_recover_metadata(
        self,
        artifact_id: str,
        tenant_id: Optional[str],
        principal_id: Optional[str],
        motet_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        meta = self._load_metadata(artifact_id, tenant_id)
        if meta:
            return meta

        sidecar_meta = self._load_metadata_sidecar(artifact_id, tenant_id)
        if not sidecar_meta:
            return None
        if not self._metadata_matches_context(sidecar_meta, tenant_id, principal_id, motet_id):
            return None

        ttl_seconds = self._remaining_ttl_seconds(sidecar_meta)
        if ttl_seconds is None:
            return None
        self._store_metadata_mapping({str(k): str(v) for k, v in sidecar_meta.items()}, ttl_seconds=ttl_seconds)
        return sidecar_meta

    def _load_metadata_sidecar(self, artifact_id: str, tenant_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not tenant_id:
            return None
        object_key = self._object_key(tenant_id, artifact_id)
        metadata_object_key = self._metadata_object_key(object_key)
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=metadata_object_key)
            body = response["Body"].read()
            data = json.loads(body.decode("utf-8", errors="ignore"))
            if not isinstance(data, dict):
                return None
            return {str(k): v for k, v in data.items()}
        except Exception:
            return None

    def _remaining_ttl_seconds(self, meta: Dict[str, Any]) -> Optional[int]:
        expires_raw = meta.get("expires_at")
        if not expires_raw:
            return 0
        try:
            remaining = int(float(expires_raw) - time.time())
        except (TypeError, ValueError):
            return None
        if remaining <= 0:
            return None
        return remaining

    def _metadata_matches_context(
        self,
        meta: Dict[str, Any],
        tenant_id: Optional[str],
        principal_id: Optional[str],
        motet_id: Optional[str],
    ) -> bool:
        if tenant_id and meta.get("tenant_id") != tenant_id:
            return False
        if principal_id and meta.get("principal_id") not in (principal_id, None, ""):
            return False
        if motet_id and meta.get("motet_id") not in (motet_id, None, ""):
            return False
        return True

    def _metadata_to_model(self, meta: Dict[str, Any]) -> ArtifactMetadata:
        try:
            kind = ArtifactKind(str(meta.get("kind") or ArtifactKind.UNKNOWN.value))
        except ValueError:
            kind = ArtifactKind.UNKNOWN
        expires_raw = meta.get("expires_at")
        metadata_raw = meta.get("metadata") or "{}"
        try:
            metadata = json.loads(metadata_raw)
        except Exception:
            metadata = {}
        return ArtifactMetadata(
            id=str(meta.get("id") or ""),
            kind=kind,
            content_type=str(meta.get("content_type") or "application/octet-stream"),
            payload_format=str(meta.get("payload_format") or "envelope"),
            bytes=int(float(meta.get("bytes") or 0)),
            checksum_sha256=str(meta.get("checksum_sha256") or ""),
            created_at=float(meta.get("created_at") or 0.0),
            expires_at=float(expires_raw) if expires_raw else None,
            source_artifact_id=str(meta.get("source_artifact_id") or "") or None,
            tenant_id=str(meta.get("tenant_id") or "") or None,
            principal_id=str(meta.get("principal_id") or "") or None,
            motet_id=str(meta.get("motet_id") or "") or None,
            metadata=metadata if isinstance(metadata, dict) else {},
        )

    def _max_payload_bytes(self, content_type: str) -> int:
        if str(content_type or "").startswith("video/"):
            return int(getattr(self._cfg, "artifact_max_video_bytes", 536_870_912) or 536_870_912)
        return int(getattr(self._cfg, "artifact_store_max_bytes", 25_000_000) or 25_000_000)

    def _get_wrapper(
        self,
        artifact_id: str,
        tenant_id: Optional[str],
        principal_id: Optional[str],
        motet_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        if not bool(getattr(self._cfg, "artifact_store_encryption", True)):
            raise ValueError("artifact_store_encryption is disabled; refusing plaintext artifact reads")
        if not tenant_id:
            raise ValueError("tenant_id is required for encrypted artifact retrieval")

        meta = self._load_or_recover_metadata(artifact_id, tenant_id, principal_id, motet_id)
        if not meta or not self._metadata_matches_context(meta, tenant_id, principal_id, motet_id):
            return None
        object_key = str(meta.get("object_key") or self._object_key(tenant_id, artifact_id))
        response = self._s3.get_object(Bucket=self._bucket, Key=object_key)
        body = response["Body"].read()
        encrypted_mapping = json.loads(body.decode("utf-8", errors="ignore"))
        decrypt_result = self._encryption.process_get(
            key=object_key,
            data=encrypted_mapping,
            isolation=IsolationContext(tenant_id=tenant_id, principal_id=principal_id, motet_id=motet_id),
            context=EncryptionContext.TOOL_ARTIFACT.value,
        )
        return json.loads(decrypt_result.plaintext.decode("utf-8", errors="ignore"))

    def _cleanup_indexes(
        self,
        artifact_id: str,
        tenant_id: Optional[str],
        kind: Optional[str],
        source_artifact_id: Optional[str],
    ) -> None:
        if not tenant_id:
            return
        for idx_key in self._index_keys(tenant_id):
            self._redis.zrem(idx_key, artifact_id)
        if kind:
            for kind_key in self._index_keys(tenant_id, f":kind:{kind}"):
                self._redis.zrem(kind_key, artifact_id)
        if source_artifact_id and kind:
            for source_key in self._index_keys(
                tenant_id, f":source:{source_artifact_id}:kind:{kind}"
            ):
                self._redis.zrem(source_key, artifact_id)
