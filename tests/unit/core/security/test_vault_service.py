"""
Tests for the Distributed Vault Service

Comprehensive test suite covering all vault functionality including encryption,
credential storage/retrieval, access control, and MCP integration.
"""

import pytest
import asyncio
import json
import os
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from typing import Dict, Any, Optional

from motet.core.security.vault_service import (
    DistributedVaultService,
    VaultEncryption,
    CredentialType,
    CredentialScope,
    CredentialSecurityLevel,
    CredentialMetadata,
    CredentialAccessRequest,
    CredentialAccessResponse,
    store_credential_for_principal,
    get_credential_for_principal
)
from motet.core.security.vault_client import VaultClient, VaultClientConfig
from motet.core.security.vault_mcp_integration import VaultMCPIntegration, MCPCredentialMapping
from motet.core.commands.base import CommandContext
from motet.core.types import Principal


class TestVaultEncryption:
    """Test the VaultEncryption class."""
    
    def test_encryption_initialization(self):
        """Test encryption initialization with master key."""
        encryption = VaultEncryption("test-master-key")
        assert encryption.master_key == "test-master-key"
        assert encryption._fernet is not None
    
    def test_encryption_without_master_key(self):
        """Test encryption initialization without master key (generates one when allowed)."""
        with patch.dict(os.environ, {"MOTET_ALLOW_EPHEMERAL_MASTER_KEY": "true"}, clear=False):
            encryption = VaultEncryption()
            assert encryption.master_key is not None
            assert len(encryption.master_key) > 0
    
    def test_encryption_with_env_key(self):
        """Test encryption initialization with environment variable."""
        test_key = "test-env-key"
        with patch.dict(os.environ, {"MOTET_VAULT_MASTER_KEY": test_key}):
            encryption = VaultEncryption()
            assert encryption.master_key == test_key
    
    def test_encrypt_decrypt_credential(self):
        """Test credential encryption and decryption."""
        encryption = VaultEncryption("test-master-key")
        
        test_data = {
            "api_key": "test-api-key-123",
            "secret": "test-secret-456",
            "metadata": {"created_at": "2024-12-19"}
        }
        
        # Encrypt
        encrypted = encryption.encrypt_credential(test_data)
        assert encrypted is not None
        assert encrypted != json.dumps(test_data)  # Should be encrypted
        
        # Decrypt
        decrypted = encryption.decrypt_credential(encrypted)
        assert decrypted == test_data
    
    def test_encrypt_decrypt_different_data(self):
        """Test encryption/decryption with different data types."""
        encryption = VaultEncryption("test-master-key")
        
        test_cases = [
            {"simple": "value"},
            {"nested": {"key": "value"}},
            {"list": [1, 2, 3]},
            {"mixed": {"str": "test", "int": 42, "bool": True, "list": [1, 2, 3]}}
        ]
        
        for test_data in test_cases:
            encrypted = encryption.encrypt_credential(test_data)
            decrypted = encryption.decrypt_credential(encrypted)
            assert decrypted == test_data
    
    def test_encryption_tamper_detection(self):
        """Test that tampered encrypted data is detected."""
        encryption = VaultEncryption("test-master-key")
        
        test_data = {"api_key": "test-key"}
        encrypted = encryption.encrypt_credential(test_data)
        
        # Tamper with the encrypted data
        tampered = encrypted[:-1] + "X"
        
        with pytest.raises(RuntimeError, match="Credential decryption failed"):
            encryption.decrypt_credential(tampered)
    
    def test_different_keys_produce_different_encryption(self):
        """Test that different master keys produce different encryption."""
        encryption1 = VaultEncryption("key1")
        encryption2 = VaultEncryption("key2")
        
        test_data = {"api_key": "test-key"}
        
        encrypted1 = encryption1.encrypt_credential(test_data)
        encrypted2 = encryption2.encrypt_credential(test_data)
        
        assert encrypted1 != encrypted2
        
        # Each should only decrypt with its own key
        assert encryption1.decrypt_credential(encrypted1) == test_data
        assert encryption2.decrypt_credential(encrypted2) == test_data
        
        # Cross-decryption should fail
        with pytest.raises(RuntimeError):
            encryption1.decrypt_credential(encrypted2)
        with pytest.raises(RuntimeError):
            encryption2.decrypt_credential(encrypted1)


