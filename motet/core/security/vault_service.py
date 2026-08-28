"""
Motet - Vault Service

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Vault Service for the Motet distributed framework.
    Provides secure credential storage and retrieval with encryption,
    multi-tenant isolation, and distributed lock management.

Dependencies:
    - typing: Type hints and annotations
    - structlog: Structured logging
    - cryptography: Encryption for credential data
    - redis: Distributed lock and state management
    - pydantic: Data validation and models

Usage:
    from motet.core.security.vault_service import DistributedVaultService
    
    vault = DistributedVaultService()
    success = vault.store_credential(
        credential_id="api_key_123",
        credential_data={"key": "secret_value"},
        credential_type=CredentialType.API_KEY,
        scope=CredentialScope.PRINCIPAL,
        security_level=CredentialSecurityLevel.CONFIDENTIAL,
        principal_id="user123",
        tenant_id="org456"
    )

Notes:
    - Thread-safe: Uses Redis for distributed lock tracking
    - Multi-tenant: Supports global, tenant, and principal-scoped credentials
    - Encrypted: All credentials encrypted at rest
    - Distributed: Locks tracked in Redis for cross-process cleanup
    - Integrates with distributed architecture and event system
    - Vault key resolve uses the tenant key, ``motet:vault:locate:{id}``
      → tenant, ``motet:vault:…`` for platform rows, then leftover names
      from ``vault_read_key_candidates`` (``None:vault:…``, unprefixed
      ``vault:…``, ``imf:vault:…``). Hash ``tenant_id`` of ``None`` is not
      a tenant; those writes use ``motet:vault:…``.
    - List reads ``{tid}:vault:index`` / ``motet:vault:index`` (SET of ids).
      Store and delete update that SET. Backfill leftovers with
      ``scripts/backfill_valkey_vault_index.py``.
    - Store and delete SCAN cache hashes for that credential id (not KEYS).
    - Retrieve updates metadata access fields and ``vault:audit`` lists.
"""


import json
import os
import signal
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set, cast
from pydantic import BaseModel, Field
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64
import structlog

from ..distributed.redis_manager import (
    get_sync_redis_client,
    store_structured_data_sync, retrieve_structured_data_sync,
    create_distributed_lock, acquire_distributed_lock_sync,
)
from ..distributed.tenant_keys import (
    VAULT_INDEX_LOGICAL,
    delete_candidate_keys,
    first_existing_key,
    is_platform_vault_logical_key,
    is_usable_tenant_id,
    product_key,
    tenant_key,
    vault_index_key,
    vault_read_key_candidates,
)
from ..config import Config
from .envelope_helper import envelope_encrypt_bytes, envelope_decrypt_bytes

logger = structlog.get_logger(__name__)

# Redis key for tracking active vault locks (thread-safe, distributed)
_ACTIVE_LOCKS_SET_KEY = "vault:active_locks"
_ACTIVE_LOCKS_SET_TTL = 3600  # 1 hour TTL for the set itself (auto-cleanup of stale entries)

# Global process instance ID (unique per VaultService instance, even if PID is reused)
# Format: PID:UUID (e.g., "12345:550e8400-e29b-41d4-a716-446655440000")
_PROCESS_INSTANCE_ID: Optional[str] = None

def _get_process_instance_id() -> str:
    """
    Get or create a unique process instance ID.
    
    This is more unique than just PID because:
    - PID can be reused after process dies
    - UUID ensures uniqueness even if PID is reused
    - Format: PID:UUID for readability and debugging
    
    Returns:
        Unique instance ID string (e.g., "12345:550e8400-e29b-41d4-a716-446655440000")
    """
    global _PROCESS_INSTANCE_ID
    if _PROCESS_INSTANCE_ID is None:
        _PROCESS_INSTANCE_ID = f"{os.getpid()}:{uuid.uuid4()}"
        logger.info("Generated process instance ID for vault locks",
                   instance_id=_PROCESS_INSTANCE_ID)
    return _PROCESS_INSTANCE_ID

def _register_lock_in_redis(lock_key: str) -> None:
    """
    Register a lock key in Redis set for tracking (thread-safe, distributed).
    
    Args:
        lock_key: The lock key to register (e.g., "lock:vault:store:credential_id")
    """
    try:
        sync_client = get_sync_redis_client("vault_service")
        sync_client.sadd(_ACTIVE_LOCKS_SET_KEY, lock_key)
        # Set TTL on the set itself (refresh on each add)
        sync_client.expire(_ACTIVE_LOCKS_SET_KEY, _ACTIVE_LOCKS_SET_TTL)
    except Exception as e:
        logger.warning("Failed to register lock in Redis",
                      lock_key=lock_key,
                      error=str(e))

def _unregister_lock_from_redis(lock_key: str) -> None:
    """
    Unregister a lock key from Redis set (thread-safe, distributed).
    
    Args:
        lock_key: The lock key to unregister
    """
    try:
        sync_client = get_sync_redis_client("vault_service")
        sync_client.srem(_ACTIVE_LOCKS_SET_KEY, lock_key)
    except Exception as e:
        logger.warning("Failed to unregister lock from Redis",
                      lock_key=lock_key,
                      error=str(e))

