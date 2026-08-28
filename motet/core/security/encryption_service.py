"""
Motet - Encryption Service for Redis Data at Rest

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Application-level encryption service for Redis data at rest.
    Uses AES-256-GCM with tenant-specific keys stored in Vault.
    Implements Phase 1B of Tenant-Specific Encryption.

    local worker support: when MOTET_VAULT_RESOLVE_URL is set the
    service fetches tenant KEKs via the cloud vault HTTPS endpoint instead of
    requiring a local DistributedVaultService (which needs the master key).
    All AES-256-GCM crypto remains local; only KEK retrieval is proxied.

Dependencies:
    - cryptography: AES-GCM encryption primitives
    - motet.core.security.vault_service: Vault service for key management (cloud mode)
    - motet.core.edge.http_vault_client: HTTP vault client (local worker mode)
    - base64: Encoding/decoding of binary data
    - os: Random IV generation

Usage:
    from motet.core.security.encryption_service import get_encryption_service

    svc = get_encryption_service()
    encrypted_blob = svc.encrypt(data_bytes, tenant_id="tenant-123")
    plaintext_bytes = svc.decrypt(encrypted_blob)

Notes:
    - Uses AES-256-GCM for authenticated encryption
    - Tenant keys are stored in Vault with CRITICAL security level
    - Keys are cached in memory for performance
    - Cloud mode: generates a tenant KEK only when no vault row exists for that
      tenant under the known key names. A retrieve miss when a row is already
      present fails instead of minting a second key.
    - Unwrap tries the current tenant KEK, then ``encryption:tenant:{tid}:previous``
      so ciphertext written before a rotation still opens.
    - Tenant KEK vault rows store ``tenant_id`` so rewrite/prefix can classify
      them as tenant-scoped (``encryption:tenant:{tid}``), not platform secrets
    - Local worker mode: KEK generation is disabled; KEKs are
      fetched from the cloud vault resolve endpoint over the WireGuard tunnel
    - Implements Phase 1B of
"""


import os
import base64
import json
import structlog
import hashlib
from typing import Dict, Any, Optional
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.backends import default_backend

from .vault_service import (
    DistributedVaultService,
    CredentialType,
    CredentialScope,
    CredentialSecurityLevel,
    CredentialAccessRequest
)

logger = structlog.get_logger(__name__)

_LOGGED_MASTER_KEY_FINGERPRINT = False


def _safe_redis_exists(client: Any, key: str) -> bool:
    """True only for a real Redis EXISTS hit (int/bool). Mocks stay False."""
    try:
        found = client.exists(key)
    except Exception:
        return False
    return isinstance(found, (int, bool)) and bool(found)


def _tenant_kek_lookup_keys(tenant_id: str) -> tuple[str, ...]:
    """Physical vault keys that may hold this tenant's KEK metadata or ciphertext."""
    from motet.core.distributed.tenant_keys import vault_read_key_candidates

    keys: list[str] = []
    seen: set[str] = set()
    for logical in (
        f"vault:metadata:encryption:tenant:{tenant_id}",
        f"vault:credential:encryption:tenant:{tenant_id}",
    ):
        for key in vault_read_key_candidates(logical, tenant_id):
            if key in seen:
                continue
            seen.add(key)
            keys.append(key)
    return tuple(keys)


def _log_master_key_fingerprint() -> None:
    """Log a hashed fingerprint of the active vault master key."""
    global _LOGGED_MASTER_KEY_FINGERPRINT
    if _LOGGED_MASTER_KEY_FINGERPRINT:
        return
    if os.getenv("MOTET_VAULT_MASTER_KEY_DEBUG", "true").lower() != "true":
        return

    master_key = os.getenv("MOTET_VAULT_MASTER_KEY")
    if master_key:
        fingerprint = hashlib.sha256(master_key.encode("utf-8")).hexdigest()
        logger.info(
            "vault_master_key_fingerprint",
            key_present=True,
            fingerprint=fingerprint,
        )
    else:
        logger.warning("vault_master_key_missing", key_present=False)

    _LOGGED_MASTER_KEY_FINGERPRINT = True


