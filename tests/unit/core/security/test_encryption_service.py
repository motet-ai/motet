"""
Tests for the Encryption Service (ADR-0056 Phase 1B)

Comprehensive test suite covering encryption/decryption, tenant isolation,
key management, and integration with RedisCommandDataManager.
"""

import pytest
import json
import base64
from typing import Any
from unittest.mock import Mock, patch, MagicMock
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from motet.core.security.encryption_service import (
    EncryptionService,
    EncryptionError,
    get_encryption_service
)
from motet.core.security.vault_service import (
    DistributedVaultService,
    CredentialType,
    CredentialScope,
    CredentialSecurityLevel,
    CredentialAccessRequest,
    CredentialAccessResponse
)


class DummyEncryptionService:
    def __init__(self):
        self.last_payload = None

    def encrypt(self, data, tenant_id=None):
        self.last_payload = data
        return {
            "encrypted_data": base64.b64encode(b"fake").decode("utf-8"),
            "iv": base64.b64encode(b"iv123").decode("utf-8"),
            "tenant_id": tenant_id,
            "encryption_version": "aes-256-gcm-v1"
        }

    def decrypt(self, encrypted_blob):
        return self.last_payload

    def wrap_key(self, dek, tenant_id: str):
        return {
            "wrapped_key": base64.b64encode(dek).decode("utf-8"),
            "iv": base64.b64encode(b"wrapiv123456").decode("utf-8"),
            "tenant_id": tenant_id,
            "encryption_version": "aes-256-gcm-v1"
        }

    def unwrap_key(self, wrapped_blob):
        wrapped_key = wrapped_blob.get("wrapped_key")
        if not wrapped_key:
            raise EncryptionError("Missing wrapped_key")
        return base64.b64decode(wrapped_key)


