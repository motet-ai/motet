"""
Motet - Redis Command Data Manager

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Redis Command Data Manager for the Motet distributed framework.
    Parent wait loads ``cmd:outcome`` via ``retrieve_command_wait_outcome``,
    which resolves ``_redis_result_key`` pointers into ``cmd:result`` so
    unary ``motet.do`` and gather/map join see the body (issues
    #229 / #242).

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.distributed.redis_command_data_manager import RedisCommandDataManager

    envelope = manager.retrieve_command_wait_outcome(command_id, tenant_id=tid)

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
    - cmd:meta updates resolve ``{tenant}:cmd:meta:{id}`` when tenant_id is
      omitted. A logical-only HGETALL leaves status at executing.
    - ``cmd:outcome.result`` may be ``{_redis_result_key:...}`` for large
      children; hydrate happens on retrieve, not at each waiter.
"""


import json
import msgpack
import structlog
import time
import os
import hashlib
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Union, cast

from redis import Redis

from motet.core.commands.distributed_types import AGENTIC_LOOP_ITERATION_META_KEY

from .redis_manager import get_sync_binary_redis_client, get_redis_manager
from .tenant_keys import decode_redis_id, tenant_key
from ..security.encryption_service import get_encryption_service, EncryptionError
from ..security.aad_helpers import compute_command_data_aad, compute_command_result_aad
from ..security.envelope_helper import (
    envelope_encrypt_bytes,
    envelope_decrypt_bytes,
)
from ..security.aad_helpers import compute_cmd_meta_aad
from ..security.encryption_contexts import EncryptionContext
from ..security.json_helpers import json_dumps_compact, json_loads
from ..security.redis_decode_helpers import normalize_redis_str_mapping

# Debug mode configuration
DEBUG_COMMAND_DATA_TTL = int(os.getenv("MOTET_DEBUG_COMMAND_DATA_TTL", "3600"))  # 1 hour

# Keep minimal routing/ops fields plaintext; encrypt the rest into `_envelope`.
_CMD_META_PLAINTEXT_KEYS = frozenset({
    "command_id",
    "command_type",
    "task_id",
    "conversation_id",
    "created_at",
    "parent_command_id",
    "triggered_by",
    "worker_id",
    "status",
    "duration_ms",
    "tenant_id",
    "motet_id",
    "principal_id",
    AGENTIC_LOOP_ITERATION_META_KEY,
})
_CMD_META_PLAINTEXT_UPDATE_KEYS = _CMD_META_PLAINTEXT_KEYS | {
    "executed_at",
    "completed_at",
}

logger = structlog.get_logger(__name__)