class EncryptionError(Exception):
    """Exception raised for encryption/decryption errors."""
    pass


class EncryptionService:
    """
    Application-level encryption service for Redis data.
    Uses AES-256-GCM with tenant-specific keys from vault.
    
    Implements Phase 1B of ADR-0056: Tenant-Specific Encryption.

    ADR-0095: when MOTET_VAULT_RESOLVE_URL is set the service operates in
    *local worker mode* — tenant KEKs are fetched via the cloud vault HTTPS
    endpoint and the master key is never required locally.
    """
    
    def __init__(self, vault_service: Optional[DistributedVaultService] = None):
        """
        Initialize encryption service.
        
        Args:
            vault_service: Vault service instance (creates new if not provided).
                           Ignored in local worker mode (ADR-0095).
        """
        self._local_mode = bool(os.getenv("MOTET_VAULT_RESOLVE_URL", "").strip())

        if self._local_mode:
            from motet.core.edge.http_vault_client import HttpVaultClient
            self._http_vault: Optional["HttpVaultClient"] = HttpVaultClient()
            self.vault: Optional[DistributedVaultService] = None  # type: ignore[assignment]
            logger.info("encryption_service_local_mode",
                        resolve_url=os.getenv("MOTET_VAULT_RESOLVE_URL"))
        else:
            self._http_vault = None
            if vault_service is None:
                self.vault = DistributedVaultService()
            else:
                self.vault = vault_service
            _log_master_key_fingerprint()
        
        self._key_cache: Dict[str, bytes] = {}
        self._previous_key_cache: Dict[str, Optional[bytes]] = {}
        self._system_principal_id = "system"
    
    def get_tenant_key(self, tenant_id: str) -> bytes:
        """
        Get tenant encryption key from vault (with caching).

        ADR-0095: in local worker mode the KEK is fetched from the cloud vault
        resolve endpoint over the WireGuard tunnel instead of directly from
        Valkey (which would require the master key).
        
        Args:
            tenant_id: Tenant identifier
            
        Returns:
            Encryption key as bytes (32 bytes for AES-256)
            
        Raises:
            EncryptionError: If key retrieval or generation fails
        """
        if tenant_id in self._key_cache:
            logger.debug("Tenant key retrieved from cache", tenant_id=tenant_id)
            return self._key_cache[tenant_id]

        if self._local_mode:
            return self._get_tenant_key_http(tenant_id)

        return self._get_tenant_key_vault(tenant_id)

    def _get_tenant_key_http(self, tenant_id: str) -> bytes:
        """Fetch tenant KEK via the cloud vault resolve endpoint (ADR-0095)."""
        assert self._http_vault is not None

        _SYSTEM_TENANTS = {"discovery-tenant", "default", "default-tenant"}
        allowed_tenant = os.getenv("MOTET_EDGE_TENANT_ID", "")
        if allowed_tenant and tenant_id != allowed_tenant and tenant_id not in _SYSTEM_TENANTS:
            raise EncryptionError(
                f"Local worker may only access its own tenant KEK "
                f"(requested={tenant_id}, allowed={allowed_tenant})"
            )

        credential_key = f"encryption:tenant:{tenant_id}"
        try:
            credential_data = self._http_vault._resolve(credential_key)
            if not credential_data:
                raise EncryptionError(
                    f"Tenant KEK not found via vault resolve (tenant may not "
                    f"be provisioned on the cloud): {tenant_id}"
                )
            key_b64 = credential_data.get("key")
            if not key_b64:
                raise EncryptionError(f"Tenant key missing 'key' field: {tenant_id}")

            key = base64.b64decode(key_b64)
            if len(key) != 32:
                raise EncryptionError(
                    f"Invalid tenant key length: {len(key)} bytes (expected 32)"
                )

            self._key_cache[tenant_id] = key
            logger.info("tenant_kek_retrieved_via_http",
                        tenant_id=tenant_id)
            return key

        except EncryptionError:
            raise
        except Exception as e:
            logger.error("Failed to get tenant KEK via HTTP",
                         tenant_id=tenant_id,
                         error=str(e),
                         exc_info=True)
            raise EncryptionError(
                f"Failed to get tenant encryption key via HTTP: {e}"
            ) from e

    def _tenant_kek_row_exists(self, tenant_id: str) -> bool:
        """True when a KEK metadata or credential hash already exists for this tenant."""
        vault = self.vault
        client = getattr(vault, "sync_redis_client", None) if vault is not None else None
        if client is None:
            return False
        return any(_safe_redis_exists(client, key) for key in _tenant_kek_lookup_keys(tenant_id))

    def _get_tenant_key_vault(self, tenant_id: str) -> bytes:
        """Fetch tenant KEK from DistributedVaultService (cloud mode)."""
        assert self.vault is not None
        try:
            credential_key = f"encryption:tenant:{tenant_id}"

            request = CredentialAccessRequest(
                principal_id=self._system_principal_id,
                credential_key=credential_key,
                tenant_id=tenant_id,
            )

            response = self.vault.retrieve_credential(request)

            if response.success and response.credential_data:
                key_b64 = response.credential_data.get("key")
                if not key_b64:
                    raise EncryptionError(f"Tenant key missing 'key' field: {tenant_id}")

                key = base64.b64decode(key_b64)

                if len(key) != 32:
                    raise EncryptionError(f"Invalid tenant key length: {len(key)} bytes (expected 32)")

                self._key_cache[tenant_id] = key

                logger.info("Tenant encryption key retrieved from vault", tenant_id=tenant_id)
                return key
            if self._tenant_kek_row_exists(tenant_id):
                raise EncryptionError(
                    f"Tenant KEK vault row exists but could not be retrieved "
                    f"for {tenant_id}: {getattr(response, 'error_message', None)}"
                )
            logger.info("Generating new tenant encryption key", tenant_id=tenant_id)
            return self._generate_and_store_tenant_key(tenant_id)
                
        except Exception as e:
            logger.error("Failed to get tenant encryption key",
                        tenant_id=tenant_id,
                        error=str(e),
                        exc_info=True)
            raise EncryptionError(f"Failed to get tenant encryption key: {e}") from e
    
    def _generate_and_store_tenant_key(self, tenant_id: str) -> bytes:
        """
        Generate a new tenant encryption key and store it in vault.
        
        Args:
            tenant_id: Tenant identifier
            
        Returns:
            Encryption key as bytes (32 bytes for AES-256)
            
        Raises:
            EncryptionError: If key generation or storage fails, or if called
                in local worker mode (ADR-0095 — KEK generation is cloud-only).
        """
        if self._local_mode:
            raise EncryptionError(
                f"Cannot generate tenant KEK on a local worker (ADR-0095). "
                f"Tenant '{tenant_id}' must be provisioned on the cloud first."
            )

        assert self.vault is not None
        try:
            if self._tenant_kek_row_exists(tenant_id):
                raise EncryptionError(
                    f"Refusing to overwrite an existing tenant KEK for {tenant_id}"
                )
            # Generate new AES-256 key (32 bytes)
            key = AESGCM.generate_key(bit_length=256)
            
            # Encode key as base64 for storage
            key_b64 = base64.b64encode(key).decode('utf-8')
            
            # Store in vault
            credential_id = f"encryption:tenant:{tenant_id}"
            success = self.vault.store_credential(
                credential_id=credential_id,
                credential_data={"key": key_b64},
                credential_type=CredentialType.CUSTOM,
                scope=CredentialScope.GLOBAL,  # Global scope for system access
                security_level=CredentialSecurityLevel.SECRET,  # Highest security level
                principal_id=self._system_principal_id,
                tenant_id=tenant_id,
                description=f"Encryption key for tenant {tenant_id} (AES-256-GCM)"
            )
            
            if not success:
                raise EncryptionError(f"Failed to store tenant encryption key in vault: {tenant_id}")
            
            # Cache for performance
            self._key_cache[tenant_id] = key
            
            logger.info("Generated and stored new tenant encryption key", tenant_id=tenant_id)
            return key
            
        except Exception as e:
            logger.error("Failed to generate and store tenant encryption key",
                        tenant_id=tenant_id,
                        error=str(e),
                        exc_info=True)
            raise EncryptionError(f"Failed to generate tenant encryption key: {e}") from e
    
    def encrypt(self, data: bytes, tenant_id: str) -> Dict[str, Any]:
        """
        Encrypt data with tenant key using AES-256-GCM.
        
        Args:
            data: Plaintext data to encrypt (bytes)
            tenant_id: Tenant identifier for key selection
            
        Returns:
            Dictionary with encrypted_data, iv, tenant_id, and encryption_version
            Format matches ADR-0056 Phase 1B specification
            
        Raises:
            EncryptionError: If encryption fails
        """
        try:
            # Get tenant encryption key
            key = self.get_tenant_key(tenant_id)
            
            # Create AES-GCM cipher
            aesgcm = AESGCM(key)
            
            # Generate random IV (nonce) - 96 bits (12 bytes) for GCM
            iv = os.urandom(12)
            
            # Encrypt and authenticate
            # Associated data is None (no additional authenticated data)
            encrypted_data = aesgcm.encrypt(iv, data, None)
            
            # Return encrypted blob as dict (for Redis hash storage)
            encrypted_blob = {
                "encrypted_data": base64.b64encode(encrypted_data).decode('utf-8'),
                "iv": base64.b64encode(iv).decode('utf-8'),
                "tenant_id": tenant_id,
                "encryption_version": "aes-256-gcm-v1"
            }
            
            logger.debug("Data encrypted successfully",
                        tenant_id=tenant_id,
                        data_size_bytes=len(data),
                        encrypted_size_bytes=len(encrypted_data))
            
            return encrypted_blob
            
        except Exception as e:
            logger.error("Failed to encrypt data",
                        tenant_id=tenant_id,
                        error=str(e),
                        exc_info=True)
            raise EncryptionError(f"Encryption failed: {e}") from e
    
    def decrypt(self, encrypted_blob: Dict[str, Any]) -> bytes:
        """
        Decrypt data with tenant key.
        
        Args:
            encrypted_blob: Dictionary with encrypted_data, iv, tenant_id, encryption_version
            
        Returns:
            Plaintext data as bytes
            
        Raises:
            EncryptionError: If decryption fails (invalid key, corrupted data, etc.)
        """
        try:
            # Extract fields from encrypted blob
            tenant_id = encrypted_blob.get("tenant_id")
            if not tenant_id:
                raise EncryptionError("Missing tenant_id in encrypted blob")
            
            encryption_version = encrypted_blob.get("encryption_version", "aes-256-gcm-v1")
            if encryption_version != "aes-256-gcm-v1":
                raise EncryptionError(f"Unsupported encryption version: {encryption_version}")
            
            encrypted_data_b64 = encrypted_blob.get("encrypted_data")
            iv_b64 = encrypted_blob.get("iv")
            
            if not encrypted_data_b64 or not iv_b64:
                raise EncryptionError("Missing encrypted_data or iv in encrypted blob")
            
            # Decode base64
            encrypted_data = base64.b64decode(encrypted_data_b64)
            iv = base64.b64decode(iv_b64)
            
            # Get tenant encryption key
            key = self.get_tenant_key(tenant_id)
            
            # Create AES-GCM cipher
            aesgcm = AESGCM(key)
            
            # Decrypt and verify authentication tag
            # Associated data is None (no additional authenticated data)
            plaintext = aesgcm.decrypt(iv, encrypted_data, None)
            
            logger.debug("Data decrypted successfully",
                        tenant_id=tenant_id,
                        plaintext_size_bytes=len(plaintext))
            
            return plaintext
            
        except Exception as e:
            logger.error("Failed to decrypt data",
                        tenant_id=encrypted_blob.get("tenant_id"),
                        error=str(e),
                        exc_info=True)
            raise EncryptionError(f"Decryption failed: {e}") from e
    
    def wrap_key(self, dek: bytes, tenant_id: str) -> Dict[str, Any]:
        """
        Wrap (encrypt) a data encryption key (DEK) with tenant key encryption key (KEK).
        
        Used for envelope encryption (Phase 3). The DEK is encrypted with the tenant's KEK
        and stored alongside the encrypted data. This allows key rotation without re-encrypting
        all data - only the DEKs need to be re-encrypted.
        
        Args:
            dek: Data encryption key to wrap (typically 32 bytes for AES-256)
            tenant_id: Tenant identifier for KEK selection
            
        Returns:
            Dictionary with wrapped_key, iv, tenant_id, and encryption_version
            Format: {wrapped_key: base64, iv: base64, tenant_id: str, encryption_version: str}
            
        Raises:
            EncryptionError: If key wrapping fails
        """
        try:
            # Get tenant KEK (key encryption key)
            kek = self.get_tenant_key(tenant_id)
            kek_fingerprint = hashlib.sha256(kek).hexdigest()
            
            # Create AES-GCM cipher with KEK
            aesgcm = AESGCM(kek)
            
            # Generate random IV for wrapping
            iv = os.urandom(12)
            
            # Encrypt the DEK with the KEK
            wrapped_dek = aesgcm.encrypt(iv, dek, None)
            
            wrapped_blob = {
                "wrapped_key": base64.b64encode(wrapped_dek).decode('utf-8'),
                "iv": base64.b64encode(iv).decode('utf-8'),
                "tenant_id": tenant_id,
                "encryption_version": "aes-256-gcm-v1",
                # ADR-0056 hardening: include KEK identity metadata to support rotation/audit.
                # Note: Vault-backed KEK versioning is not yet implemented; this fingerprint
                # provides a stable identifier for the KEK material in use.
                "kek_id": f"encryption:tenant:{tenant_id}",
                "kek_fingerprint_sha256": kek_fingerprint,
            }
            
            logger.debug("DEK wrapped successfully",
                        tenant_id=tenant_id,
                        dek_size_bytes=len(dek))
            
            return wrapped_blob
            
        except Exception as e:
            logger.error("Failed to wrap key",
                        tenant_id=tenant_id,
                        error=str(e),
                        exc_info=True)
            raise EncryptionError(f"Key wrapping failed: {e}") from e
    
    def _previous_tenant_keys(self, tenant_id: str) -> list[bytes]:
        """KEKs stored as ``encryption:tenant:{tid}:previous`` after a rotation."""
        if tenant_id in self._previous_key_cache:
            prev = self._previous_key_cache[tenant_id]
            return [prev] if prev else []
        if self._local_mode or self.vault is None:
            self._previous_key_cache[tenant_id] = None
            return []
        try:
            response = self.vault.retrieve_credential(
                CredentialAccessRequest(
                    principal_id=self._system_principal_id,
                    credential_key=f"encryption:tenant:{tenant_id}:previous",
                    tenant_id=tenant_id,
                )
            )
        except Exception as exc:
            logger.warning(
                "previous_tenant_kek_retrieve_failed",
                tenant_id=tenant_id,
                error=str(exc),
            )
            self._previous_key_cache[tenant_id] = None
            return []
        if not (response.success and response.credential_data):
            self._previous_key_cache[tenant_id] = None
            return []
        key_b64 = response.credential_data.get("key")
        if not key_b64:
            self._previous_key_cache[tenant_id] = None
            return []
        key = base64.b64decode(key_b64)
        if len(key) != 32:
            self._previous_key_cache[tenant_id] = None
            return []
        self._previous_key_cache[tenant_id] = key
        logger.info("previous_tenant_kek_retrieved", tenant_id=tenant_id)
        return [key]

    def _unwrap_keks(self, tenant_id: str, expected_fpr: Optional[str]) -> list[bytes]:
        """Current KEK plus any previous KEK, fingerprint match first."""
        seen: set[bytes] = set()
        ordered: list[bytes] = []
        for kek in [self.get_tenant_key(tenant_id), *self._previous_tenant_keys(tenant_id)]:
            if kek in seen:
                continue
            seen.add(kek)
            ordered.append(kek)
        if not expected_fpr:
            return ordered
        matched = [k for k in ordered if hashlib.sha256(k).hexdigest() == expected_fpr]
        unmatched = [k for k in ordered if k not in matched]
        return matched + unmatched

    def unwrap_key(self, wrapped_blob: Dict[str, Any]) -> bytes:
        """
        Unwrap (decrypt) a data encryption key (DEK) using tenant key encryption key (KEK).
        
        Used for envelope encryption (Phase 3). Decrypts the wrapped DEK so it can be used
        to decrypt the actual data. Tries the current tenant KEK, then a stored previous
        KEK when fingerprints differ after rotation.
        
        Args:
            wrapped_blob: Dictionary with wrapped_key, iv, tenant_id, encryption_version
            
        Returns:
            Unwrapped DEK as bytes
            
        Raises:
            EncryptionError: If key unwrapping fails
        """
        try:
            tenant_id = wrapped_blob.get("tenant_id")
            if not tenant_id:
                raise EncryptionError("Missing tenant_id in wrapped key blob")
            
            encryption_version = wrapped_blob.get("encryption_version", "aes-256-gcm-v1")
            if encryption_version != "aes-256-gcm-v1":
                raise EncryptionError(f"Unsupported encryption version: {encryption_version}")
            
            wrapped_key_b64 = wrapped_blob.get("wrapped_key")
            iv_b64 = wrapped_blob.get("iv")
            
            if not wrapped_key_b64 or not iv_b64:
                raise EncryptionError("Missing wrapped_key or iv in wrapped blob")
            
            wrapped_dek = base64.b64decode(wrapped_key_b64)
            iv = base64.b64decode(iv_b64)
            expected_fpr = wrapped_blob.get("kek_fingerprint_sha256")
            last_error: Optional[Exception] = None
            for kek in self._unwrap_keks(tenant_id, expected_fpr if isinstance(expected_fpr, str) else None):
                try:
                    dek = AESGCM(kek).decrypt(iv, wrapped_dek, None)
                    logger.debug(
                        "DEK unwrapped successfully",
                        tenant_id=tenant_id,
                        dek_size_bytes=len(dek),
                    )
                    return dek
                except Exception as exc:
                    last_error = exc
            raise EncryptionError(f"Key unwrapping failed: {last_error}") from last_error
            
        except EncryptionError:
            raise
        except Exception as e:
            logger.error("Failed to unwrap key",
                        tenant_id=wrapped_blob.get("tenant_id"),
                        error=str(e),
                        exc_info=True)
            raise EncryptionError(f"Key unwrapping failed: {e}") from e
    
    def clear_key_cache(self, tenant_id: Optional[str] = None) -> None:
        """
        Clear key cache (for testing or key rotation).
        
        Args:
            tenant_id: Specific tenant to clear (None = clear all)
        """
        if tenant_id:
            if tenant_id in self._key_cache:
                del self._key_cache[tenant_id]
                logger.debug("Cleared key cache for tenant", tenant_id=tenant_id)
            self._previous_key_cache.pop(tenant_id, None)
        else:
            self._key_cache.clear()
            self._previous_key_cache.clear()
            logger.debug("Cleared all key caches")


# Global encryption service instance
_encryption_service: Optional[EncryptionService] = None


def get_encryption_service(vault_service: Optional[DistributedVaultService] = None) -> EncryptionService:
    """
    Get global encryption service instance.
    
    Args:
        vault_service: Optional vault service instance (creates new if not provided)
        
    Returns:
        EncryptionService instance
    """
    global _encryption_service
    
    if _encryption_service is None:
        _encryption_service = EncryptionService(vault_service)
    
    return _encryption_service


__all__ = [
    'EncryptionService',
    'EncryptionError',
    'get_encryption_service'
]

