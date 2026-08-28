"""
Tests for Vault cache envelope encryption helpers.
"""

import json
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from motet.core.security.vault_service import DistributedVaultService
from motet.core.security.envelope_helper import (
    EnvelopeEncryptResult,
    EnvelopeDecryptResult,
)


@pytest.fixture
def vault_service():
    """Provision a vault service with Redis clients mocked out."""
    with patch("motet.core.security.vault_service.get_sync_redis_client") as mock_sync:
        mock_sync.return_value = Mock()
        service = DistributedVaultService()
        service.sync_redis_client = Mock()
        service._envelope_provider = object()
        yield service


def test_write_cache_entry_stores_envelope(vault_service):
    payload = {
        "credential_data": {"api_key": "secret"},
        "expires_at": datetime.utcnow().isoformat(),
        "cached_at": datetime.utcnow().isoformat(),
    }
    envelope = {
        "encrypted": True,
        "encryption_mode": "envelope-v1",
        "encryption": {
            "encrypted_data": "ciphertext",
            "iv": "iv",
            "tenant_id": "tenant-123",
            "encryption_version": "aes-256-gcm-v1",
        },
        "dek": {
            "wrapped_key": "wrapped",
            "iv": "iv",
            "tenant_id": "tenant-123",
            "encryption_version": "aes-256-gcm-v1",
        },
    }

    with patch("motet.core.security.vault_service.envelope_encrypt_bytes") as mock_encrypt, \
         patch("motet.core.security.vault_service.store_structured_data_sync") as mock_store:
        mock_encrypt.return_value = EnvelopeEncryptResult(
            envelope=envelope,
            encryption_time_ms=1.2,
            dek_wrap_time_ms=0.4,
        )

        vault_service._write_cache_entry("cache:key", payload, "tenant-123")

        mock_store.assert_called_once()
        stored_data = mock_store.call_args[0][2]
        assert "_envelope" in stored_data
        assert stored_data["_envelope"]["encryption"]["tenant_id"] == "tenant-123"


def test_decrypt_cache_entry_returns_plain_payload(vault_service):
    payload = {
        "credential_data": {"api_key": "cached"},
        "expires_at": None,
        "cached_at": datetime.utcnow().isoformat(),
    }
    envelope_holder = {"_envelope": {"mock": "value"}}

    with patch("motet.core.security.vault_service.envelope_decrypt_bytes") as mock_decrypt:
        mock_decrypt.return_value = EnvelopeDecryptResult(
            plaintext=json.dumps(payload).encode("utf-8"),
            tenant_id="tenant-123",
            decryption_time_ms=0.9,
            dek_unwrap_time_ms=0.2,
        )

        result = vault_service._decrypt_cache_entry(envelope_holder, "cache:key")

        assert result == payload
        mock_decrypt.assert_called_once()