def _vault_signal_handler(signum, frame):
    """
    Signal handler to clean up vault locks on process shutdown.
    
    Reads active locks from Redis and releases only locks owned by this process instance.
    Uses process instance ID (PID:UUID) instead of just PID to avoid conflicts with
    PID reuse or multiple threads/greenlets in the same process.
    
    This ensures locks are released even if the process crashes or is killed,
    without interfering with locks from other processes or process instances.
    """
    current_instance_id = _get_process_instance_id()
    logger.info("Received shutdown signal, cleaning up vault locks",
               signal=signum,
               instance_id=current_instance_id)
    
    try:
        sync_client = get_sync_redis_client("vault_service")
        
        # Get all active lock keys from Redis set
        lock_keys = cast(Set[bytes], sync_client.smembers(_ACTIVE_LOCKS_SET_KEY))
        
        if not lock_keys:
            logger.info("No active vault locks to clean up")
            return
        
        logger.info("Found active vault locks in Redis",
                   total_locks=len(lock_keys),
                   instance_id=current_instance_id)
        
        # Track locks we successfully release (to remove from set)
        released_locks = []
        
        # Release only locks owned by this process instance
        for raw_key in lock_keys:
            lock_key = raw_key.decode('utf-8') if isinstance(raw_key, bytes) else str(raw_key)
            try:
                # Get the lock_value from Redis (stored as value of lock_key)
                # lock_value is either:
                # - Old format: PID (e.g., "12345") - for backward compatibility
                # - New format: PID:UUID (e.g., "12345:550e8400-e29b-41d4-a716-446655440000")
                raw_lock_value = sync_client.get(lock_key)
                
                if not raw_lock_value:
                    # Lock already expired or released
                    logger.debug("Lock already expired or released",
                               lock_key=lock_key)
                    # Remove from set since it's gone
                    sync_client.srem(_ACTIVE_LOCKS_SET_KEY, lock_key)
                    continue
                
                # Ensure lock_value is a string (Redis may return bytes in some configs)
                lock_value: str = raw_lock_value.decode('utf-8') if isinstance(raw_lock_value, bytes) else str(raw_lock_value)
                
                # Only release locks owned by this process instance
                # Support both old format (PID only) and new format (PID:UUID)
                # For old format, check if PID matches (backward compatibility)
                # For new format, check exact match (proper isolation)
                current_pid = str(os.getpid())
                is_our_lock = (
                    lock_value == current_instance_id or  # New format: exact match
                    (lock_value == current_pid and ":" not in lock_value)  # Old format: PID match (backward compat)
                )
                
                if not is_our_lock:
                    logger.debug("Skipping lock owned by different process instance",
                               lock_key=lock_key,
                               lock_owner=lock_value,
                               current_instance_id=current_instance_id)
                    continue
                
                # Recreate DistributedLock object with lock_value
                # The lock_value is needed for proper release (atomic check-and-delete)
                lock = create_distributed_lock("vault_service", lock_key, ttl_seconds=30)
                lock.lock_value = lock_value  # Set the lock_value for release
                lock._acquired = True  # Mark as acquired so release will work
                
                # Release the lock
                if lock.release_sync():
                    logger.info("Released vault lock during shutdown",
                              lock_key=lock_key,
                              instance_id=current_instance_id)
                    released_locks.append(lock_key)
                else:
                    logger.warning("Failed to release vault lock (may have been released by another process)",
                                 lock_key=lock_key,
                                 instance_id=current_instance_id)
                    # Still remove from set since we tried to release it
                    released_locks.append(lock_key)
                
            except Exception as e:
                logger.error("Failed to release vault lock during shutdown",
                           lock_key=lock_key,
                           instance_id=current_instance_id,
                           error=str(e),
                           exc_info=True)
        
        # Remove only our released locks from the Redis set (not the entire set!)
        # This preserves locks from other processes
        if released_locks:
            sync_client.srem(_ACTIVE_LOCKS_SET_KEY, *released_locks)
            logger.info("Removed released locks from Redis set",
                       released_count=len(released_locks),
                       instance_id=current_instance_id)
        else:
            logger.info("No locks owned by this process instance to clean up",
                       instance_id=current_instance_id)
        
    except Exception as e:
        logger.error("Failed to clean up vault locks during shutdown",
                    error=str(e),
                    exc_info=True)

# Register signal handlers for vault lock cleanup (only in main thread)
try:
    signal.signal(signal.SIGTERM, _vault_signal_handler)
    signal.signal(signal.SIGINT, _vault_signal_handler)
except ValueError:
    # Signal handlers can only be registered in the main thread
    # This is expected in Celery worker processes
    pass


class CredentialType(str, Enum):
    """Types of credentials supported by the vault."""
    API_KEY = "api_key"
    BEARER_TOKEN = "bearer_token"
    OAUTH_TOKEN = "oauth_token"
    DATABASE_PASSWORD = "database_password"
    SSH_KEY = "ssh_key"
    CERTIFICATE = "certificate"
    SERVICE_ACCOUNT_KEY = "service_account_key"
    CUSTOM = "custom"


class CredentialScope(str, Enum):
    """Scopes for credential access control."""
    GLOBAL = "global"           # Organization-wide
    MOTET = "motet"           # Motet-specific
    TENANT = "tenant"           # Tenant-specific
    PRINCIPAL = "principal"     # Principal-specific


class CredentialSecurityLevel(str, Enum):
    """Security levels for credential classification."""
    PUBLIC = "public"           # No encryption needed
    INTERNAL = "internal"       # Basic encryption
    CONFIDENTIAL = "confidential"  # Strong encryption
    SECRET = "secret"           # Maximum encryption + audit
    TOP_SECRET = "top_secret"   # Maximum encryption + audit + access logging


class CredentialMetadata(BaseModel):
    """Metadata for stored credentials."""
    credential_id: str
    credential_type: CredentialType
    scope: CredentialScope
    security_level: CredentialSecurityLevel
    principal_id: str
    tenant_id: Optional[str] = None
    motet_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: str = ""
    expires_at: Optional[datetime] = None
    last_accessed_at: Optional[datetime] = None
    access_count: int = 0
    tags: List[str] = Field(default_factory=list)
    description: str = ""


class CredentialAccessRequest(BaseModel):
    """Request for credential access."""
    principal_id: str
    tenant_id: Optional[str] = None
    motet_id: Optional[str] = None
    credential_key: str = ""
    required_scopes: List[CredentialScope] = Field(default_factory=list)
    required_security_level: Optional[CredentialSecurityLevel] = None