class TestEncryptionService:
    """Test cases for EncryptionService."""
    
    @pytest.fixture
    def mock_vault_service(self):
        """Create a mock Vault service."""
        vault = Mock(spec=DistributedVaultService)
        vault.retrieve_credential = Mock()
        vault.store_credential = Mock(return_value=True)
        return vault
    
    @pytest.fixture
    def encryption_service(self, mock_vault_service):
        """Create an EncryptionService instance with mock vault."""
        return EncryptionService(vault_service=mock_vault_service)
    
    def test_encryption_service_initialization(self, mock_vault_service):
        """Test encryption service initialization."""
        service = EncryptionService(vault_service=mock_vault_service)
        assert service.vault == mock_vault_service
        assert service._key_cache == {}
        assert service._system_principal_id == "system"
    
    def test_encryption_service_initialization_without_vault(self):
        """Test encryption service creates vault if not provided."""
        with patch('motet.core.security.encryption_service.DistributedVaultService') as mock_vault_class:
            mock_vault = Mock()
            mock_vault_class.return_value = mock_vault
            
            service = EncryptionService()
            assert service.vault == mock_vault
            mock_vault_class.assert_called_once()
    
    def test_get_tenant_key_new_tenant(self, encryption_service, mock_vault_service):
        """Test getting encryption key for a new tenant (generates new key)."""
        tenant_id = "tenant-123"
        
        # Mock vault to return no existing credential (new tenant)
        mock_vault_service.retrieve_credential.return_value = CredentialAccessResponse(
            success=False,
            error_message="Credential not found"
        )
        
        # Mock key generation
        with patch('motet.core.security.encryption_service.AESGCM.generate_key') as mock_generate:
            mock_key = b'\x01' * 32  # 32 bytes for AES-256
            mock_generate.return_value = mock_key
            
            key = encryption_service.get_tenant_key(tenant_id)
            
            # Verify key is correct length
            assert len(key) == 32
            assert key == mock_key
            
            # Verify vault was called to store the new key
            mock_vault_service.store_credential.assert_called_once()
            call_args = mock_vault_service.store_credential.call_args
            assert call_args[1]['credential_id'] == f"encryption:tenant:{tenant_id}"
            assert call_args[1]['credential_type'] == CredentialType.CUSTOM
            assert call_args[1]['scope'] == CredentialScope.GLOBAL
            assert call_args[1]['security_level'] == CredentialSecurityLevel.SECRET
            assert call_args[1]['principal_id'] == "system"
            assert call_args[1]['tenant_id'] == tenant_id
            
            # Verify key is cached
            assert tenant_id in encryption_service._key_cache
            assert encryption_service._key_cache[tenant_id] == key
    
    def test_get_tenant_key_existing_tenant(self, encryption_service, mock_vault_service):
        """Test getting encryption key for existing tenant (retrieves from vault)."""
        tenant_id = "tenant-456"
        key_b64 = base64.b64encode(b'\x02' * 32).decode('utf-8')
        
        # Mock vault to return existing credential
        mock_vault_service.retrieve_credential.return_value = CredentialAccessResponse(
            success=True,
            credential_data={"key": key_b64}
        )
        
        key = encryption_service.get_tenant_key(tenant_id)
        
        # Verify key is correct
        assert len(key) == 32
        assert key == b'\x02' * 32
        
        # Verify vault was called to retrieve the key
        mock_vault_service.retrieve_credential.assert_called_once()
        call_args = mock_vault_service.retrieve_credential.call_args
        assert isinstance(call_args[0][0], CredentialAccessRequest)
        assert call_args[0][0].credential_key == f"encryption:tenant:{tenant_id}"
        assert call_args[0][0].tenant_id == tenant_id
        
        # Verify key is cached
        assert tenant_id in encryption_service._key_cache
        
        # Verify vault.store_credential was NOT called (key already exists)
        mock_vault_service.store_credential.assert_not_called()

    def test_get_tenant_key_refuses_generate_when_leftover_row_exists(self):
        """Retrieve miss plus an existing vault row must not mint a second KEK."""
        tenant_id = "motet-global"
        vault = Mock()
        vault.retrieve_credential.return_value = CredentialAccessResponse(
            success=False,
            error_message="Credential not found",
        )
        vault.store_credential.return_value = True
        vault.sync_redis_client.exists.side_effect = (
            lambda key: 1 if str(key).startswith("None:") else 0
        )
        service = EncryptionService(vault_service=vault)

        with pytest.raises(EncryptionError, match="could not be retrieved"):
            service.get_tenant_key(tenant_id)

        vault.store_credential.assert_not_called()
        assert tenant_id not in service._key_cache

    def test_unwrap_key_uses_previous_tenant_kek(self):
        """After rotation, unwrap opens DEKs wrapped with the stored previous KEK."""
        tenant_id = "tenant-rotated"
        old_kek = AESGCM.generate_key(bit_length=256)
        new_kek = AESGCM.generate_key(bit_length=256)
        dek = AESGCM.generate_key(bit_length=256)

        old_vault = Mock()
        old_vault.retrieve_credential.return_value = CredentialAccessResponse(
            success=True,
            credential_data={"key": base64.b64encode(old_kek).decode("utf-8")},
        )
        wrapped = EncryptionService(vault_service=old_vault).wrap_key(dek, tenant_id)

        new_vault = Mock()

        def retrieve(request):
            if request.credential_key == f"encryption:tenant:{tenant_id}:previous":
                return CredentialAccessResponse(
                    success=True,
                    credential_data={"key": base64.b64encode(old_kek).decode("utf-8")},
                )
            return CredentialAccessResponse(
                success=True,
                credential_data={"key": base64.b64encode(new_kek).decode("utf-8")},
            )

        new_vault.retrieve_credential.side_effect = retrieve
        unwrapped = EncryptionService(vault_service=new_vault).unwrap_key(wrapped)
        assert unwrapped == dek
    
    def test_get_tenant_key_caching(self, encryption_service, mock_vault_service):
        """Test that tenant keys are cached after first retrieval."""
        tenant_id = "tenant-789"
        key_b64 = base64.b64encode(b'\x03' * 32).decode('utf-8')
        
        # Mock vault to return existing credential
        mock_vault_service.retrieve_credential.return_value = CredentialAccessResponse(
            success=True,
            credential_data={"key": key_b64}
        )
        
        # First call - should hit vault
        key1 = encryption_service.get_tenant_key(tenant_id)
        assert mock_vault_service.retrieve_credential.call_count == 1
        
        # Second call - should use cache
        key2 = encryption_service.get_tenant_key(tenant_id)
        assert mock_vault_service.retrieve_credential.call_count == 1  # No additional call
        assert key1 == key2
    
    def test_get_tenant_key_invalid_key_length(self, encryption_service, mock_vault_service):
        """Test error handling for invalid key length from vault."""
        tenant_id = "tenant-invalid"
        key_b64 = base64.b64encode(b'\x04' * 16).decode('utf-8')  # Wrong length (16 bytes)
        
        # Mock vault to return invalid key length
        mock_vault_service.retrieve_credential.return_value = CredentialAccessResponse(
            success=True,
            credential_data={"key": key_b64}
        )
        
        with pytest.raises(EncryptionError, match="Invalid tenant key length"):
            encryption_service.get_tenant_key(tenant_id)
    
    def test_get_tenant_key_missing_key_field(self, encryption_service, mock_vault_service):
        """Test error handling for missing 'key' field in vault response."""
        tenant_id = "tenant-missing-key"
        
        # Mock vault to return credential without 'key' field
        mock_vault_service.retrieve_credential.return_value = CredentialAccessResponse(
            success=True,
            credential_data={"other_field": "value"}  # Missing 'key'
        )
        
        with pytest.raises(EncryptionError, match="Tenant key missing 'key' field"):
            encryption_service.get_tenant_key(tenant_id)
    
    def test_encrypt_decrypt_roundtrip(self, encryption_service, mock_vault_service):
        """Test encryption and decryption roundtrip."""
        tenant_id = "tenant-roundtrip"
        test_data = b"Hello, encrypted world!"
        
        # Mock vault to return existing credential
        key = AESGCM.generate_key(bit_length=256)
        key_b64 = base64.b64encode(key).decode('utf-8')
        mock_vault_service.retrieve_credential.return_value = CredentialAccessResponse(
            success=True,
            credential_data={"key": key_b64}
        )
        
        # Encrypt
        encrypted_blob = encryption_service.encrypt(test_data, tenant_id)
        
        # Verify encrypted blob structure
        assert "encrypted_data" in encrypted_blob
        assert "iv" in encrypted_blob
        assert encrypted_blob["tenant_id"] == tenant_id
        assert encrypted_blob["encryption_version"] == "aes-256-gcm-v1"
        
        # Verify encrypted data is different from plaintext
        encrypted_bytes = base64.b64decode(encrypted_blob["encrypted_data"])
        assert encrypted_bytes != test_data
        
        # Decrypt
        decrypted_data = encryption_service.decrypt(encrypted_blob)
        
        # Verify decrypted data matches original
        assert decrypted_data == test_data
    
    def test_encrypt_decrypt_json_data(self, encryption_service, mock_vault_service):
        """Test encryption/decryption with JSON data."""
        tenant_id = "tenant-json"
        test_data_dict = {
            "api_key": "sk-secret-123",
            "user_id": "user-456",
            "metadata": {"created_at": "2024-12-19"}
        }
        test_data_bytes = json.dumps(test_data_dict).encode('utf-8')
        
        # Mock vault to return existing credential
        key = AESGCM.generate_key(bit_length=256)
        key_b64 = base64.b64encode(key).decode('utf-8')
        mock_vault_service.retrieve_credential.return_value = CredentialAccessResponse(
            success=True,
            credential_data={"key": key_b64}
        )
        
        # Encrypt
        encrypted_blob = encryption_service.encrypt(test_data_bytes, tenant_id)
        
        # Decrypt
        decrypted_bytes = encryption_service.decrypt(encrypted_blob)
        decrypted_dict = json.loads(decrypted_bytes)
        
        # Verify decrypted data matches original
        assert decrypted_dict == test_data_dict
    
    def test_tenant_isolation(self, encryption_service, mock_vault_service):
        """Test that tenant A cannot decrypt tenant B's data."""
        tenant_a = "tenant-a"
        tenant_b = "tenant-b"
        test_data = b"Sensitive data for tenant A"
        
        # Generate different keys for each tenant
        key_a = AESGCM.generate_key(bit_length=256)
        key_b = AESGCM.generate_key(bit_length=256)
        
        # Mock vault to return different keys for different tenants
        def mock_retrieve(request):
            if "tenant-a" in request.credential_key:
                return CredentialAccessResponse(
                    success=True,
                    credential_data={"key": base64.b64encode(key_a).decode('utf-8')}
                )
            elif "tenant-b" in request.credential_key:
                return CredentialAccessResponse(
                    success=True,
                    credential_data={"key": base64.b64encode(key_b).decode('utf-8')}
                )
            return CredentialAccessResponse(success=False)
        
        mock_vault_service.retrieve_credential.side_effect = mock_retrieve
        
        # Encrypt with tenant A's key
        encrypted_blob = encryption_service.encrypt(test_data, tenant_a)
        encrypted_blob["tenant_id"] = tenant_b  # Try to decrypt with tenant B's key
        
        # Decryption should fail (wrong key)
        with pytest.raises(EncryptionError):
            encryption_service.decrypt(encrypted_blob)
    
    def test_decrypt_missing_fields(self, encryption_service, mock_vault_service):
        """Test error handling for missing fields in encrypted blob."""
        tenant_id = "tenant-missing-fields"
        
        # Mock vault
        key = AESGCM.generate_key(bit_length=256)
        key_b64 = base64.b64encode(key).decode('utf-8')
        mock_vault_service.retrieve_credential.return_value = CredentialAccessResponse(
            success=True,
            credential_data={"key": key_b64}
        )
        
        # Test missing encrypted_data
        with pytest.raises(EncryptionError, match="Missing encrypted_data or iv"):
            encryption_service.decrypt({
                "iv": "test",
                "tenant_id": tenant_id,
                "encryption_version": "aes-256-gcm-v1"
            })
        
        # Test missing iv
        with pytest.raises(EncryptionError, match="Missing encrypted_data or iv"):
            encryption_service.decrypt({
                "encrypted_data": "test",
                "tenant_id": tenant_id,
                "encryption_version": "aes-256-gcm-v1"
            })
        
        # Test missing tenant_id
        with pytest.raises(EncryptionError, match="Missing tenant_id"):
            encryption_service.decrypt({
                "encrypted_data": "test",
                "iv": "test",
                "encryption_version": "aes-256-gcm-v1"
            })
    
    def test_decrypt_unsupported_version(self, encryption_service, mock_vault_service):
        """Test error handling for unsupported encryption version."""
        tenant_id = "tenant-unsupported"
        
        with pytest.raises(EncryptionError, match="Unsupported encryption version"):
            encryption_service.decrypt({
                "encrypted_data": "test",
                "iv": "test",
                "tenant_id": tenant_id,
                "encryption_version": "unsupported-v2"
            })
    
    def test_decrypt_tampered_data(self, encryption_service, mock_vault_service):
        """Test that tampered encrypted data is detected."""
        tenant_id = "tenant-tampered"
        test_data = b"Original data"
        
        # Mock vault
        key = AESGCM.generate_key(bit_length=256)
        key_b64 = base64.b64encode(key).decode('utf-8')
        mock_vault_service.retrieve_credential.return_value = CredentialAccessResponse(
            success=True,
            credential_data={"key": key_b64}
        )
        
        # Encrypt
        encrypted_blob = encryption_service.encrypt(test_data, tenant_id)
        
        # Tamper with encrypted data
        tampered_blob = encrypted_blob.copy()
        tampered_data = base64.b64decode(tampered_blob["encrypted_data"])
        tampered_data = tampered_data[:-1] + b'X'  # Modify last byte
        tampered_blob["encrypted_data"] = base64.b64encode(tampered_data).decode('utf-8')
        
        # Decryption should fail (authentication tag verification)
        with pytest.raises(EncryptionError):
            encryption_service.decrypt(tampered_blob)
    
    def test_clear_key_cache(self, encryption_service, mock_vault_service):
        """Test clearing key cache."""
        tenant_id = "tenant-cache"
        key_b64 = base64.b64encode(b'\x05' * 32).decode('utf-8')
        
        # Mock vault
        mock_vault_service.retrieve_credential.return_value = CredentialAccessResponse(
            success=True,
            credential_data={"key": key_b64}
        )
        
        # Get key (populates cache)
        encryption_service.get_tenant_key(tenant_id)
        assert tenant_id in encryption_service._key_cache
        
        # Clear cache for specific tenant
        encryption_service.clear_key_cache(tenant_id)
        assert tenant_id not in encryption_service._key_cache
        
        # Clear all caches
        encryption_service.get_tenant_key(tenant_id)  # Repopulate
        encryption_service.clear_key_cache()
        assert len(encryption_service._key_cache) == 0
    
    def test_get_encryption_service_singleton(self, mock_vault_service):
        """Test that get_encryption_service returns singleton instance."""
        with patch('motet.core.security.encryption_service._encryption_service', None):
            service1 = get_encryption_service(mock_vault_service)
            service2 = get_encryption_service()
            
            # Should return same instance
            assert service1 is service2