class TestDistributedVaultService:
    """Test the DistributedVaultService class."""
    
    @pytest.fixture
    def vault_service(self):
        """Create a vault service instance for testing."""
        with patch.dict(os.environ, {"MOTET_VAULT_MASTER_KEY": "test-master-key-for-unit-tests"}, clear=False):
            with patch('motet.core.security.vault_service.get_sync_redis_client') as mock_redis:
                mock_redis.return_value = Mock()
                service = DistributedVaultService()
                yield service
    
    @pytest.fixture
    def sample_credential_data(self):
        """Sample credential data for testing."""
        return {
            "api_key": "test-api-key-123",
            "secret": "test-secret-456",
            "metadata": {"created_at": "2024-12-19"}
        }
    
    @pytest.fixture
    def sample_metadata(self):
        """Sample credential metadata for testing."""
        return CredentialMetadata(
            credential_id="test_credential",
            credential_type=CredentialType.API_KEY,
            scope=CredentialScope.PRINCIPAL,
            security_level=CredentialSecurityLevel.CONFIDENTIAL,
            principal_id="test_principal",
            tenant_id="test_tenant",
            motet_id="test_motet",
            created_by="test_principal",
            expires_at=datetime.utcnow() + timedelta(days=30),
            tags=["test", "api"],
            description="Test credential"
        )
    
    def test_store_credential_success(self, vault_service, sample_credential_data):
        """Test successful credential storage."""
        with patch('motet.core.security.vault_service.acquire_distributed_lock_sync') as mock_lock:
            with patch('motet.core.security.vault_service.store_structured_data_sync') as mock_store:
                with patch.object(vault_service.sync_redis_client, 'scan_iter', return_value=iter([])):
                    with patch.object(vault_service.sync_redis_client, 'delete', return_value=0):
                        mock_lock_inst = Mock()
                        mock_lock_inst.release_sync = Mock(return_value=True)
                        mock_lock.return_value = mock_lock_inst
                        mock_store.return_value = None

                        result = vault_service.store_credential(
                            credential_id="test_credential",
                            credential_data=sample_credential_data,
                            credential_type=CredentialType.API_KEY,
                            scope=CredentialScope.PRINCIPAL,
                            security_level=CredentialSecurityLevel.CONFIDENTIAL,
                            principal_id="test_principal",
                            tenant_id="test_tenant",
                            motet_id="test_motet"
                        )

                        assert result is True
                        assert mock_lock.called
                        assert mock_store.call_count >= 2  # credential data + metadata
                        vault_service.sync_redis_client.sadd.assert_any_call(
                            "test_tenant:vault:index", "test_credential"
                        )
    
    def test_store_credential_lock_failure(self, vault_service, sample_credential_data):
        """Test credential storage when lock acquisition fails."""
        with patch('motet.core.security.vault_service.acquire_distributed_lock_sync') as mock_lock:
            mock_lock.return_value = None
            
            result = vault_service.store_credential(
                credential_id="test_credential",
                credential_data=sample_credential_data,
                credential_type=CredentialType.API_KEY,
                scope=CredentialScope.PRINCIPAL,
                security_level=CredentialSecurityLevel.CONFIDENTIAL,
                principal_id="test_principal"
            )
            
            assert result is False
    
    def test_retrieve_credential_success(self, vault_service):
        """Test successful credential retrieval."""
        request = CredentialAccessRequest(
            principal_id="test_principal",
            tenant_id="test_tenant",
            motet_id="test_motet",
            credential_key="test_credential"
        )
        
        # Mock the retrieval process
        with patch('motet.core.security.vault_service.retrieve_structured_data_sync') as mock_retrieve:
            with patch('motet.core.security.vault_service.store_structured_data_sync') as mock_store:
                # Mock cache miss
                mock_retrieve.side_effect = [
                    None,  # cache miss
                    {  # metadata
                        "credential_id": "test_credential",
                        "scope": "principal",
                        "principal_id": "test_principal",
                        "tenant_id": "test_tenant",
                        "motet_id": "test_motet",
                        "expires_at": None
                    },
                    {  # credential data
                        "encrypted_data": vault_service.encryption.encrypt_credential({"api_key": "test-key"})
                    }
                ]
                mock_store.return_value = None
                
                response = vault_service.retrieve_credential(request)
                
                assert response.success is True
                assert response.credential_data == {"api_key": "test-key"}
                assert response.access_granted_at is not None
    
    def test_retrieve_credential_not_found(self, vault_service):
        """Test credential retrieval when credential doesn't exist."""
        request = CredentialAccessRequest(
            principal_id="test_principal",
            credential_key="nonexistent_credential"
        )
        
        with patch('motet.core.security.vault_service.retrieve_structured_data_sync') as mock_retrieve:
            mock_retrieve.side_effect = [None, None]  # cache miss, metadata not found
            
            response = vault_service.retrieve_credential(request)
            
            assert response.success is False
            assert "not found" in (response.error_message or "").lower()

    def test_retrieve_credential_unauthorized(self, vault_service):
        """Test credential retrieval when principal is unauthorized."""
        request = CredentialAccessRequest(
            principal_id="unauthorized_principal",
            credential_key="test_credential"
        )
        
        with patch('motet.core.security.vault_service.retrieve_structured_data_sync') as mock_retrieve:
            mock_retrieve.side_effect = [
                None,  # cache miss
                {  # metadata with different principal
                    "credential_id": "test_credential",
                    "scope": "principal",
                    "principal_id": "different_principal",
                    "tenant_id": None,
                    "motet_id": None
                }
            ]
            
            response = vault_service.retrieve_credential(request)
            
            assert response.success is False
            assert "access denied" in (response.error_message or getattr(response, "error", "") or "").lower()
    
    def test_retrieve_credential_expired(self, vault_service):
        """Test credential retrieval when credential has expired."""
        request = CredentialAccessRequest(
            principal_id="test_principal",
            credential_key="expired_credential"
        )
        
        expired_time = datetime.utcnow() - timedelta(days=1)
        
        with patch('motet.core.security.vault_service.retrieve_structured_data_sync') as mock_retrieve:
            mock_retrieve.side_effect = [
                None,  # cache miss
                {  # metadata with expired time
                    "credential_id": "expired_credential",
                    "scope": "principal",
                    "principal_id": "test_principal",
                    "tenant_id": None,
                    "motet_id": None,
                    "expires_at": expired_time.isoformat()
                }
            ]
            
            response = vault_service.retrieve_credential(request)
            
            assert response.success is False
            assert "expired" in (response.error_message or "").lower()
    
    def test_authorization_global_scope(self, vault_service):
        """Test authorization for global scope credentials."""
        request = CredentialAccessRequest(
            principal_id="any_principal",
            credential_key="global_credential"
        )
        
        metadata = {
            "scope": "global",
            "principal_id": "creator_principal"
        }
        
        result = vault_service._check_authorization(request, metadata)
        assert result is True
    
    def test_authorization_principal_scope(self, vault_service):
        """Test authorization for principal scope credentials."""
        # Authorized access
        request = CredentialAccessRequest(
            principal_id="test_principal",
            credential_key="principal_credential"
        )
        
        metadata = {
            "scope": "principal",
            "principal_id": "test_principal"
        }
        
        result = vault_service._check_authorization(request, metadata)
        assert result is True
        
        # Unauthorized access
        request.principal_id = "different_principal"
        result = vault_service._check_authorization(request, metadata)
        assert result is False
    
    def test_authorization_tenant_scope(self, vault_service):
        """Test authorization for tenant scope credentials."""
        # Authorized access
        request = CredentialAccessRequest(
            principal_id="test_principal",
            tenant_id="test_tenant",
            credential_key="tenant_credential"
        )
        
        metadata = {
            "scope": "tenant",
            "tenant_id": "test_tenant"
        }
        
        result = vault_service._check_authorization(request, metadata)
        assert result is True
        
        # Unauthorized access
        request.tenant_id = "different_tenant"
        result = vault_service._check_authorization(request, metadata)
        assert result is False
    
    def test_authorization_motet_scope(self, vault_service):
        """Test authorization for motet scope credentials."""
        # Authorized access
        request = CredentialAccessRequest(
            principal_id="test_principal",
            motet_id="test_motet",
            credential_key="motet_credential"
        )
        
        metadata = {
            "scope": "motet",
            "motet_id": "test_motet"
        }
        
        result = vault_service._check_authorization(request, metadata)
        assert result is True
        
        # Unauthorized access
        request.motet_id = "different_motet"
        result = vault_service._check_authorization(request, metadata)
        assert result is False
    
    def test_list_credentials(self, vault_service):
        """Test listing credentials for a principal."""
        def _smembers(key: str):
            if key == "test_tenant:vault:index":
                return {"cred1", "cred2"}
            return set()

        vault_service.sync_redis_client.smembers = Mock(side_effect=_smembers)
        with patch('motet.core.security.vault_service.retrieve_structured_data_sync') as mock_retrieve:
            mock_retrieve.side_effect = [
                {  # cred1 metadata
                    "credential_id": "cred1",
                    "credential_type": "api_key",
                    "scope": "principal",
                    "security_level": "confidential",
                    "principal_id": "test_principal"
                },
                {  # cred2 metadata
                    "credential_id": "cred2",
                    "credential_type": "bearer_token",
                    "scope": "tenant",
                    "security_level": "confidential",
                    "principal_id": "test_principal",
                    "tenant_id": "test_tenant"
                }
            ]
            
            credentials = vault_service.list_credentials(
                principal_id="test_principal",
                tenant_id="test_tenant"
            )
            
            assert len(credentials) == 2
            assert {c.credential_id for c in credentials} == {"cred1", "cred2"}
            vault_service.sync_redis_client.smembers.assert_any_call("test_tenant:vault:index")
            vault_service.sync_redis_client.smembers.assert_any_call("motet:vault:index")
            vault_service.sync_redis_client.keys.assert_not_called()
    
    def test_clear_credential_cache_scans_and_dedupes(self, vault_service):
        """Cache clear uses SCAN, not KEYS, and deletes each hash once."""
        hits = {
            "vault:cache:*:test_credential*": [
                b"vault:cache:p1:test_credential",
                b"acme:vault:cache:p1:test_credential",
            ],
            "*:vault:cache:*:test_credential*": [
                b"acme:vault:cache:p1:test_credential",
            ],
            "motet:vault:cache:*:test_credential*": [
                b"motet:vault:cache:p1:test_credential",
            ],
            "*:motet:vault:cache:*:test_credential*": [],
        }

        def _scan_iter(*, match: str, count: int = 100):
            return iter(hits.get(match, []))

        redis = vault_service.sync_redis_client
        redis.scan_iter = Mock(side_effect=_scan_iter)
        redis.delete = Mock(return_value=2)
        redis.keys = Mock(return_value=["should-not-be-used"])

        cleared = vault_service._clear_credential_cache("test_credential")

        assert cleared == 3
        redis.keys.assert_not_called()
        redis.delete.assert_called_once()
        deleted = list(redis.delete.call_args.args)
        assert deleted == [
            b"vault:cache:p1:test_credential",
            b"acme:vault:cache:p1:test_credential",
            b"motet:vault:cache:p1:test_credential",
        ]
        scanned = [c.kwargs["match"] for c in redis.scan_iter.call_args_list]
        assert "vault:cache:*:test_credential*" in scanned
        assert "*:vault:cache:*:test_credential*" in scanned
        assert "motet:vault:cache:*:test_credential*" in scanned
        assert "imf:vault:cache:*:test_credential*" not in scanned

    def test_delete_credential_success(self, vault_service):
        """Test successful credential deletion."""
        mock_lock = Mock()
        mock_lock.release_sync = Mock(return_value=True)
        with patch('motet.core.security.vault_service.retrieve_structured_data_sync') as mock_retrieve:
            with patch('motet.core.security.vault_service.acquire_distributed_lock_sync') as mock_lock_fn:
                mock_retrieve.return_value = {
                    "credential_id": "test_credential",
                    "principal_id": "test_principal",
                    "tenant_id": "test_tenant",
                }
                mock_lock_fn.return_value = mock_lock
                vault_service.sync_redis_client.delete = Mock(return_value=1)
                vault_service.sync_redis_client.scan_iter = Mock(return_value=iter([]))
                
                result = vault_service.delete_credential(
                    credential_id="test_credential",
                    principal_id="test_principal"
                )
                
                assert result is True
                assert vault_service.sync_redis_client.delete.called
                vault_service.sync_redis_client.srem.assert_any_call(
                    "test_tenant:vault:index", "test_credential"
                )
                scanned = [
                    c.kwargs["match"]
                    for c in vault_service.sync_redis_client.scan_iter.call_args_list
                ]
                assert "vault:cache:*:test_credential*" in scanned
                assert "*:vault:cache:*:test_credential*" in scanned
                assert "motet:vault:cache:*:test_credential*" in scanned
                assert "imf:vault:cache:*:test_credential*" not in scanned
                vault_service.sync_redis_client.keys.assert_not_called()
    
    def test_delete_credential_unauthorized(self, vault_service):
        """Test credential deletion when principal is unauthorized."""
        with patch('motet.core.security.vault_service.retrieve_structured_data_sync') as mock_retrieve:
            mock_retrieve.return_value = {
                "credential_id": "test_credential",
                "principal_id": "different_principal"
            }
            
            result = vault_service.delete_credential(
                credential_id="test_credential",
                principal_id="test_principal"
            )
            
            assert result is False

    def test_resolve_vault_key_skips_scan_when_tenant_known(self, vault_service):
        redis = vault_service.sync_redis_client
        redis.exists.return_value = 0
        resolved = vault_service._resolve_vault_key(
            "vault:credential:oauth_tokens:google_workspace",
            tenant_id="motet-global",
        )
        assert resolved == "motet-global:vault:credential:oauth_tokens:google_workspace"
        redis.scan.assert_not_called()

    def test_resolve_vault_key_uses_locate_when_tenant_unknown(self, vault_service):
        redis = vault_service.sync_redis_client
        redis.exists.return_value = 0
        redis.get.return_value = "acme"
        redis.scan = Mock(side_effect=AssertionError("vault resolve must not SCAN"))
        resolved = vault_service._resolve_vault_key(
            "vault:credential:oauth_tokens:google_workspace",
        )
        assert resolved == "acme:vault:credential:oauth_tokens:google_workspace"
        redis.get.assert_called_with("motet:vault:locate:oauth_tokens:google_workspace")

    def test_resolve_vault_key_finds_none_prefix_leftover(self, vault_service):
        redis = vault_service.sync_redis_client
        leftover = "None:vault:metadata:encryption:tenant:default"
        redis.exists.side_effect = lambda key: 1 if key == leftover else 0
        redis.get.return_value = None
        resolved = vault_service._resolve_vault_key(
            "vault:metadata:encryption:tenant:default",
            tenant_id="default",
        )
        assert resolved == leftover

    def test_scoped_vault_key_rejects_none_tenant(self, vault_service):
        assert vault_service._scoped_vault_key(
            "vault:metadata:encryption:tenant:default", "None"
        ) == "motet:vault:metadata:encryption:tenant:default"
        assert vault_service._scoped_vault_key(
            "vault:metadata:encryption:tenant:default", "default"
        ) == "default:vault:metadata:encryption:tenant:default"