class RedisCommandDataManager:
    """Manages command data storage in Redis with TTL and cleanup."""
    
    def __init__(self, redis_client: Optional[Redis] = None, ttl_seconds: int = 3600, enable_encryption: bool = True):
        """
        Initialize Redis command data manager.
        
        Args:
            redis_client: Redis client instance (optional, will create if not provided)
            ttl_seconds: Default TTL for stored data in seconds
            enable_encryption: Whether to enable encryption at rest (default: True)
        """
        if redis_client is None:
            # Use the sync binary Redis client for MsgPack binary data
            self.redis: Redis = get_sync_binary_redis_client("command_data_manager")
        else:
            self.redis = redis_client
        self.default_ttl = ttl_seconds
        self._debug_mode = False  # Will be set based on environment
        self._enable_encryption = enable_encryption
        self._encryption_service = None
        if self._enable_encryption:
            try:
                self._encryption_service = get_encryption_service()
            except Exception as e:
                # ADR-0056 development policy: fail-closed. If encryption is enabled, do not
                # silently disable it, as that would allow plaintext writes to Redis.
                logger.error(
                    "Failed to initialize encryption service (encryption required)",
                    error=str(e),
                    exc_info=True,
                )
                raise RuntimeError("Encryption is enabled but encryption service is unavailable") from e
        self._redis_manager = None

    def _get_redis_manager(self):
        if self._redis_manager is None:
            try:
                self._redis_manager = get_redis_manager()
            except Exception as e:
                logger.warning("Failed to initialize UnifiedRedisManager", error=str(e))
                self._redis_manager = None
        return self._redis_manager

    def _command_key(self, kind: str, command_id: str, tenant_id: Optional[str] = None) -> str:
        logical = f"cmd:{kind}:{command_id}"
        tid = (tenant_id or "").strip()
        return tenant_key(tid, logical) if tid else logical

    def _resolve_cmd_meta_key(
        self, command_id: str, tenant_id: Optional[str] = None
    ) -> tuple[str, Dict[Any, Any]]:
        """
        Find the cmd:meta hash for *command_id*.

        Writers store ``{tenant}:cmd:meta:{id}``. Updates that omit tenant_id
        must still find that key; otherwise status stays ``executing``.
        """
        logical = f"cmd:meta:{command_id}"
        tid = (tenant_id or "").strip()
        candidates = [tenant_key(tid, logical)] if tid else [logical]
        seen: set[str] = set()
        for key in candidates:
            if key in seen:
                continue
            seen.add(key)
            existing = self.redis.hgetall(key)
            if existing:
                return key, cast(Dict[Any, Any], existing)
        for raw_key in self.redis.scan_iter(match=f"*:cmd:meta:{command_id}", count=10):
            key = decode_redis_id(raw_key)
            if key in seen:
                continue
            seen.add(key)
            existing = self.redis.hgetall(key)
            if existing:
                return key, cast(Dict[Any, Any], existing)
        return logical, {}

    def _get_bytes_with_fallback(self, key: str, tenant_id: Optional[str] = None) -> tuple[Optional[bytes], str]:
        """GET key, then the tenant-prefixed/legacy alternate when tenant_id is known."""
        tid = (tenant_id or "").strip()
        candidates = (key,)
        if tid:
            logical = key
            prefix = f"{tid}:"
            if logical.startswith(prefix):
                logical = logical[len(prefix) :]
            candidates = (tenant_key(tid, logical),)
        for candidate in candidates:
            data = self.redis.get(candidate)
            if data:
                return cast(bytes, data), candidate
        return None, key
    
    def _serialize_for_msgpack(self, obj):
        """
        Recursively convert datetime objects and other non-serializable types to JSON-compatible formats.
        
        This ensures MsgPack can serialize the data without errors.
        
        Args:
            obj: Object to serialize (dict, list, datetime, etc.)
            
        Returns:
            Serialized object with datetime objects converted to ISO strings
        """
        # Handle None and simple types
        if obj is None or isinstance(obj, (bool, int, float, str, bytes)):
            return obj
        elif isinstance(obj, datetime):
            return obj.isoformat()
        elif isinstance(obj, dict):
            # Check if this is a message dict (has 'role' and 'content')
            # Filter out null/empty fields from message dicts to keep API calls clean
            if 'role' in obj and 'content' in obj:
                # This is a message dict - filter out None, empty dicts, and empty lists
                filtered = {}
                for key, value in obj.items():
                    serialized_value = self._serialize_for_msgpack(value)
                    # Always include role and content
                    if key in ('role', 'content'):
                        filtered[key] = serialized_value
                    # Include other fields only if they have meaningful values
                    elif serialized_value not in (None, {}, []):
                        filtered[key] = serialized_value
                return filtered
            else:
                # Regular dict - recursively serialize without filtering
                return {key: self._serialize_for_msgpack(value) for key, value in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [self._serialize_for_msgpack(item) for item in obj]
        elif isinstance(obj, set):
            return [self._serialize_for_msgpack(item) for item in obj]
        elif hasattr(obj, 'model_dump'):
            # Handle Pydantic models - exclude None values
            # Message filtering happens at the dict level above
            return self._serialize_for_msgpack(obj.model_dump(mode='json', exclude_none=True))
        else:
            # For any other non-serializable object, convert to string
            try:
                import json
                json.dumps(obj)
                return obj  # Already JSON-serializable
            except (TypeError, ValueError):
                # Not JSON-serializable - convert to string
                return str(obj)
    
    def calculate_redis_ttl(self, command_timeout_seconds: Optional[int] = None) -> int:
        """
        Calculate Redis TTL based on command timeout with safety margin.
        
        Args:
            command_timeout_seconds: Command timeout in seconds
            
        Returns:
            TTL in seconds (2x command timeout + 5 minutes minimum)
        """
        if command_timeout_seconds is not None:
            # TTL = 2x command timeout + 5 minutes minimum
            return max(command_timeout_seconds * 2, 300)
        return self.default_ttl
    
    def _get_debug_ttl(self, base_ttl: int, command_type: str) -> int:
        """
        Get debug-aware TTL for command data.
        
        Args:
            base_ttl: Base TTL in seconds
            command_type: Type of command for debug categorization
            
        Returns:
            Extended TTL if in debug mode, otherwise base TTL
        """
        if not self._debug_mode:
            return base_ttl
        
        # Debug mode: extend TTL significantly for investigation
        debug_extensions = {
            "core.agent_turn": 3600,          # 1 hour for turn commands
            "core.model_inference": 1800,    # 30 minutes for model commands
            "core.tool_execution": 900,      # 15 minutes for tool commands
            "core.workflow_execution": 7200, # 2 hours for workflow commands
        }
        
        extension = debug_extensions.get(command_type, 1800)  # Default 30 minutes
        return base_ttl + extension

    def _encrypt_storage_payload(
        self,
        payload: Dict[str, Any],
        tenant_id: Optional[str],
        command_id: str,
        context: str,
        motet_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Encrypt a payload using envelope encryption, returning the storage blob and metrics.

        Args:
            payload: Dict containing the data/result plus metadata.
            tenant_id: Tenant identifier (required for encryption).
            command_id: Command identifier (for logging).
            context: Textual context for logging (e.g., "command_data", "command_result").
            motet_id: Motet identifier (required for encryption, used in AAD).

        Returns:
            Dict with keys:
                - storage: Storage payload ready for MsgPack serialization
                - encryption_time_ms: float
                - dek_wrap_time_ms: float
                - encryption_mode: str
        """
        encryption_time = 0.0
        dek_wrap_time = 0.0
        encryption_mode = "none"

        # ADR-0056: fail-closed. If encryption is enabled, tenant_id is required and
        # encryption failures must not fall back to plaintext storage.
        if not self._enable_encryption:
            payload["encrypted"] = False
            return {
                "storage": payload,
                "encryption_time_ms": encryption_time,
                "dek_wrap_time_ms": dek_wrap_time,
                "encryption_mode": encryption_mode,
            }

        tenant_id_s = (tenant_id or "").strip()
        if not tenant_id_s:
            raise ValueError(f"tenant_id is required for {context} encryption")
        if not self._encryption_service:
            raise RuntimeError(f"Encryption is enabled but encryption service is unavailable for {context}")

        # ADR-0056 hardening: Bind ciphertext to (tenant_id, motet_id, command_id, payload context)
        # to prevent cut-and-paste substitution across commands within the same tenant and across motets.
        motet_id_s = (motet_id or "").strip()
        if not motet_id_s:
            raise ValueError(f"motet_id is required for {context} encryption")
        if context == EncryptionContext.COMMAND_DATA.value:
            aad = compute_command_data_aad(command_id=command_id, tenant_id=tenant_id_s, motet_id=motet_id_s)
        elif context == EncryptionContext.COMMAND_RESULT.value:
            aad = compute_command_result_aad(command_id=command_id, tenant_id=tenant_id_s, motet_id=motet_id_s)
        else:
            aad = None

        metadata = payload.get("metadata", {})
        encryption_start = time.time()
        try:
            payload_bytes = cast(bytes, msgpack.packb(payload, use_bin_type=True))
            encrypt_result = envelope_encrypt_bytes(
                payload_bytes,
                tenant_id_s,
                self._encryption_service,
                context=context,
                aad=aad,
            )

            storage_blob = {
                **encrypt_result.envelope,
                "metadata": metadata,
                "encrypted": True,
            }

            encryption_mode = storage_blob.get("encryption_mode", "envelope-v1")
            encryption_time = encrypt_result.encryption_time_ms
            dek_wrap_time = encrypt_result.dek_wrap_time_ms

            logger.debug(
                "Payload encrypted via envelope",
                context=context,
                command_id=command_id,
                tenant_id=tenant_id_s,
                encryption_time_ms=round(encryption_time, 2),
                dek_wrap_time_ms=round(dek_wrap_time, 2),
            )

            return {
                "storage": storage_blob,
                "encryption_time_ms": encryption_time,
                "dek_wrap_time_ms": dek_wrap_time,
                "encryption_mode": encryption_mode,
            }
        except (EncryptionError, ValueError) as e:
            encryption_time = (time.time() - encryption_start) * 1000
            logger.error(
                "Failed to encrypt payload (fail-closed)",
                context=context,
                command_id=command_id,
                tenant_id=tenant_id_s,
                error=str(e),
                encryption_time_ms=round(encryption_time, 2),
                exc_info=True,
            )
            raise RuntimeError(f"Failed to encrypt {context}: {e}") from e

    def _decrypt_storage_payload(
        self,
        storage_data: Dict[str, Any],
        tenant_id: Optional[str],
        command_id: str,
        context: str,
        payload_field: str,
        motet_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Decrypt a storage payload produced by _encrypt_storage_payload.

        Args:
            storage_data: Raw dict retrieved from Redis.
            tenant_id: Optional tenant_id supplied externally.
            command_id: Command identifier for logging.
            context: Logging context ("command_data" / "command_result").
            payload_field: Field containing the payload ("data" or "result").
            motet_id: Optional motet_id for AAD computation. Falls back to metadata if not provided.

        Returns:
            Dict with keys:
                - payload: decrypted payload value (e.g., command data/result)
                - metadata: associated metadata dict
                - decryption_time_ms: float
                - encrypted: bool
                - tenant_id: resolved tenant id
        """
        is_encrypted = storage_data.get("encrypted", False)
        metadata = storage_data.get("metadata", {}) or {}
        resolved_tenant_id = tenant_id
        decryption_time = 0.0

        if not is_encrypted:
            return {
                "payload": storage_data.get(payload_field, {}),
                "metadata": metadata,
                "decryption_time_ms": decryption_time,
                "encrypted": False,
                "tenant_id": resolved_tenant_id
            }

        if not self._enable_encryption:
            raise ValueError("Encrypted payload found but encryption service not available")
        enc = self._encryption_service
        if enc is None:
            raise ValueError("Encrypted payload found but encryption service not available")

        decrypt_start = time.time()
        try:
            # Try caller-provided tenant_id first, then fall back to envelope's tenant_id
            resolved_tenant_id_s = (tenant_id or "").strip()
            if not resolved_tenant_id_s:
                # Try to read tenant_id from the envelope (stored during encryption)
                encryption_blob = storage_data.get("encryption", {})
                resolved_tenant_id_s = (encryption_blob.get("tenant_id") or "").strip()
            if not resolved_tenant_id_s:
                # Fail closed: we require tenant_id to compute AAD and decrypt envelope payloads.
                raise ValueError(f"tenant_id is required for {context} decryption (not provided by caller and not in envelope)")

            # Get motet_id: prefer explicit parameter, then metadata - require it for decryption
            motet_id_s = (motet_id or "").strip() or (metadata.get("motet_id") or "").strip()
            if not motet_id_s:
                raise ValueError(f"motet_id is required for {context} decryption (not provided by caller and not in metadata)")

            if context == EncryptionContext.COMMAND_DATA.value:
                aad = compute_command_data_aad(command_id=command_id, tenant_id=resolved_tenant_id_s, motet_id=motet_id_s)
            elif context == EncryptionContext.COMMAND_RESULT.value:
                aad = compute_command_result_aad(command_id=command_id, tenant_id=resolved_tenant_id_s, motet_id=motet_id_s)
            else:
                aad = None

            decrypt_result = envelope_decrypt_bytes(
                storage_data,
                enc,
                context=context,
                aad=aad,
            )
            resolved_tenant_id = decrypt_result.tenant_id or resolved_tenant_id
            decrypted_storage_data = msgpack.unpackb(decrypt_result.plaintext, raw=False)
            payload = decrypted_storage_data.get(payload_field, {})
            decryption_time = decrypt_result.decryption_time_ms

            logger.debug(
                "Payload decrypted via envelope",
                context=context,
                command_id=command_id,
                tenant_id=resolved_tenant_id,
                decryption_time_ms=round(decryption_time, 2),
                dek_unwrap_time_ms=round(decrypt_result.dek_unwrap_time_ms, 2),
            )

            return {
                "payload": payload,
                "metadata": metadata,
                "decryption_time_ms": decryption_time,
                "encrypted": True,
                "tenant_id": resolved_tenant_id,
                "dek_unwrap_time_ms": decrypt_result.dek_unwrap_time_ms,
            }
        except (EncryptionError, ValueError) as e:
            decryption_time = (time.time() - decrypt_start) * 1000
            logger.error(
                "Failed to decrypt payload",
                context=context,
                command_id=command_id,
                tenant_id=resolved_tenant_id,
                motet_id_param=motet_id,
                motet_id_from_metadata=metadata.get("motet_id") if metadata else None,
                error=str(e),
                error_type=type(e).__name__,
                decryption_time_ms=round(decryption_time, 2),
                exc_info=True
            )
            raise ValueError(f"Failed to decrypt {context}: {e}") from e
    
    def store_command_data(
        self, 
        command_id: str, 
        data: Dict[str, Any],
        command_timeout_seconds: Optional[int] = None,
        command_type: str = "unknown",
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None
    ) -> str:
        """
        Store command data and return Redis key.
        
        Args:
            command_id: Unique command identifier
            data: Command data to store
            command_timeout_seconds: Command timeout for TTL calculation
            command_type: Type of command for debug categorization
            tenant_id: Optional tenant ID for encryption (required if encryption enabled)
            motet_id: Optional motet ID for encryption (required if encryption enabled)
            
        Returns:
            Redis key where data was stored
        """
        if self._enable_encryption:
            if not tenant_id:
                raise ValueError("tenant_id is required for command data encryption")
            if not motet_id:
                raise ValueError("motet_id is required for command data encryption")
        
        start_time = time.time()
        try:
            # Calculate TTL based on command timeout
            ttl_start = time.time()
            base_ttl = self.calculate_redis_ttl(command_timeout_seconds)
            debug_ttl = self._get_debug_ttl(base_ttl, command_type)
            ttl_time = (time.time() - ttl_start) * 1000  # Convert to ms
            
            # Store with metadata
            key = self._command_key("data", command_id, tenant_id)
            
            # Prepare storage data
            prep_start = time.time()
            storage_data = {
                "data": data,
                "metadata": {
                    "command_type": command_type,
                    "stored_at": datetime.utcnow().isoformat(),
                    "ttl_seconds": debug_ttl,
                    "debug_mode": self._debug_mode,
                    "original_ttl": base_ttl,
                    "command_id": command_id,
                    "motet_id": motet_id  # Store motet_id in metadata for retrieval
                }
            }
            
            # Serialize datetime objects and Pydantic models before MsgPack serialization
            # This prevents "can not serialize 'datetime.datetime' object" errors
            storage_data = cast(
                Dict[str, Any],
                self._serialize_for_msgpack(storage_data),
            )
            prep_time = (time.time() - prep_start) * 1000  # Convert to ms
            
            encryption_meta = self._encrypt_storage_payload(
                storage_data,
                tenant_id,
                command_id,
                context=EncryptionContext.COMMAND_DATA.value,
                motet_id=motet_id,
            )
            storage_data = encryption_meta["storage"]
            encryption_time = encryption_meta["encryption_time_ms"]
            dek_wrap_time = encryption_meta["dek_wrap_time_ms"]
            encryption_mode = encryption_meta["encryption_mode"]
            
            # Serialize data with MsgPack
            serialize_start = time.time()
            serialized_data = cast(bytes, msgpack.packb(storage_data, use_bin_type=True))
            serialize_time = (time.time() - serialize_start) * 1000  # Convert to ms
            
            # Store in Redis
            redis_start = time.time()
            ttl_int = int(debug_ttl)
            self.redis.setex(key, ttl_int, serialized_data)
            redis_time = (time.time() - redis_start) * 1000  # Convert to ms
            
            total_time = (time.time() - start_time) * 1000  # Convert to ms
            
            logger.info(
                "Stored command data in Redis",
                command_id=command_id,
                command_type=command_type,
                tenant_id=tenant_id,
                encrypted=self._enable_encryption and tenant_id is not None,
                ttl_seconds=debug_ttl,
                data_size_bytes=len(serialized_data),
                timing_ms={
                    "total": round(total_time, 2),
                    "ttl_calculation": round(ttl_time, 2),
                    "data_preparation": round(prep_time, 2),
                    "encryption": round(encryption_time, 2),
                    "dek_wrap": round(dek_wrap_time, 2),
                    "serialization": round(serialize_time, 2),
                    "redis_operation": round(redis_time, 2)
                },
                encryption_mode=encryption_mode
            )
            
            return key
            
        except Exception as e:
            logger.error(
                "Failed to store command data in Redis",
                command_id=command_id,
                error=str(e),
                exc_info=True
            )
            raise RuntimeError(f"Failed to store command data: {e}") from e
    
    
    def retrieve_command_data(self, key: str, tenant_id: Optional[str] = None, motet_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Retrieve command data from Redis.
        
        Args:
            key: Redis key to retrieve data from
            tenant_id: Optional tenant ID for decryption (required if data is encrypted)
            motet_id: Optional motet ID for decryption (used in AAD, falls back to metadata)
            
        Returns:
            Command data dictionary
            
        Raises:
            ValueError: If data not found or expired
            EncryptionError: If decryption fails
        """
        start_time = time.time()
        try:
            # Retrieve from Redis
            redis_start = time.time()
            data, key = self._get_bytes_with_fallback(key, tenant_id)
            redis_time = (time.time() - redis_start) * 1000  # Convert to ms
            
            if not data:
                raise ValueError(f"Command data not found or expired: {key}")
            data_bytes = cast(bytes, data)

            # Deserialize MsgPack data
            deserialize_start = time.time()
            storage_data = msgpack.unpackb(data_bytes, raw=False)
            deserialize_time = (time.time() - deserialize_start) * 1000  # Convert to ms
            
            # Check if data is encrypted
            is_encrypted = storage_data.get("encrypted", False)
            metadata = storage_data.get("metadata", {}) or {}
            
            extract_start = time.time()
            # Pass motet_id through - _decrypt_storage_payload handles resolution from metadata if needed
            decrypt_result = self._decrypt_storage_payload(
                storage_data,
                tenant_id,
                metadata.get("command_id", key),
                context=EncryptionContext.COMMAND_DATA.value,
                payload_field="data",
                motet_id=motet_id
            )
            command_data = decrypt_result["payload"] or {}
            metadata = decrypt_result["metadata"] or metadata
            decryption_time = decrypt_result["decryption_time_ms"]
            is_encrypted = decrypt_result["encrypted"]
            tenant_id = decrypt_result["tenant_id"]
            extract_time = 0 if is_encrypted else (time.time() - extract_start) * 1000
            
            total_time = (time.time() - start_time) * 1000  # Convert to ms
            
            # Debug: Check what data we're retrieving
            # Note: Avoid noisy prints; log structured metadata only.
            if "messages" in command_data and isinstance(command_data.get("messages"), list):
                logger.debug(
                    "retrieve_command_data_messages_summary",
                    key=key,
                    command_id=metadata.get("command_id", key),
                    message_count=len(command_data.get("messages", [])),
                )
            
            logger.info(
                "Retrieved command data from Redis",
                key=key,
                tenant_id=tenant_id,
                encrypted=is_encrypted,
                command_type=metadata.get("command_type"),
                stored_at=metadata.get("stored_at"),
                data_size_bytes=len(data_bytes),
                timing_ms={
                    "total": round(total_time, 2),
                    "redis_operation": round(redis_time, 2),
                    "deserialization": round(deserialize_time, 2),
                    "decryption": round(decryption_time, 2),
                    "data_extraction": round(extract_time, 2)
                }
            )
            
            return command_data
            
        except Exception as e:
            logger.error(
                "Failed to retrieve command data from Redis",
                key=key,
                error=str(e),
                exc_info=True
            )
            raise ValueError(f"Command data not found or expired: {key}") from e
    
    def store_command_result(
        self, 
        command_id: str, 
        result: Any,
        command_timeout_seconds: Optional[int] = None,
        command_type: str = "unknown",
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        kind: str = "result",
    ) -> str:
        """
        Store command result and return Redis key.
        
        Args:
            command_id: Unique command identifier
            result: Command result to store
            command_timeout_seconds: Command timeout for TTL calculation (optional)
            command_type: Type of command for debug categorization
            tenant_id: Optional tenant ID used for encryption (required if encryption enabled)
            motet_id: Optional motet ID used for encryption (required if encryption enabled)
            kind: ``result`` (domain / large-payload / debug) or ``outcome``
                (parent wait envelope, issue #229). Do not overwrite ``result``
                with the wait envelope — large results already live there.
            
        Returns:
            Redis key where result was stored
            
        Raises:
            ValueError: If tenant_id or motet_id is required but not provided
        """
        if self._enable_encryption:
            if not tenant_id:
                raise ValueError("tenant_id is required for command result encryption")
            if not motet_id:
                raise ValueError("motet_id is required for command result encryption")
        
        start_time = time.time()
        try:
            # Calculate TTL for result
            ttl_start = time.time()
            if command_timeout_seconds is not None:
                # Use the original TTL calculation for large results
                ttl = self.calculate_redis_ttl(command_timeout_seconds)
            else:
                # Use debug-aware TTL for debugging purposes
                ttl = DEBUG_COMMAND_DATA_TTL if self._debug_mode else self.default_ttl
            ttl_time = (time.time() - ttl_start) * 1000  # Convert to ms
            
            # Store result with metadata
            prep_start = time.time()
            key_kind = (kind or "result").strip() or "result"
            key = self._command_key(key_kind, command_id, tenant_id)
            storage_data = {
                "result": result,
                "metadata": {
                    "command_id": command_id,
                    "command_type": command_type,
                    "stored_at": datetime.utcnow().isoformat(),
                    "ttl_seconds": ttl,
                    "result_type": type(result).__name__,
                    "debug_mode": self._debug_mode,
                    "motet_id": motet_id  # Store motet_id in metadata for retrieval
                }
            }
            
            # Serialize non-serializable objects (like Request objects)
            # This prevents "can not serialize 'Request' object" errors
            storage_data = cast(
                Dict[str, Any],
                self._serialize_for_msgpack(storage_data),
            )
            prep_time = (time.time() - prep_start) * 1000  # Convert to ms
            
            encryption_meta = self._encrypt_storage_payload(
                storage_data,
                tenant_id,
                command_id,
                context=EncryptionContext.COMMAND_RESULT.value,
                motet_id=motet_id,
            )
            storage_data = encryption_meta["storage"]
            encryption_time = encryption_meta["encryption_time_ms"]
            dek_wrap_time = encryption_meta["dek_wrap_time_ms"]
            encryption_mode = encryption_meta["encryption_mode"]

            # Use MsgPack for storage
            serialize_start = time.time()
            serialized_data = cast(bytes, msgpack.packb(storage_data, use_bin_type=True))
            serialize_time = (time.time() - serialize_start) * 1000  # Convert to ms
            
            # Store in Redis
            redis_start = time.time()
            self.redis.setex(key, ttl, serialized_data)
            redis_time = (time.time() - redis_start) * 1000  # Convert to ms
            
            total_time = (time.time() - start_time) * 1000  # Convert to ms
            
            logger.info(
                "Stored command result in Redis",
                command_id=command_id,
                command_type=command_type,
                tenant_id=tenant_id,
                ttl_seconds=ttl,
                result_size_bytes=len(serialized_data),
                timing_ms={
                    "total": round(total_time, 2),
                    "ttl_calculation": round(ttl_time, 2),
                    "data_preparation": round(prep_time, 2),
                    "dek_wrap": round(dek_wrap_time, 2),
                    "serialization": round(serialize_time, 2),
                    "redis_operation": round(redis_time, 2),
                    "encryption": round(encryption_time, 2)
                },
                encrypted=self._enable_encryption and tenant_id is not None,
                encryption_mode=encryption_mode
            )
            
            return key
            
        except Exception as e:
            logger.error(
                "Failed to store command result in Redis",
                command_id=command_id,
                error=str(e),
                exc_info=True
            )
            raise RuntimeError(f"Failed to store command result: {e}") from e

    def store_command_wait_outcome(
        self,
        command_id: str,
        envelope: Dict[str, Any],
        *,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        command_timeout_seconds: Optional[int] = None,
    ) -> str:
        """Persist the parent-wait envelope at ``cmd:outcome:{command_id}`` (#229).

        ``envelope.result`` may be a ``{_redis_result_key}`` pointer. Retrieve
        hydrates it; do not duplicate the large body here.
        """
        return self.store_command_result(
            command_id=command_id,
            result=envelope,
            command_timeout_seconds=command_timeout_seconds,
            command_type=str(envelope.get("command_type") or "unknown"),
            tenant_id=tenant_id,
            motet_id=motet_id,
            kind="outcome",
        )

    def has_command_wait_outcome(
        self,
        command_id: str,
        *,
        tenant_id: Optional[str] = None,
    ) -> bool:
        """True when ``cmd:outcome:{command_id}`` has a stored body."""
        key = self._command_key("outcome", command_id, tenant_id)
        try:
            data, _resolved = self._get_bytes_with_fallback(key, tenant_id)
            return bool(data)
        except Exception:
            return False

    def retrieve_command_wait_outcome(
        self,
        command_id: str,
        *,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> Any:
        """Load the parent-wait envelope and resolve any ``cmd:result`` pointer.

        After the result wake, unary ``motet.do`` and gather/map join both
        call this. Large children store the ADR-0029 body at ``cmd:result``
        and leave ``{_redis_result_key: ...}`` in ``cmd:outcome.result``.
        Hydrate here so waiters never treat the pointer as the envelope.
        """
        key = self._command_key("outcome", command_id, tenant_id)
        envelope = self.retrieve_command_result(
            key, tenant_id=tenant_id, motet_id=motet_id
        )
        return self.hydrate_wait_outcome_envelope(
            envelope,
            tenant_id=tenant_id,
            motet_id=motet_id,
        )

    def hydrate_wait_outcome_envelope(
        self,
        envelope: Any,
        *,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> Any:
        """Replace ``result._redis_result_key`` with the stored command body."""
        if not isinstance(envelope, dict):
            return envelope
        result = envelope.get("result")
        if not isinstance(result, dict):
            return envelope
        redis_key = result.get("_redis_result_key")
        if not isinstance(redis_key, str) or not redis_key.strip():
            return envelope
        redis_key = redis_key.strip()
        try:
            full_result = self.retrieve_command_result(
                redis_key, tenant_id=tenant_id, motet_id=motet_id
            )
        except Exception as retrieve_error:
            logger.error(
                "wait_outcome_result_rehydrate_failed",
                command_id=envelope.get("command_id"),
                outcome_result_key=redis_key,
                tenant_id=tenant_id,
                error=str(retrieve_error),
                error_type=type(retrieve_error).__name__,
                exc_info=True,
            )
            raise ValueError(
                f"Wait outcome pointed at missing command result: {redis_key}"
            ) from retrieve_error
        return {
            **envelope,
            "result": full_result,
            "result_retrieved_from_redis": True,
        }
    
    def retrieve_command_result(self, key: str, tenant_id: Optional[str] = None, motet_id: Optional[str] = None) -> Any:
        """
        Retrieve command result from Redis.
        
        Args:
            key: Redis key to retrieve result from
            tenant_id: Optional tenant ID for decryption (required if data is encrypted)
            motet_id: Optional motet ID for decryption (required if data is encrypted, will be read from metadata if not provided)
            
        Returns:
            Command result
            
        Raises:
            ValueError: If result not found or expired
        """
        start_time = time.time()
        try:
            # Retrieve from Redis
            redis_start = time.time()
            data, key = self._get_bytes_with_fallback(key, tenant_id)
            redis_time = (time.time() - redis_start) * 1000  # Convert to ms
            
            if not data:
                raise ValueError(f"Command result not found or expired: {key}")
            data_bytes = cast(bytes, data)

            # Deserialize MsgPack data
            deserialize_start = time.time()
            storage_data = msgpack.unpackb(data_bytes, raw=False)
            deserialize_time = (time.time() - deserialize_start) * 1000  # Convert to ms
            
            metadata = storage_data.get("metadata", {}) or {}
            
            extract_start = time.time()
            # Pass motet_id through - _decrypt_storage_payload handles resolution from metadata if needed
            decrypt_result = self._decrypt_storage_payload(
                storage_data,
                tenant_id,
                metadata.get("command_id", key),
                context=EncryptionContext.COMMAND_RESULT.value,
                payload_field="result",
                motet_id=motet_id
            )
            result = decrypt_result["payload"]
            metadata = decrypt_result["metadata"] or metadata
            decrypt_time = decrypt_result["decryption_time_ms"]
            is_encrypted = decrypt_result["encrypted"]
            tenant_id = decrypt_result["tenant_id"]
            extract_time = 0 if is_encrypted else (time.time() - extract_start) * 1000
            
            total_time = (time.time() - start_time) * 1000  # Convert to ms
            
            logger.info(
                "Retrieved command result from Redis",
                key=key,
                command_id=metadata.get("command_id"),
                result_type=metadata.get("result_type"),
                data_size_bytes=len(data_bytes),
                timing_ms={
                    "total": round(total_time, 2),
                    "redis_operation": round(redis_time, 2),
                    "deserialization": round(deserialize_time, 2),
                    "data_extraction": round(extract_time, 2),
                    "decryption": round(decrypt_time, 2)
                },
                encrypted=is_encrypted,
                tenant_id=tenant_id
            )
            
            return result
            
        except Exception as e:
            logger.error(
                "Failed to retrieve command result from Redis",
                key=key,
                error=str(e),
                exc_info=True
            )
            raise ValueError(f"Command result not found or expired: {key}") from e
    
    def cleanup_expired_data(self, pattern: str = "cmd:*") -> int:
        """
        Clean up expired command data and results.
        
        Args:
            pattern: Redis key pattern to clean up
            
        Returns:
            Number of keys cleaned up
        """
        try:
            keys_to_delete = []
            
            # Scan for keys matching pattern
            for key in self.redis.scan_iter(match=pattern):
                # Check if key exists (not expired)
                if self.redis.exists(key):
                    # Get TTL to check if it's close to expiration
                    ttl = cast(int, self.redis.ttl(key))
                    if ttl < 60:  # Less than 1 minute left
                        keys_to_delete.append(key)
            
            # Delete expired keys
            if keys_to_delete:
                deleted_count = cast(int, self.redis.delete(*keys_to_delete))
                logger.info(
                    "Cleaned up expired command data",
                    deleted_count=deleted_count,
                    pattern=pattern
                )
                return deleted_count
            
            return 0
            
        except Exception as e:
            logger.error(
                "Failed to cleanup expired command data",
                pattern=pattern,
                error=str(e),
                exc_info=True
            )
            return 0
    
    def get_storage_stats(self) -> Dict[str, Any]:
        """
        Get storage statistics for command data.
        
        Returns:
            Dictionary with storage statistics
        """
        try:
            stats = {
                "total_command_data_keys": 0,
                "total_command_result_keys": 0,
                "total_storage_bytes": 0,
                "oldest_data_age_seconds": 0,
                "newest_data_age_seconds": 0
            }
            
            oldest_timestamp = None
            newest_timestamp = None
            
            # Scan for command data keys
            for key in self.redis.scan_iter(match="cmd:data:*"):
                if self.redis.exists(key):
                    stats["total_command_data_keys"] += 1
                    
                    # Get data size
                    data = self.redis.get(key)
                    if data:
                        blob = cast(bytes, data)
                        stats["total_storage_bytes"] += len(blob)
                        
                        # Parse metadata for timestamps
                        try:
                            storage_data = msgpack.unpackb(blob, raw=False)
                            metadata = storage_data.get("metadata", {})
                            stored_at = metadata.get("stored_at")
                            if stored_at:
                                stored_time = datetime.fromisoformat(stored_at)
                                if oldest_timestamp is None or stored_time < oldest_timestamp:
                                    oldest_timestamp = stored_time
                                if newest_timestamp is None or stored_time > newest_timestamp:
                                    newest_timestamp = stored_time
                        except Exception:
                            pass  # Skip if metadata parsing fails
            
            # Scan for command result keys
            for key in self.redis.scan_iter(match="cmd:result:*"):
                if self.redis.exists(key):
                    stats["total_command_result_keys"] += 1
                    
                    # Get data size
                    data = self.redis.get(key)
                    if data:
                        stats["total_storage_bytes"] += len(cast(bytes, data))
            
            # Calculate age statistics
            if oldest_timestamp and newest_timestamp:
                now = datetime.utcnow()
                stats["oldest_data_age_seconds"] = int((now - oldest_timestamp).total_seconds())
                stats["newest_data_age_seconds"] = int((now - newest_timestamp).total_seconds())
            
            return stats
            
        except Exception as e:
            logger.error(
                "Failed to get storage statistics",
                error=str(e),
                exc_info=True
            )
            return {}
    
    def set_debug_mode(self, enabled: bool) -> None:
        """
        Enable or disable debug mode for extended TTL.
        
        Args:
            enabled: Whether to enable debug mode
        """
        self._debug_mode = enabled
        logger.info("Debug mode changed", enabled=enabled)
    
    def store_command_metadata(
        self, 
        command_id: str,
        command_type: str,
        task_id: str,
        tenant_id: str,
        motet_id: str,
        principal_id: str = "",
        conversation_id: str = "",
        parent_command_id: Optional[str] = None,
        triggered_by: Optional[str] = None,
        worker_id: Optional[str] = None,
        status: str = "created",
        duration_ms: Optional[int] = None,
        **additional_metadata
    ) -> str:
        """
        Store command metadata for debugging and flow tracking.
        
        Args:
            command_id: Unique command identifier
            command_type: Type of command
            task_id: Task ID this command belongs to
            conversation_id: Conversation ID
            parent_command_id: Parent command that triggered this one
            triggered_by: What triggered this command
            worker_id: Worker that executed this command
            status: Command status
            duration_ms: Command execution duration in milliseconds
            **additional_metadata: Additional metadata to store
            
        Returns:
            Redis key where metadata was stored
        """
        try:
            metadata = {
                "command_id": command_id,
                "command_type": command_type,
                "task_id": task_id,
                "conversation_id": conversation_id,
                "created_at": datetime.utcnow().isoformat(),
                "parent_command_id": parent_command_id,
                "triggered_by": triggered_by,
                "worker_id": worker_id,
                "status": status,
                "duration_ms": duration_ms,
                "tenant_id": tenant_id,
                "motet_id": motet_id,
                "principal_id": principal_id,
                **additional_metadata
            }
            
            key = self._command_key("meta", command_id, tenant_id)
            
            # Use extended TTL for debug mode
            ttl = DEBUG_COMMAND_DATA_TTL if self._debug_mode else self.default_ttl
            
            tenant_id_s = (tenant_id or "").strip()
            motet_id_s = (motet_id or "").strip()
            if not tenant_id_s:
                raise ValueError("tenant_id is required for cmd:meta encryption")
            if not motet_id_s:
                raise ValueError("motet_id is required for cmd:meta encryption")
            if not self._enable_encryption or not self._encryption_service:
                raise RuntimeError("Encryption is required for cmd:meta storage but encryption service is not available")

            # Keep minimal routing/ops fields plaintext; encrypt the rest into `_envelope`.
            plaintext: Dict[str, Any] = {
                k: v for k, v in metadata.items() if k in _CMD_META_PLAINTEXT_KEYS and v is not None
            }
            sensitive: Dict[str, Any] = {
                k: v for k, v in metadata.items() if k not in _CMD_META_PLAINTEXT_KEYS and v is not None
            }

            envelope = envelope_encrypt_bytes(
                payload_bytes=json.dumps(sensitive, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8", errors="ignore"),
                tenant_id=tenant_id_s,
                encryption_service=self._encryption_service,
                context=EncryptionContext.CMD_META.value,
                aad=compute_cmd_meta_aad(command_id=command_id, tenant_id=tenant_id_s, motet_id=motet_id_s),
            ).envelope

            mapping: Dict[str, Any] = {k: str(v) for k, v in plaintext.items() if v is not None}
            mapping["_envelope"] = json_dumps_compact(envelope)

            self.redis.hset(key, mapping=mapping)
            self.redis.expire(key, ttl)
            
            logger.info(
                "Stored command metadata",
                command_id=command_id,
                command_type=command_type,
                task_id=task_id,
                ttl_seconds=ttl
            )
            
            return key
            
        except Exception as e:
            logger.error(
                "Failed to store command metadata",
                command_id=command_id,
                error=str(e),
                exc_info=True
            )
            raise RuntimeError(f"Failed to store command metadata: {e}") from e
    
    def update_command_metadata(
        self,
        command_id: str,
        **updates
    ) -> None:
        """
        Update existing command metadata.
        
        Args:
            command_id: Command ID to update
            **updates: Fields to update
        """
        try:
            tenant_hint = str(updates.get("tenant_id") or "").strip()
            key, existing = self._resolve_cmd_meta_key(command_id, tenant_hint)
            if existing:
                normalized_probe = normalize_redis_str_mapping(cast(Dict[Any, Any], existing))
                tenant_hint = tenant_hint or str(normalized_probe.get("tenant_id") or "").strip()
                if tenant_hint:
                    key = self._command_key("meta", command_id, tenant_hint)
            if not existing:
                logger.warning("Command metadata not found for update", command_id=command_id)
                return
            
            normalized = normalize_redis_str_mapping(cast(Dict[Any, Any], existing))

            envelope_json = normalized.get("_envelope") or ""
            if not envelope_json:
                raise RuntimeError("cmd:meta missing _envelope field")
            if not self._enable_encryption or not self._encryption_service:
                raise RuntimeError("Encryption is required for cmd:meta updates but encryption service is not available")

            # Decrypt current sensitive payload
            envelope = json_loads(str(envelope_json))
            sensitive_current = json_loads(
                envelope_decrypt_bytes(
                    envelope=envelope,
                    encryption_service=self._encryption_service,
                    context=EncryptionContext.CMD_META.value,
                    aad=compute_cmd_meta_aad(
                        command_id=command_id,
                        tenant_id=(normalized.get("tenant_id") or "").strip(),
                        motet_id=(normalized.get("motet_id") or "").strip(),
                    ),
                ).plaintext.decode("utf-8", errors="ignore")
            )
            if not isinstance(sensitive_current, dict):
                sensitive_current = {}

            # Apply updates: safe keys stay plaintext; everything else goes to encrypted payload.
            plaintext_updates: Dict[str, Any] = {}
            sensitive_updates: Dict[str, Any] = {}
            for k, v in updates.items():
                if v is None:
                    continue
                if k in _CMD_META_PLAINTEXT_UPDATE_KEYS:
                    plaintext_updates[k] = v
                else:
                    sensitive_updates[k] = v

            sensitive_current.update(sensitive_updates)
            tenant_id = (plaintext_updates.get("tenant_id") or normalized.get("tenant_id") or "").strip()
            if not tenant_id:
                raise ValueError("tenant_id is required for cmd:meta encryption (update)")

            new_envelope = envelope_encrypt_bytes(
                payload_bytes=json.dumps(sensitive_current, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8", errors="ignore"),
                tenant_id=tenant_id,
                encryption_service=self._encryption_service,
                context=EncryptionContext.CMD_META.value,
                aad=compute_cmd_meta_aad(
                    command_id=command_id,
                    tenant_id=tenant_id,
                    motet_id=(plaintext_updates.get("motet_id") or normalized.get("motet_id") or "").strip(),
                ),
            ).envelope

            mapping: Dict[str, Any] = {k: str(v) for k, v in plaintext_updates.items()}
            mapping["_envelope"] = json_dumps_compact(new_envelope)

            ttl = cast(int, self.redis.ttl(key))  # Preserve existing TTL
            self.redis.hset(key, mapping=mapping)
            if ttl and int(ttl) > 0:
                self.redis.expire(key, int(ttl))
            else:
                ttl = DEBUG_COMMAND_DATA_TTL if self._debug_mode else self.default_ttl
                self.redis.expire(key, ttl)
            
            logger.info(
                "Updated command metadata",
                command_id=command_id,
                updates=list(updates.keys())
            )
            
        except Exception as e:
            logger.error(
                "Failed to update command metadata",
                command_id=command_id,
                error=str(e),
                exc_info=True
            )
            raise RuntimeError(f"Failed to update command metadata: {e}") from e



# Global instance for easy access
_redis_command_data_manager = None


def get_redis_command_data_manager() -> RedisCommandDataManager:
    """Get global Redis command data manager instance."""
    global _redis_command_data_manager
    if _redis_command_data_manager is None:
        _redis_command_data_manager = RedisCommandDataManager()
    return _redis_command_data_manager