class TestEncryptionServiceIntegration:
    """Integration tests for EncryptionService with RedisCommandDataManager."""
    
    @pytest.fixture
    def mock_vault_service(self):
        """Create a mock Vault service."""
        vault = Mock(spec=DistributedVaultService)
        vault.retrieve_credential = Mock()
        vault.store_credential = Mock(return_value=True)
        return vault
    
    @pytest.fixture
    def mock_redis(self):
        """Create a mock Redis client."""
        redis_mock = Mock()
        redis_mock.setex = Mock()
        redis_mock.get = Mock()
        return redis_mock
    
    def test_redis_command_data_manager_with_encryption(self, mock_redis, mock_vault_service):
        """Test RedisCommandDataManager with encryption enabled."""
        from motet.core.distributed.redis_command_data_manager import RedisCommandDataManager
        
        # Create manager with encryption enabled
        manager = RedisCommandDataManager(
            redis_client=mock_redis,
            ttl_seconds=3600,
            enable_encryption=True
        )
        
        # Mock encryption service
        with patch('motet.core.security.encryption_service.get_encryption_service') as mock_get_service:
            encryption_service = DummyEncryptionService()
            mock_get_service.return_value = encryption_service
            manager._encryption_service = encryption_service
            
            # Test data
            command_id = "test-command-encrypted"
            tenant_id = "tenant-integration"
            test_data = {
                "messages": [{"role": "user", "content": "Hello"}],
                "api_key": "sk-secret-123"
            }
            
            import msgpack
            redis_key = manager.store_command_data(
                command_id=command_id,
                data=test_data,
                tenant_id=tenant_id,
                motet_id="test-motet"
            )
            # Verify Redis was called
            assert mock_redis.setex.called
            
            # Get stored data from Redis mock (setex(key, ttl, value))
            # Call args: (key, ttl, value) or kwargs
            call_args_list = mock_redis.setex.call_args
            if call_args_list[0]:  # Positional args
                stored_data = call_args_list[0][2]  # Third argument is the value
            else:  # Keyword args
                stored_data = call_args_list[1]['value']
            
            # Deserialize to check structure
            storage_data = msgpack.unpackb(stored_data, raw=False)
            
            # Verify data is encrypted
            assert storage_data.get("encrypted") is True
            assert "encryption" in storage_data
            assert "dek" in storage_data
            assert "metadata" in storage_data
            
            # Mock Redis get to return stored data
            mock_redis.get.return_value = stored_data
            
            # Retrieve and decrypt
            retrieved_data = manager.retrieve_command_data(redis_key, tenant_id=tenant_id, motet_id="test-motet")
            
            # Verify decrypted data matches original
            assert retrieved_data == test_data
    
    def test_redis_command_data_manager_backward_compatibility(self, mock_redis):
        """Test RedisCommandDataManager handles plaintext data (backward compatibility)."""
        from motet.core.distributed.redis_command_data_manager import RedisCommandDataManager
        
        # Create manager
        manager = RedisCommandDataManager(
            redis_client=mock_redis,
            ttl_seconds=3600,
            enable_encryption=False  # Encryption disabled
        )
        
        # Test data
        command_id = "test-command-plaintext"
        test_data = {"test": "data"}
        
        # Store plaintext data
        redis_key = manager.store_command_data(
            command_id=command_id,
            data=test_data
        )
        
        # Get stored data (setex(key, ttl, value))
        call_args_list = mock_redis.setex.call_args
        if call_args_list[0]:  # Positional args
            stored_data = call_args_list[0][2]  # Third argument is the value
        else:  # Keyword args
            stored_data = call_args_list[1]['value']
        import msgpack
        storage_data = msgpack.unpackb(stored_data, raw=False)
        
        # Verify data is plaintext
        assert storage_data.get("encrypted") is False
        assert "data" in storage_data
        
        # Mock Redis get to return stored data
        mock_redis.get.return_value = stored_data
        
        # Retrieve plaintext data
        retrieved_data = manager.retrieve_command_data(redis_key)
        
        # Verify data matches
        assert retrieved_data == test_data