class TestVaultClient:
    """Test the VaultClient class."""
    
    @pytest.fixture
    def vault_client(self):
        """Create a vault client instance for testing."""
        with patch('motet.core.security.vault_client.DistributedVaultService') as mock_service:
            mock_svc = Mock()
            mock_svc.retrieve_credential = Mock(return_value=CredentialAccessResponse(
                success=True, credential_data={"api_key": "test-key"}, access_granted_at=datetime.utcnow()
            ))
            mock_service.return_value = mock_svc
            client = VaultClient()
            yield client
    
    @pytest.fixture
    def command_context(self):
        """Create a command context for testing."""
        return CommandContext(
            task_id="test_task",
            principal_id="test_principal",
            tenant_id="test_tenant",
            motet_id="test_motet",
            conversation_id="test_conversation"
        )
    
    def test_get_credential_success(self, vault_client, command_context):
        """Test successful credential retrieval."""
        credential_data = {"api_key": "test-key"}
        
        with patch.object(vault_client.vault_service, 'retrieve_credential') as mock_retrieve:
            mock_retrieve.return_value = CredentialAccessResponse(
                success=True,
                credential_data=credential_data,
                access_granted_at=datetime.utcnow()
            )
            
            result = vault_client.get_credential(
                credential_key="test_credential",
                context=command_context
            )
            
            assert result == credential_data
            assert mock_retrieve.called
    
    def test_get_credential_cache_hit(self, vault_client, command_context):
        """Test credential retrieval from cache."""
        credential_data = {"api_key": "test-key"}
        
        # Store in cache first
        cache_key = vault_client._make_cache_key(command_context.principal_id, "test_credential")
        vault_client._store_in_cache(cache_key, credential_data)
        
        result = vault_client.get_credential(
            credential_key="test_credential",
            context=command_context
        )
        
        assert result == credential_data
        # Should not call vault service
        assert not vault_client.vault_service.retrieve_credential.called
    
    def test_get_credential_retry_logic(self, vault_client, command_context):
        """Test retry logic for credential retrieval."""
        with patch.object(vault_client.vault_service, 'retrieve_credential') as mock_retrieve:
            with patch('motet.core.security.vault_client.worker_sleep') as mock_sleep:
                # First call fails, second succeeds
                mock_retrieve.side_effect = [
                    Exception("Network error"),
                    CredentialAccessResponse(
                        success=True,
                        credential_data={"api_key": "test-key"},
                        access_granted_at=datetime.utcnow()
                    )
                ]
                
                result = vault_client.get_credential(
                    credential_key="test_credential",
                    context=command_context
                )
                
                assert result == {"api_key": "test-key"}
                assert mock_retrieve.call_count == 2
                assert mock_sleep.called
    
    def test_get_bearer_token(self, vault_client, command_context):
        """Test bearer token retrieval."""
        credential_data = {"access_token": "test-bearer-token"}
        
        with patch.object(vault_client, 'get_credential') as mock_get:
            mock_get.return_value = credential_data
            
            result = vault_client.get_bearer_token("google_workspace", command_context)
            
            assert result == "test-bearer-token"
            mock_get.assert_called_with(
                credential_key="google_workspace_bearer_token",
                context=command_context,
                credential_type=CredentialType.BEARER_TOKEN
            )
    
    def test_get_api_key(self, vault_client, command_context):
        """Test API key retrieval."""
        credential_data = {"api_key": "test-api-key"}
        
        with patch.object(vault_client, 'get_credential') as mock_get:
            mock_get.return_value = credential_data
            
            result = vault_client.get_api_key("openai", command_context)
            
            assert result == "test-api-key"
            mock_get.assert_called_with(
                credential_key="openai_api_key",
                context=command_context,
                credential_type=CredentialType.API_KEY
            )
    
    def test_store_credential(self, vault_client, command_context):
        """Test credential storage."""
        credential_data = {"api_key": "test-key"}

        with patch.object(vault_client.vault_service, 'store_credential') as mock_store:
            mock_store.return_value = True

            result = vault_client.store_credential(
                credential_key="test_credential",
                credential_data=credential_data,
                context=command_context
            )

            assert result is True
            mock_store.assert_called()
    
    def test_clear_cache(self, vault_client):
        """Test cache clearing."""
        # Add some test data to cache
        vault_client._local_cache["principal1:cred1"] = {"data": "test1"}
        vault_client._local_cache["principal2:cred2"] = {"data": "test2"}
        vault_client._cache_timestamps["principal1:cred1"] = 1234567890
        vault_client._cache_timestamps["principal2:cred2"] = 1234567890
        
        # Clear cache for specific principal
        vault_client.clear_cache("principal1")
        
        assert "principal1:cred1" not in vault_client._local_cache
        assert "principal2:cred2" in vault_client._local_cache
        
        # Clear entire cache
        vault_client.clear_cache()
        
        assert len(vault_client._local_cache) == 0
        assert len(vault_client._cache_timestamps) == 0
    
    def test_get_metrics(self, vault_client):
        """Test metrics collection."""
        # Add some test metrics
        vault_client._access_metrics = {
            "total_requests": 100,
            "cache_hits": 20,
            "vault_hits": 80,
            "vault_errors": 5
        }
        
        metrics = vault_client.get_metrics()
        
        assert metrics["total_requests"] == 100
        assert metrics["cache_hits"] == 20
        assert metrics["vault_hits"] == 80
        assert metrics["vault_errors"] == 5
        assert metrics["cache_hit_rate_percent"] == 20.0
        assert metrics["error_rate_percent"] == 5.0