class CredentialAccessResponse(BaseModel):
    """Response from credential access request."""
    success: bool
    credential_data: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    access_granted_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class VaultEncryption:
    """Handles encryption/decryption of credential data."""
    
    def __init__(self, master_key: Optional[str] = None):
        self.master_key = master_key or self._get_master_key()
        self._fernet = self._create_fernet()
    
    def _get_master_key(self) -> str:
        """Get master encryption key from environment or generate one."""
        key = os.getenv('MOTET_VAULT_MASTER_KEY')
        if not key:
            allow_ephemeral = os.getenv("MOTET_ALLOW_EPHEMERAL_MASTER_KEY", "false").lower() == "true"
            test_mode = os.getenv("MOTET_TEST_MODE", "false").lower() == "true"
            if not allow_ephemeral and not test_mode:
                logger.error("Missing MOTET_VAULT_MASTER_KEY (refusing to auto-generate)")
                raise RuntimeError(
                    "MOTET_VAULT_MASTER_KEY is required. Set MOTET_ALLOW_EPHEMERAL_MASTER_KEY=true "
                    "to allow auto-generation in non-production environments."
                )
            # Generate a new key for non-production/test environments only
            key = Fernet.generate_key().decode()
            logger.warning(
                "Generated new vault master key - set MOTET_VAULT_MASTER_KEY for production",
                allow_ephemeral=allow_ephemeral,
                test_mode=test_mode,
            )
        return key
    
    def _create_fernet(self) -> Fernet:
        """Create Fernet encryption instance."""
        if isinstance(self.master_key, str):
            key_bytes = self.master_key.encode()
            salt_hex = os.getenv("MOTET_VAULT_SALT")
            if salt_hex:
                salt = bytes.fromhex(salt_hex)
            else:
                salt = b"imf_vault_salt"
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=100000,
            )
            key = base64.urlsafe_b64encode(kdf.derive(key_bytes))
        else:
            key = self.master_key
        
        return Fernet(key)
    
    def encrypt_credential(self, data: Dict[str, Any]) -> str:
        """Encrypt credential data."""
        try:
            json_data = json.dumps(data)
            encrypted_data = self._fernet.encrypt(json_data.encode())
            return base64.urlsafe_b64encode(encrypted_data).decode()
        except Exception as e:
            logger.error("Failed to encrypt credential", error=str(e))
            raise RuntimeError(f"Credential encryption failed: {e}")
    
    def decrypt_credential(self, encrypted_data: str) -> Dict[str, Any]:
        """Decrypt credential data."""
        try:
            encrypted_bytes = base64.urlsafe_b64decode(encrypted_data.encode())
            decrypted_data = self._fernet.decrypt(encrypted_bytes)
            return json.loads(decrypted_data.decode())
        except Exception as e:
            logger.error("Failed to decrypt credential", error=str(e))
            raise RuntimeError(f"Credential decryption failed: {e}")