class TestEncryptionServiceWithDistributedCommand:
    """Integration tests for EncryptionService with DistributedCommand."""
    
    @pytest.fixture
    def mock_vault_service(self):
        """Create a mock Vault service."""
        vault = Mock(spec=DistributedVaultService)
        vault.retrieve_credential = Mock()
        vault.store_credential = Mock(return_value=True)
        return vault
    
    @pytest.fixture
    def mock_redis(self):
        """Create a mock Redis client."""
        redis_mock = Mock()
        redis_mock.setex = Mock()
        redis_mock.get = Mock()
        return redis_mock
    
    def test_distributed_command_passes_tenant_id(self, mock_redis, mock_vault_service):
        """Test that DistributedCommand passes tenant_id to RedisCommandDataManager."""
        from motet.core.commands.distributed import (
            DistributedCommand, DistributedCommandContext
        )
        from motet.core.distributed.redis_command_data_manager import RedisCommandDataManager
        
        # Create manager with encryption enabled
        manager = RedisCommandDataManager(
            redis_client=mock_redis,
            ttl_seconds=3600,
            enable_encryption=True
        )
        
        # Mock encryption service
        with patch('motet.core.security.encryption_service.get_encryption_service') as mock_get_service:
            encryption_service = DummyEncryptionService()
            mock_get_service.return_value = encryption_service
            manager._encryption_service = encryption_service
            
            # Mock get_redis_command_data_manager to return our manager
            with patch('motet.core.distributed.get_redis_command_data_manager') as mock_get_manager:
                mock_get_manager.return_value = manager

                # Create a test command with tenant_id
                class TestCommand(DistributedCommand):
                    def _get_default_timeout(self):
                        return 60
                    
                    def _get_default_priority(self):
                        return 5
                    
                    def _setup_command_specifics(self):
                        pass
                    
                    @classmethod
                    def _get_data_class(cls):
                        return None
                    
                    def get_command_type(self):
                        return "test"
                    
                    async def _do_execute(self, worker_context):
                        return {"result": "success"}
                    
                    def can_undo(self) -> bool:
                        return False
                    
                    def undo(self) -> Any:
                        raise NotImplementedError
                
                # Create command with tenant_id in context
                context = DistributedCommandContext(
                    task_id="test-task",
                    conversation_id="test-conv",
                    tenant_id="test-tenant-123"
                )
                
                command = TestCommand("test-task", {"test": "data"})
                command.distributed_context = context
                command.command_id = "test-command-id"
                
                # Store command data (should pass tenant_id)
                redis_key = command._store_command_data_in_redis({"test": "data"})
                
                # Verify store_command_data was called with tenant_id
                assert mock_redis.setex.called
                
                # Verify the stored data is encrypted (check structure)
                # setex(key, ttl, value) - value is third positional arg
                call_args_list = mock_redis.setex.call_args
                if call_args_list[0]:  # Positional args
                    stored_data = call_args_list[0][2]  # Third argument is the value
                else:  # Keyword args
                    stored_data = call_args_list[1]['value']
                import msgpack
                storage_data = msgpack.unpackb(stored_data, raw=False)
                
                # Should be encrypted since tenant_id was provided
                assert storage_data.get("encrypted") is True
                assert "encryption" in storage_data
                assert "dek" in storage_data