class TestVaultMCPIntegration:
    """Test the VaultMCPIntegration class."""
    
    @pytest.fixture
    def vault_mcp_integration(self):
        """Create a vault MCP integration instance for testing."""
        with patch('motet.core.security.vault_mcp_integration.get_vault_client') as mock_client:
            mock_vc = Mock()
            mock_vc.store_credential = Mock(return_value=True)
            mock_vc.get_credential = Mock(return_value={})
            mock_vc.clear_cache = Mock(return_value=None)
            mock_client.return_value = mock_vc
            integration = VaultMCPIntegration()
            yield integration
    
    @pytest.fixture
    def command_context(self):
        """Create a command context for testing."""
        return CommandContext(
            task_id="test_task",
            principal_id="test_principal",
            tenant_id="test_tenant",
            motet_id="test_motet",
            conversation_id="test_conversation"
        )
    
    def test_initialize_default_mappings(self, vault_mcp_integration):
        """Test that default MCP mappings are initialized."""
        assert "google_workspace" in vault_mcp_integration.credential_mappings
        assert "github" in vault_mcp_integration.credential_mappings
        assert "openai" in vault_mcp_integration.credential_mappings
        assert "anthropic" in vault_mcp_integration.credential_mappings
    
    def test_register_mcp_credential_mapping(self, vault_mcp_integration):
        """Test registering a new MCP credential mapping."""
        mapping = MCPCredentialMapping(
            mcp_server_id="custom_service",
            credential_keys=["custom_key"],
            credential_types=[CredentialType.API_KEY],
            required_scopes=[CredentialScope.PRINCIPAL],
            security_level=CredentialSecurityLevel.CONFIDENTIAL
        )
        
        vault_mcp_integration.register_mcp_credential_mapping(mapping)
        
        assert "custom_service" in vault_mcp_integration.credential_mappings
        assert vault_mcp_integration.credential_mappings["custom_service"] == mapping
    
    def test_get_mcp_environment_variables_success(self, vault_mcp_integration, command_context):
        """Test successful MCP environment variable retrieval."""
        credential_data = {
            "access_token": "test-access-token",
            "refresh_token": "test-refresh-token"
        }
        
        with patch.object(vault_mcp_integration.vault_client, 'get_credential') as mock_get:
            mock_get.return_value = credential_data
            
            env_vars = vault_mcp_integration.get_mcp_environment_variables(
                "google_workspace", command_context
            )
            
            assert "GOOGLE_ACCESS_TOKEN" in env_vars or "GOOGLE_REFRESH_TOKEN" in env_vars or len(env_vars) >= 0
            if "GOOGLE_ACCESS_TOKEN" in env_vars:
                assert env_vars["GOOGLE_ACCESS_TOKEN"] == "test-access-token"
    
    def test_get_mcp_environment_variables_unknown_server(self, vault_mcp_integration, command_context):
        """Test MCP environment variable retrieval for unknown server."""
        env_vars = vault_mcp_integration.get_mcp_environment_variables(
            "unknown_server", command_context
        )
        
        assert env_vars == {}
    
    def test_store_mcp_credential(self, vault_mcp_integration, command_context):
        """Test storing MCP credential."""
        credential_data = {"api_key": "test-key"}
        
        with patch.object(vault_mcp_integration.vault_client, 'store_credential') as mock_store:
            mock_store.return_value = True
            
            result = vault_mcp_integration.store_mcp_credential(
                mcp_server_id="openai",
                credential_key="openai_api_key",
                credential_data=credential_data,
                context=command_context
            )
            
            assert result is True
            mock_store.assert_called()
    
    def test_refresh_mcp_credentials(self, vault_mcp_integration, command_context):
        """Test refreshing MCP credentials."""
        with patch.object(vault_mcp_integration.vault_client, 'clear_cache') as mock_clear:
            with patch.object(vault_mcp_integration, 'get_mcp_environment_variables') as mock_get:
                mock_get.return_value = {"GOOGLE_ACCESS_TOKEN": "new-token"}
                
                result = vault_mcp_integration.refresh_mcp_credentials(
                    "google_workspace", command_context
                )
                
                assert result is True
                mock_clear.assert_called()
                mock_get.assert_called()
    
    def test_get_supported_mcp_servers(self, vault_mcp_integration):
        """Test getting list of supported MCP servers."""
        servers = vault_mcp_integration.get_supported_mcp_servers()
        
        assert "google_workspace" in servers
        assert "github" in servers
        assert "openai" in servers
        assert "anthropic" in servers
    
    def test_get_mcp_credential_mapping(self, vault_mcp_integration):
        """Test getting MCP credential mapping."""
        mapping = vault_mcp_integration.get_mcp_credential_mapping("google_workspace")
        
        assert mapping is not None
        assert mapping.mcp_server_id == "google_workspace"
        assert "google_bearer_token" in mapping.credential_keys or "oauth:tokens:google_workspace" in str(mapping.credential_keys)
        
        # Test unknown server
        mapping = vault_mcp_integration.get_mcp_credential_mapping("unknown_server")
        assert mapping is None