class DistributedVaultService:
    """
    Distributed vault service for secure credential management.
    
    Features:
    - Multi-tenant credential isolation
    - Principal-based access control
    - Motet-scoped credentials
    - High-performance caching
    - Audit logging
    - Encryption at rest
    """
    
    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self.sync_redis_client = get_sync_redis_client("vault_service")
        self.encryption = VaultEncryption()
        self._cache_ttl = 300  # 5 minutes default cache TTL
        self._lock_ttl = 30    # 30 seconds for distributed locks
        self._envelope_provider = None
        
        # Tenant-scoped logical prefixes (stored ``{tid}:vault:…``).
        # Platform writes use ``motet:vault:…`` via ``_scoped_vault_key``.
        self.credential_prefix = "vault:credential"
        self.metadata_prefix = "vault:metadata"
        self.cache_prefix = "vault:cache"
        self.audit_prefix = "vault:audit"
    
    def _make_credential_logical(self, credential_id: str) -> str:
        return f"{self.credential_prefix}:{credential_id}"

    def _scoped_vault_key(self, logical: str, tenant_id: Optional[str] = None) -> str:
        if is_usable_tenant_id(tenant_id):
            return tenant_key(str(tenant_id).strip(), logical)
        return product_key(logical)

    def _make_credential_key(self, credential_id: str, tenant_id: Optional[str] = None) -> str:
        """Generate Redis key for credential data."""
        return self._scoped_vault_key(self._make_credential_logical(credential_id), tenant_id)

    def _make_metadata_logical(self, credential_id: str) -> str:
        return f"{self.metadata_prefix}:{credential_id}"

    def _make_metadata_key(self, credential_id: str, tenant_id: Optional[str] = None) -> str:
        """Generate Redis key for credential metadata."""
        return self._scoped_vault_key(self._make_metadata_logical(credential_id), tenant_id)

    def _locate_key(self, credential_id: str) -> str:
        return product_key(f"vault:locate:{credential_id}")

    def _credential_id_from_logical(self, logical: str) -> str:
        for prefix in (f"{self.metadata_prefix}:", f"{self.credential_prefix}:"):
            if logical.startswith(prefix):
                return logical[len(prefix):]
        return logical

    def _locator_tenant(self, credential_id: str) -> Optional[str]:
        raw = self.sync_redis_client.get(self._locate_key(credential_id))
        if isinstance(raw, (bytes, bytearray)):
            text = raw.decode("utf-8")
        elif isinstance(raw, str):
            text = raw
        else:
            return None
        text = text.strip()
        if not text or text in ("None", "null"):
            return None
        return text

    def _write_locate(self, credential_id: str, tenant_id: Optional[str]) -> None:
        if not is_usable_tenant_id(tenant_id):
            return
        self.sync_redis_client.set(self._locate_key(credential_id), str(tenant_id).strip())

    def _index_add(self, credential_id: str, tenant_id: Optional[str]) -> None:
        cid = (credential_id or "").strip()
        if not cid:
            return
        self.sync_redis_client.sadd(vault_index_key(tenant_id), cid)

    def _index_remove(self, credential_id: str, tenant_id: Optional[str]) -> None:
        cid = (credential_id or "").strip()
        if not cid:
            return
        self.sync_redis_client.srem(vault_index_key(tenant_id), cid)

    def _decode_member(self, raw: Any) -> str:
        if isinstance(raw, (bytes, bytearray)):
            return raw.decode("utf-8", errors="replace")
        return str(raw)

    def _smembers_ids(self, index_key: str) -> List[str]:
        raw = self.sync_redis_client.smembers(index_key) or []
        return [m for m in (self._decode_member(item) for item in raw) if m]

    def _scan_index_keys(self) -> List[str]:
        """Index SET keys only (one per tenant plus platform). SCAN, not KEYS."""
        seen: set[str] = set()
        out: List[str] = []
        suffix = f":{VAULT_INDEX_LOGICAL}"
        for pattern in (f"*:{VAULT_INDEX_LOGICAL}", VAULT_INDEX_LOGICAL):
            for raw in self.sync_redis_client.scan_iter(match=pattern, count=100) or []:
                key = self._decode_member(raw)
                if key != VAULT_INDEX_LOGICAL and not key.endswith(suffix):
                    continue
                if key in seen:
                    continue
                seen.add(key)
                out.append(key)
        return out

    def _tenant_from_index_key(self, index_key: str) -> Optional[str]:
        if index_key in (VAULT_INDEX_LOGICAL, product_key(VAULT_INDEX_LOGICAL)):
            return None
        tenant, sep, rest = index_key.partition(":")
        if sep and rest == VAULT_INDEX_LOGICAL and is_usable_tenant_id(tenant):
            return tenant
        return None

    def _iter_indexed_credentials(
        self, tenant_id: Optional[str]
    ) -> List[tuple[str, Optional[str]]]:
        """
        Credential id + index tenant for list.

        Known tenant: that tenant's SET plus the platform SET.
        No tenant / admin: SCAN index keys, then SMEMBERS each.
        """
        pairs: List[tuple[str, Optional[str]]] = []
        seen: set[tuple[str, Optional[str]]] = set()
        if is_usable_tenant_id(tenant_id):
            scoped: List[tuple[str, Optional[str]]] = [
                (vault_index_key(tenant_id), str(tenant_id).strip()),
                (vault_index_key(None), None),
            ]
        else:
            scoped = [
                (key, self._tenant_from_index_key(key)) for key in self._scan_index_keys()
            ]
        for index_key, idx_tenant in scoped:
            for credential_id in self._smembers_ids(index_key):
                item = (credential_id, idx_tenant)
                if item in seen:
                    continue
                seen.add(item)
                pairs.append(item)
        return pairs

    def _resolve_vault_key(self, logical: str, tenant_id: Optional[str] = None) -> str:
        product = product_key(logical)
        if is_platform_vault_logical_key(logical) or is_platform_vault_logical_key(product):
            found = first_existing_key(self.sync_redis_client, product)
            return found or product

        ordered: List[str] = []
        if is_usable_tenant_id(tenant_id):
            ordered.append(tenant_key(str(tenant_id).strip(), logical))
        located = self._locator_tenant(self._credential_id_from_logical(logical))
        if is_usable_tenant_id(located):
            ordered.append(tenant_key(str(located).strip(), logical))
        ordered.extend(vault_read_key_candidates(logical, tenant_id))
        seen: Set[str] = set()
        unique: List[str] = []
        for key in ordered:
            if key in seen:
                continue
            seen.add(key)
            unique.append(key)
        found = first_existing_key(self.sync_redis_client, unique)
        if found:
            return found
        if is_usable_tenant_id(located):
            return tenant_key(str(located).strip(), logical)
        return self._scoped_vault_key(logical, tenant_id)
    
    def _make_cache_key(
        self,
        principal_id: str,
        credential_key: str,
        *,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None,
    ) -> str:
        """Generate Redis key for credential cache.

        Includes tenant/motet from the *access request* so cache entries cannot be
        reused across tenants or motets (authorization must not be bypassed on cache hit).
        """
        key = f"{self.cache_prefix}:{principal_id}:{credential_key}"
        if tenant_id:
            key = f"{key}:t:{tenant_id}"
        if motet_id:
            key = f"{key}:m:{motet_id}"
        return self._scoped_vault_key(key, tenant_id)
    
    def _make_audit_key(self, credential_id: str, tenant_id: Optional[str] = None) -> str:
        """Generate Redis key for audit logs."""
        return self._scoped_vault_key(f"{self.audit_prefix}:{credential_id}", tenant_id)

    def _clear_credential_cache(self, credential_id: str) -> int:
        """Delete cache hashes for *credential_id* (legacy and tenant-prefixed)."""
        cache_keys: List[Any] = []
        seen: set[str] = set()
        prefixes = (
            self.cache_prefix,
            product_key(self.cache_prefix),
        )
        seen_patterns: set[str] = set()
        for prefix in prefixes:
            for pattern in (
                f"{prefix}:*:{credential_id}*",
                f"*:{prefix}:*:{credential_id}*",
            ):
                if pattern in seen_patterns:
                    continue
                seen_patterns.add(pattern)
                for raw in self.sync_redis_client.scan_iter(match=pattern, count=100) or []:
                    key = self._decode_member(raw)
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    cache_keys.append(raw)
        if cache_keys:
            self.sync_redis_client.delete(*cache_keys)
        return len(cache_keys)

    def _get_envelope_provider(self):
        """Lazily initialize the envelope encryption provider to avoid circular imports."""
        if self._envelope_provider is None:
            from .encryption_service import get_encryption_service  # Local import to avoid circular dependency

            self._envelope_provider = get_encryption_service()
        return self._envelope_provider

    def _resolve_cache_tenant_id(
        self,
        request: 'CredentialAccessRequest',
        metadata: Dict[str, Any],
    ) -> Optional[str]:
        """Resolve tenant_id for cache encryption."""
        if request.tenant_id:
            return request.tenant_id
        tenant_id = metadata.get("tenant_id")
        return tenant_id

    def _write_cache_entry(self, cache_key: str, payload: Dict[str, Any], tenant_id: str) -> None:
        """Encrypt and persist a cache entry."""
        try:
            from .encryption_contexts import EncryptionContext
            from .json_helpers import json_dumps_compact_bytes

            provider = self._get_envelope_provider()
            payload_bytes = json_dumps_compact_bytes(payload)
            encrypt_result = envelope_encrypt_bytes(
                payload_bytes,
                tenant_id,
                provider,  # type: ignore[arg-type]  # EncryptionService satisfies EnvelopeKeyProvider at runtime
                context=EncryptionContext.VAULT_CACHE.value,
            )
            envelope = {
                **encrypt_result.envelope,
                "schema_version": "vault-cache-envelope-v1",
                "encryption": {**encrypt_result.envelope["encryption"]},
            }
            envelope["encryption"]["encryption_time_ms"] = round(encrypt_result.encryption_time_ms, 2)
            envelope["encryption"]["dek_wrap_time_ms"] = round(encrypt_result.dek_wrap_time_ms, 2)

            store_structured_data_sync(
                "vault_service",
                cache_key,
                {"_envelope": envelope},
                format_type="hash",
            )
            self.sync_redis_client.expire(cache_key, self._cache_ttl)
        except Exception as exc:
            logger.error(
                "vault_cache_encryption_failed",
                cache_key=cache_key,
                tenant_id=tenant_id,
                error=str(exc),
            )

    def _decrypt_cache_entry(self, cache_data: Dict[str, Any], cache_key: str) -> Optional[Dict[str, Any]]:
        """Decrypt a cached credential payload."""
        envelope = cache_data.get("_envelope")
        if not envelope:
            logger.warning("vault_cache_missing_envelope", cache_key=cache_key)
            self.sync_redis_client.delete(cache_key)
            return None

        try:
            from .encryption_contexts import EncryptionContext
            from .json_helpers import json_loads

            provider = self._get_envelope_provider()
            decrypt_result = envelope_decrypt_bytes(
                envelope,
                provider,  # type: ignore[arg-type]  # EncryptionService satisfies EnvelopeKeyProvider at runtime
                context=EncryptionContext.VAULT_CACHE.value,
            )
            payload = json_loads(decrypt_result.plaintext.decode("utf-8", errors="ignore"))
            return payload
        except Exception as exc:
            logger.error(
                "vault_cache_decryption_failed",
                cache_key=cache_key,
                error=str(exc),
            )
            self.sync_redis_client.delete(cache_key)
            return None
    
    def store_credential(
        self,
        credential_id: str,
        credential_data: Dict[str, Any],
        credential_type: CredentialType,
        scope: CredentialScope,
        security_level: CredentialSecurityLevel,
        principal_id: str,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        tags: Optional[List[str]] = None,
        description: str = ""
    ) -> bool:
        """
        Store a credential in the vault.
        
        Args:
            credential_id: Unique identifier for the credential
            credential_data: The actual credential data to store
            credential_type: Type of credential
            scope: Access scope for the credential
            security_level: Security classification level
            principal_id: ID of the principal storing the credential
            tenant_id: Optional tenant ID for tenant-scoped credentials
            motet_id: Optional motet ID for motet-scoped credentials
            expires_at: Optional expiration time
            tags: Optional tags for categorization
            description: Optional description
            
        Returns:
            True if credential was stored successfully
        """
        try:
            # Create metadata
            metadata = CredentialMetadata(
                credential_id=credential_id,
                credential_type=credential_type,
                scope=scope,
                security_level=security_level,
                principal_id=principal_id,
                tenant_id=tenant_id,
                motet_id=motet_id,
                created_by=principal_id,
                expires_at=expires_at,
                tags=tags or [],
                description=description
            )
            
            # Encrypt credential data
            encrypted_data = self.encryption.encrypt_credential(credential_data)
            
            # Use distributed lock to ensure atomic storage
            lock_key = f"lock:vault:store:{credential_id}"
            lock = acquire_distributed_lock_sync(
                "vault_service", 
                lock_key, 
                lock_value=_get_process_instance_id(),  # Use unique instance ID instead of PID
                ttl_seconds=self._lock_ttl
            )
            
            if not lock:
                logger.error("Failed to acquire lock for credential storage", credential_id=credential_id)
                return False
            
            # Register lock in Redis for cleanup on shutdown (thread-safe, distributed)
            _register_lock_in_redis(lock_key)
            
            try:
                # Store encrypted credential data
                store_structured_data_sync(
                    "vault_service",
                    self._make_credential_key(credential_id, tenant_id),
                    {"encrypted_data": encrypted_data},
                    format_type="hash"
                )
                
                # Store metadata (convert enums to their values and handle None values)
                metadata_dict = metadata.__dict__.copy()
                metadata_dict["scope"] = metadata.scope.value
                metadata_dict["credential_type"] = metadata.credential_type.value
                metadata_dict["security_level"] = metadata.security_level.value
                
                # Handle None values for datetime fields
                if metadata_dict.get("expires_at") is None:
                    metadata_dict["expires_at"] = None
                elif hasattr(metadata_dict["expires_at"], 'isoformat'):
                    metadata_dict["expires_at"] = metadata_dict["expires_at"].isoformat()
                
                store_structured_data_sync(
                    "vault_service",
                    self._make_metadata_key(credential_id, tenant_id),
                    metadata_dict,
                    format_type="hash"
                )
                self._write_locate(credential_id, tenant_id)
                self._index_add(credential_id, tenant_id)
                
                cleared = self._clear_credential_cache(credential_id)
                logger.debug(
                    "Cleared cache for credential",
                    credential_id=credential_id,
                    principal_id=principal_id,
                    cleared=cleared,
                )
                
                # Set TTL if credential expires
                if expires_at:
                    ttl_seconds = int((expires_at - datetime.utcnow()).total_seconds())
                    if ttl_seconds > 0:
                        self.sync_redis_client.expire(self._make_credential_key(credential_id, tenant_id), ttl_seconds)
                        self.sync_redis_client.expire(self._make_metadata_key(credential_id, tenant_id), ttl_seconds)
                
                logger.info("Credential stored successfully",
                           credential_id=credential_id,
                           credential_type=credential_type.value if hasattr(credential_type, 'value') else credential_type,
                           scope=scope.value if hasattr(scope, 'value') else scope,
                           security_level=security_level.value if hasattr(security_level, 'value') else security_level,
                           principal_id=principal_id)
                
                return True
                
            finally:
                # Unregister lock from Redis and release it
                _unregister_lock_from_redis(lock_key)
                lock.release_sync()
                
        except Exception as e:
            logger.error("Failed to store credential",
                        credential_id=credential_id,
                        error=str(e),
                        exc_info=True)
            return False
    
    def retrieve_credential(
        self,
        request: CredentialAccessRequest
    ) -> CredentialAccessResponse:
        """
        Retrieve a credential from the vault.
        
        Args:
            request: Credential access request with principal and scope information
            
        Returns:
            CredentialAccessResponse with credential data or error
        """
        try:
            # Check cache first.
            # Skip the vault cache for encryption:tenant:* credentials to avoid
            # circular recursion (see write path comment below for full call chain).
            cache_key = self._make_cache_key(
                request.principal_id,
                request.credential_key,
                tenant_id=request.tenant_id,
                motet_id=request.motet_id,
            )
            _skip_vault_cache = request.credential_key.startswith("encryption:tenant:")
            cached_data = (
                None
                if _skip_vault_cache
                else retrieve_structured_data_sync("vault_service", cache_key, format_type="hash")
            )
            
            if cached_data:
                cache_payload = self._decrypt_cache_entry(cached_data, cache_key)
                if cache_payload:
                    logger.info("Credential retrieved from cache",
                               principal_id=request.principal_id,
                               credential_key=request.credential_key)
                    
                    expires_at = None
                    expires_at_str = cache_payload.get("expires_at")
                    if expires_at_str and expires_at_str != "None":
                        expires_at = datetime.fromisoformat(expires_at_str)
                    if expires_at and datetime.utcnow() > expires_at:
                        try:
                            self.sync_redis_client.delete(cache_key)
                        except Exception:
                            pass
                        # Fall through to storage so we return a proper "expired" response
                    else:
                        return CredentialAccessResponse(
                            success=True,
                            credential_data=cache_payload.get("credential_data"),
                            access_granted_at=datetime.utcnow(),
                            expires_at=expires_at
                        )
            
            # Retrieve from storage
            metadata_key = self._resolve_vault_key(
                self._make_metadata_logical(request.credential_key), request.tenant_id
            )
            metadata_data = retrieve_structured_data_sync("vault_service", metadata_key, format_type="hash")
            
            if not metadata_data:
                return CredentialAccessResponse(
                    success=False,
                    error_message=f"Credential not found: {request.credential_key}"
                )
            
            # Check authorization
            if not self._check_authorization(request, metadata_data):
                return CredentialAccessResponse(
                    success=False,
                    error_message="Access denied: insufficient permissions"
                )
            
            # Check expiration
            expires_at_str = metadata_data.get("expires_at")
            if expires_at_str and expires_at_str != "None":
                try:
                    expires_at = datetime.fromisoformat(expires_at_str)
                    if datetime.utcnow() > expires_at:
                        return CredentialAccessResponse(
                            success=False,
                            error_message="Credential has expired"
                        )
                except (ValueError, TypeError):
                    pass
            
            # Retrieve and decrypt credential data
            credential_key = self._resolve_vault_key(
                self._make_credential_logical(request.credential_key), request.tenant_id
            )
            credential_data = retrieve_structured_data_sync("vault_service", credential_key, format_type="hash")
            
            if not credential_data:
                return CredentialAccessResponse(
                    success=False,
                    error_message="Credential data not found"
                )
            
            # Decrypt credential data
            encrypted_data = credential_data.get("encrypted_data")
            if not encrypted_data:
                return CredentialAccessResponse(
                    success=False,
                    error_message="Invalid credential format"
                )
            
            decrypted_data = self.encryption.decrypt_credential(encrypted_data)
            
            # Update access tracking
            self._update_access_tracking(request, metadata_data)
            
            # Cache the credential (enveloped).
            # Skip caching for encryption:tenant:* keys to avoid infinite recursion:
            # EncryptionService.get_tenant_key → vault.retrieve_credential →
            # _write_cache_entry → envelope_encrypt_bytes → wrap_key →
            # get_tenant_key (key not yet in _key_cache) → retrieve_credential → ∞
            # These keys are already cached in EncryptionService._key_cache.
            _skip_cache = request.credential_key.startswith("encryption:tenant:")
            if not _skip_cache:
                cache_payload = {
                    "credential_data": decrypted_data,
                    "expires_at": metadata_data.get("expires_at"),
                    "cached_at": datetime.utcnow().isoformat()
                }
                cache_tenant_id = self._resolve_cache_tenant_id(request, metadata_data)
                if cache_tenant_id:
                    self._write_cache_entry(cache_key, cache_payload, cache_tenant_id)
                else:
                    logger.warning(
                        "vault_cache_skipped_missing_tenant",
                        principal_id=request.principal_id,
                        credential_key=request.credential_key
                    )
            
            logger.info("Credential retrieved successfully",
                       principal_id=request.principal_id,
                       credential_key=request.credential_key,
                       security_level=metadata_data.get("security_level"))
            
            # Handle expires_at properly
            expires_at = None
            expires_at_str = metadata_data.get("expires_at")
            if expires_at_str and expires_at_str != "None":
                expires_at = datetime.fromisoformat(expires_at_str)
            
            return CredentialAccessResponse(
                success=True,
                credential_data=decrypted_data,
                access_granted_at=datetime.utcnow(),
                expires_at=expires_at
            )
            
        except Exception as e:
            logger.error("Failed to retrieve credential",
                        principal_id=request.principal_id,
                        credential_key=request.credential_key,
                        error=str(e),
                        exc_info=True)
            return CredentialAccessResponse(
                success=False,
                error_message=f"Internal error: {str(e)}"
            )
    
    def _clean_metadata_for_parsing(self, metadata_data: Dict[str, Any]) -> Dict[str, Any]:
        """Clean metadata data for Pydantic parsing."""
        cleaned = metadata_data.copy()
        
        # Handle string 'None' values
        if cleaned.get("expires_at") == "None":
            cleaned["expires_at"] = None
        
        if cleaned.get("last_accessed_at") == "None":
            cleaned["last_accessed_at"] = None
        
        # Parse datetime strings
        created_at = cleaned.get("created_at")
        if isinstance(created_at, str):
            try:
                cleaned["created_at"] = datetime.fromisoformat(created_at)
            except (ValueError, TypeError):
                cleaned["created_at"] = None
        
        expires_at = cleaned.get("expires_at")
        if isinstance(expires_at, str) and expires_at != "None":
            try:
                cleaned["expires_at"] = datetime.fromisoformat(expires_at)
            except (ValueError, TypeError):
                cleaned["expires_at"] = None
        
        last_accessed_at = cleaned.get("last_accessed_at")
        if isinstance(last_accessed_at, str) and last_accessed_at != "None":
            try:
                cleaned["last_accessed_at"] = datetime.fromisoformat(last_accessed_at)
            except (ValueError, TypeError):
                cleaned["last_accessed_at"] = None
        
        # Parse tags from JSON string
        tags_raw = cleaned.get("tags")
        if isinstance(tags_raw, str):
            try:
                cleaned["tags"] = json.loads(tags_raw)
            except (json.JSONDecodeError, TypeError):
                cleaned["tags"] = []
        
        # Parse access_count as integer
        if cleaned.get("access_count"):
            try:
                cleaned["access_count"] = int(cleaned["access_count"])
            except (ValueError, TypeError):
                cleaned["access_count"] = 0
        
        return cleaned

    def _check_authorization(
        self,
        request: CredentialAccessRequest,
        metadata: Dict[str, Any]
    ) -> bool:
        """Check if the principal is authorized to access the credential."""
        try:
            # Check scope-based authorization
            credential_scope = CredentialScope(metadata.get("scope", "principal"))
            
            # Global scope - accessible to all
            if credential_scope == CredentialScope.GLOBAL:
                return True
            
            # Principal scope - only accessible to the principal who created it
            if credential_scope == CredentialScope.PRINCIPAL:
                return metadata.get("principal_id") == request.principal_id
            
            # Tenant scope - accessible to principals in the same tenant
            if credential_scope == CredentialScope.TENANT:
                return (metadata.get("tenant_id") == request.tenant_id and
                        request.tenant_id is not None)
            
            # Motet scope - accessible to principals in the same motet
            if credential_scope == CredentialScope.MOTET:
                return (metadata.get("motet_id") == request.motet_id and
                        request.motet_id is not None)
            
            return False
            
        except Exception as e:
            logger.error("Authorization check failed",
                        principal_id=request.principal_id,
                        error=str(e))
            return False
    
    def _update_access_tracking(
        self,
        request: CredentialAccessRequest,
        metadata: Dict[str, Any]
    ) -> None:
        """Update access tracking and audit logs."""
        try:
            # Update metadata with access information
            metadata_key = self._resolve_vault_key(
                self._make_metadata_logical(request.credential_key), request.tenant_id
            )
            metadata["last_accessed_at"] = datetime.utcnow().isoformat()
            metadata["access_count"] = metadata.get("access_count", 0) + 1
            
            store_structured_data_sync("vault_service", metadata_key, metadata, format_type="hash")
            
            # Create audit log entry
            audit_entry = {
                "credential_id": request.credential_key,
                "principal_id": request.principal_id,
                "tenant_id": request.tenant_id,
                "motet_id": request.motet_id,
                "access_time": datetime.utcnow().isoformat(),
                "action": "retrieve",
                "security_level": metadata.get("security_level")
            }
            
            audit_key = self._make_audit_key(request.credential_key)
            self.sync_redis_client.lpush(audit_key, json.dumps(audit_entry))
            self.sync_redis_client.expire(audit_key, 86400 * 30)  # Keep audit logs for 30 days
            
        except Exception as e:
            logger.error("Failed to update access tracking",
                        principal_id=request.principal_id,
                        error=str(e))
    
    def list_credentials(
        self,
        principal_id: str,
        tenant_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        credential_type: Optional[CredentialType] = None,
        include_all: bool = False
    ) -> List[CredentialMetadata]:
        """
        List credentials accessible to the principal.
        
        Args:
            principal_id: Principal ID for authorization checks
            tenant_id: Optional tenant ID filter
            motet_id: Optional motet ID filter
            credential_type: Optional credential type filter
            include_all: If True, bypass authorization checks and return all credentials
                        (useful for ops dashboard/admin views)
        
        Returns:
            List of credential metadata
        """
        try:
            accessible_credentials = []
            for credential_id, idx_tenant in self._iter_indexed_credentials(tenant_id):
                key = self._make_metadata_key(credential_id, idx_tenant)
                metadata_data = retrieve_structured_data_sync("vault_service", key, format_type="hash")
                if not metadata_data:
                    continue
                
                # Skip authorization check if include_all is True
                if not include_all:
                    # Check if principal can access this credential
                    request = CredentialAccessRequest(
                        principal_id=principal_id,
                        tenant_id=tenant_id,
                        motet_id=motet_id,
                        credential_key=metadata_data.get("credential_id", "")
                    )
                    
                    if not self._check_authorization(request, metadata_data):
                        continue
                
                # Apply credential type filter if specified
                if credential_type is not None and metadata_data.get("credential_type") != credential_type.value:
                    continue
                
                # Clean up the metadata data for Pydantic parsing
                cleaned_data = self._clean_metadata_for_parsing(metadata_data)
                accessible_credentials.append(CredentialMetadata(**cleaned_data))
            
            return accessible_credentials
            
        except Exception as e:
            logger.error("Failed to list credentials",
                        principal_id=principal_id,
                        include_all=include_all,
                        error=str(e))
            return []
    
    def delete_credential(
        self,
        credential_id: str,
        principal_id: str,
        tenant_id: Optional[str] = None,
    ) -> bool:
        """Delete a credential from the vault."""
        try:
            # Check if principal has permission to delete
            metadata_key = self._resolve_vault_key(
                self._make_metadata_logical(credential_id), tenant_id
            )
            metadata_data = retrieve_structured_data_sync("vault_service", metadata_key, format_type="hash")
            
            if not metadata_data:
                return False
            
            # Authorization check:
            # - Global credentials (empty stored principal_id) can be deleted by any authenticated user
            # - User-specific credentials can only be deleted by the creator
            stored_principal_id = metadata_data.get("principal_id", "")
            is_global_credential = stored_principal_id == "" or stored_principal_id is None
            is_owner = stored_principal_id == principal_id
            
            if not is_global_credential and not is_owner:
                logger.warning("Unauthorized credential deletion attempt",
                              credential_id=credential_id,
                              principal_id=principal_id,
                              stored_principal_id=stored_principal_id)
                return False
            
            # Use distributed lock for atomic deletion
            lock_key = f"lock:vault:delete:{credential_id}"
            lock = acquire_distributed_lock_sync(
                "vault_service", 
                lock_key, 
                lock_value=_get_process_instance_id(),  # Use unique instance ID instead of PID
                ttl_seconds=self._lock_ttl
            )
            
            if not lock:
                return False
            
            # Register lock in Redis for cleanup on shutdown (thread-safe, distributed)
            _register_lock_in_redis(lock_key)
            
            try:
                # Delete credential data and metadata
                stored_tenant = str(metadata_data.get("tenant_id") or "") or None
                delete_candidate_keys(
                    self.sync_redis_client,
                    tenant_key(stored_tenant, self._make_credential_logical(credential_id))
                    if stored_tenant
                    else self._make_credential_logical(credential_id),
                )
                delete_candidate_keys(
                    self.sync_redis_client,
                    tenant_key(stored_tenant, self._make_metadata_logical(credential_id))
                    if stored_tenant
                    else self._make_metadata_logical(credential_id),
                )
                delete_candidate_keys(
                    self.sync_redis_client,
                    self._locate_key(credential_id),
                )
                index_tenant = stored_tenant if is_usable_tenant_id(stored_tenant) else None
                self._index_remove(credential_id, index_tenant)
                
                self._clear_credential_cache(credential_id)
                
                logger.info("Credential deleted successfully",
                           credential_id=credential_id,
                           principal_id=principal_id)
                
                return True
                
            finally:
                # Unregister lock from Redis and release it
                _unregister_lock_from_redis(lock_key)
                lock.release_sync()
                
        except Exception as e:
            logger.error("Failed to delete credential",
                        credential_id=credential_id,
                        principal_id=principal_id,
                        error=str(e))
            return False


# Convenience functions for easy integration
def store_credential_for_principal(
    credential_id: str,
    credential_data: Dict[str, Any],
    credential_type: CredentialType,
    principal_id: str,
    tenant_id: Optional[str] = None,
    motet_id: Optional[str] = None,
    scope: CredentialScope = CredentialScope.PRINCIPAL,
    security_level: CredentialSecurityLevel = CredentialSecurityLevel.CONFIDENTIAL
) -> bool:
    """Convenience function to store a credential for a principal."""
    vault = DistributedVaultService()
    return vault.store_credential(
        credential_id=credential_id,
        credential_data=credential_data,
        credential_type=credential_type,
        scope=scope,
        security_level=security_level,
        principal_id=principal_id,
        tenant_id=tenant_id,
        motet_id=motet_id
    )


def get_credential_for_principal(
    credential_key: str,
    principal_id: str,
    tenant_id: Optional[str] = None,
    motet_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Convenience function to get a credential for a principal."""
    vault = DistributedVaultService()
    request = CredentialAccessRequest(
        principal_id=principal_id,
        tenant_id=tenant_id,
        motet_id=motet_id,
        credential_key=credential_key
    )
    
    response = vault.retrieve_credential(request)
    return response.credential_data if response.success else None


# Export main classes and functions
__all__ = [
    'DistributedVaultService',
    'CredentialType',
    'CredentialScope', 
    'CredentialSecurityLevel',
    'CredentialMetadata',
    'CredentialAccessRequest',
    'CredentialAccessResponse',
    'store_credential_for_principal',
    'get_credential_for_principal'
]