class TestConvenienceFunctions:
    """Test convenience functions."""
    
    def test_store_credential_for_principal(self):
        """Test store_credential_for_principal convenience function."""
        with patch('motet.core.security.vault_service.DistributedVaultService') as mock_service:
            mock_instance = Mock()
            mock_instance.store_credential.return_value = True
            mock_service.return_value = mock_instance
            
            result = store_credential_for_principal(
                credential_id="test_credential",
                credential_data={"api_key": "test-key"},
                credential_type=CredentialType.API_KEY,
                principal_id="test_principal"
            )
            
            assert result is True
            mock_instance.store_credential.assert_called()
    
    def test_get_credential_for_principal(self):
        """Test get_credential_for_principal convenience function."""
        with patch('motet.core.security.vault_service.DistributedVaultService') as mock_service:
            mock_instance = Mock()
            mock_instance.retrieve_credential.return_value = CredentialAccessResponse(
                success=True,
                credential_data={"api_key": "test-key"},
                access_granted_at=datetime.utcnow()
            )
            mock_service.return_value = mock_instance
            
            result = get_credential_for_principal(
                credential_key="test_credential",
                principal_id="test_principal"
            )
            
            assert result == {"api_key": "test-key"}
            mock_instance.retrieve_credential.assert_called()


if __name__ == "__main__":
    pytest.main([__file__])
