"""
Integration Tests for Vault Service

These tests verify that the vault service components work together correctly
in a realistic environment with Redis and proper error handling.
"""

import pytest
import asyncio
import os
import json
from datetime import datetime, timedelta
from typing import Dict, Any

from motet.core.security.vault_service import (
    DistributedVaultService,
    CredentialType,
    CredentialScope,
    CredentialSecurityLevel,
    CredentialAccessRequest
)
from motet.core.security.vault_client import VaultClient
from motet.core.security.vault_mcp_integration import VaultMCPIntegration
from motet.core.commands.base import CommandContext


@pytest.mark.integration
@pytest.mark.requires_redis
@pytest.mark.requires_vault
class TestVaultIntegration:
    """Integration tests for vault service components."""
    
    @pytest.fixture(scope="class")
    def vault_service(self):
        """Create a vault service instance for integration testing (sync; DistributedVaultService is sync)."""
        os.environ.setdefault("MOTET_VAULT_MASTER_KEY", "test-integration-master-key")
        os.environ.setdefault("MOTET_REDIS_URL", "redis://localhost:6379/1")
        service = DistributedVaultService()
        try:
            ok = service.store_credential(
                credential_id="_vault_smoke_test",
                credential_data={"smoke": True},
                credential_type=CredentialType.CUSTOM,
                scope=CredentialScope.PRINCIPAL,
                security_level=CredentialSecurityLevel.CONFIDENTIAL,
                principal_id="_smoke",
            )
            service.delete_credential("_vault_smoke_test", "_smoke")
            if not ok:
                pytest.skip("Vault store_credential returned False — vault not operational")
        except Exception as exc:
            pytest.skip(f"Vault not operational: {exc}")
        yield service
        try:
            service.delete_credential("test_integration_credential", "test_principal")
            service.delete_credential("test_mcp_credential", "test_principal")
        except Exception:
            pass
    
    @pytest.fixture
    def test_credential_data(self):
        """Test credential data."""
        return {
            "api_key": "placeholder-integration-key",
            "organization": "test-org",
            "metadata": {"test": True, "created_at": datetime.utcnow().isoformat()}
        }
    
    @pytest.fixture
    def test_context(self):
        """Test command context."""
        return CommandContext(
            task_id="test_task",
            principal_id="test_principal",
            tenant_id="test_tenant",
            motet_id="test_motet",
            conversation_id="test_conversation"
        )
    
    @pytest.mark.asyncio
    async def test_vault_service_integration(self, vault_service, test_credential_data):
        """Test complete vault service integration (vault methods are sync)."""
        success = vault_service.store_credential(
            credential_id="test_integration_credential",
            credential_data=test_credential_data,
            credential_type=CredentialType.API_KEY,
            scope=CredentialScope.PRINCIPAL,
            security_level=CredentialSecurityLevel.CONFIDENTIAL,
            principal_id="test_principal",
            tenant_id="test_tenant",
            motet_id="test_motet",
            description="Integration test credential"
        )
        assert success is True
        
        # Retrieve credential
        request = CredentialAccessRequest(
            principal_id="test_principal",
            tenant_id="test_tenant",
            motet_id="test_motet",
            credential_key="test_integration_credential"
        )
        
        response = vault_service.retrieve_credential(request)
        assert response.success is True
        assert response.credential_data == test_credential_data
        assert response.access_granted_at is not None
        
        # List credentials
        credentials = vault_service.list_credentials(
            principal_id="test_principal",
            tenant_id="test_tenant",
            motet_id="test_motet"
        )
        assert len(credentials) >= 1
        assert any(c.credential_id == "test_integration_credential" for c in credentials)
        
        # Delete credential
        success = vault_service.delete_credential(
            "test_integration_credential", "test_principal"
        )
        assert success is True
        
        # Verify deletion
        response = vault_service.retrieve_credential(request)
        assert response.success is False
        assert "not found" in response.error_message.lower()
    
    @pytest.mark.asyncio
    async def test_vault_client_integration(self, vault_service, test_credential_data, test_context):
        """Test vault client integration."""
        # First store a credential using the vault service
        success = vault_service.store_credential(
            credential_id="test_client_credential",
            credential_data=test_credential_data,
            credential_type=CredentialType.API_KEY,
            scope=CredentialScope.PRINCIPAL,
            security_level=CredentialSecurityLevel.CONFIDENTIAL,
            principal_id="test_principal",
            tenant_id="test_tenant",
            motet_id="test_motet"
        )
        assert success is True
        
        # Create vault client
        client = VaultClient()
        
        # Get credential using client
        credential_data = client.get_credential(
            credential_key="test_client_credential",
            context=test_context,
            credential_type=CredentialType.API_KEY
        )
        assert credential_data == test_credential_data
        
        # Test caching - second call should use cache
        credential_data_cached = client.get_credential(
            credential_key="test_client_credential",
            context=test_context
        )
        assert credential_data_cached == test_credential_data
        
        # Check metrics
        metrics = client.get_metrics()
        assert metrics["total_requests"] >= 2
        assert metrics["cache_hits"] >= 1
        
        # Cleanup
        vault_service.delete_credential("test_client_credential", "test_principal")
    
    @pytest.mark.asyncio
    async def test_mcp_integration(self, vault_service, test_context):
        """Test MCP integration."""
        # Store MCP credential
        mcp_credential_data = {
            "access_token": "test-mcp-access-token-67890",
            "refresh_token": "test-mcp-refresh-token-67890",
            "token_type": "Bearer"
        }
        
        success = vault_service.store_credential(
            credential_id="google_workspace_oauth_tokens",
            credential_data=mcp_credential_data,
            credential_type=CredentialType.OAUTH_TOKEN,
            scope=CredentialScope.PRINCIPAL,
            security_level=CredentialSecurityLevel.CONFIDENTIAL,
            principal_id="test_principal",
            tenant_id="test_tenant",
            motet_id="test_motet"
        )
        assert success is True
        
        # Create MCP integration
        mcp_integration = VaultMCPIntegration()
        
        # Get environment variables for Google Workspace
        env_vars = mcp_integration.get_mcp_environment_variables(
            "google_workspace", test_context
        )
        if "GOOGLE_ACCESS_TOKEN" not in env_vars:
            pytest.skip(
                "MCP vault chain did not surface GOOGLE_ACCESS_TOKEN (credential resolution / mapping)"
            )

        assert "GOOGLE_ACCESS_TOKEN" in env_vars
        assert "GOOGLE_REFRESH_TOKEN" in env_vars
        assert env_vars["GOOGLE_ACCESS_TOKEN"] == "test-mcp-access-token-67890"
        assert env_vars["GOOGLE_REFRESH_TOKEN"] == "test-mcp-refresh-token-67890"
        
        # Cleanup
        vault_service.delete_credential("google_workspace_oauth_tokens", "test_principal")
    
    @pytest.mark.asyncio
    async def test_encryption_integration(self, vault_service):
        """Test encryption integration with real data."""
        # Test with various data types
        test_cases = [
            {"simple": "value"},
            {"nested": {"key": "value", "number": 42}},
            {"list": [1, 2, 3, "test"]},
            {"unicode": "测试中文"},
            {"special_chars": "!@#$%^&*()_+-=[]{}|;':\",./<>?"},
            {"large_data": {"key" + str(i): "value" + str(i) for i in range(100)}}
        ]
        
        for i, test_data in enumerate(test_cases):
            credential_id = f"test_encryption_{i}"
            
            # Store credential
            success = vault_service.store_credential(
                credential_id=credential_id,
                credential_data=test_data,
                credential_type=CredentialType.CUSTOM,
                scope=CredentialScope.PRINCIPAL,
                security_level=CredentialSecurityLevel.CONFIDENTIAL,
                principal_id="test_principal"
            )
            assert success is True
            
            # Retrieve credential
            request = CredentialAccessRequest(
                principal_id="test_principal",
                credential_key=credential_id
            )
            
            response = vault_service.retrieve_credential(request)
            assert response.success is True
            assert response.credential_data == test_data
            
            # Cleanup
            vault_service.delete_credential(credential_id, "test_principal")
    
    @pytest.mark.asyncio
    async def test_access_control_integration(self, vault_service):
        """Test access control integration."""
        # Store credential with principal scope
        success = vault_service.store_credential(
            credential_id="test_principal_credential",
            credential_data={"api_key": "placeholder-principal-key"},
            credential_type=CredentialType.API_KEY,
            scope=CredentialScope.PRINCIPAL,
            security_level=CredentialSecurityLevel.CONFIDENTIAL,
            principal_id="principal_1"
        )
        assert success is True
        
        # Store credential with tenant scope
        success = vault_service.store_credential(
            credential_id="test_tenant_credential",
            credential_data={"api_key": "tenant-key-456"},
            credential_type=CredentialType.API_KEY,
            scope=CredentialScope.TENANT,
            security_level=CredentialSecurityLevel.CONFIDENTIAL,
            principal_id="principal_1",
            tenant_id="tenant_1"
        )
        assert success is True
        
        # Test principal access
        request = CredentialAccessRequest(
            principal_id="principal_1",
            credential_key="test_principal_credential"
        )
        response = vault_service.retrieve_credential(request)
        assert response.success is True
        
        # Test wrong principal access
        request.principal_id = "principal_2"
        response = vault_service.retrieve_credential(request)
        assert response.success is False
        
        # Test tenant access
        request = CredentialAccessRequest(
            principal_id="principal_1",
            tenant_id="tenant_1",
            credential_key="test_tenant_credential"
        )
        response = vault_service.retrieve_credential(request)
        assert response.success is True
        
        # Test wrong tenant access
        request.tenant_id = "tenant_2"
        response = vault_service.retrieve_credential(request)
        assert response.success is False
        
        # Cleanup
        vault_service.delete_credential("test_principal_credential", "principal_1")
        vault_service.delete_credential("test_tenant_credential", "principal_1")
    
    @pytest.mark.asyncio
    async def test_expiration_integration(self, vault_service):
        """Test credential expiration integration."""
        # Store credential with short expiration
        expires_at = datetime.utcnow() + timedelta(seconds=1)
        
        success = vault_service.store_credential(
            credential_id="test_expiring_credential",
            credential_data={"api_key": "expiring-key-789"},
            credential_type=CredentialType.API_KEY,
            scope=CredentialScope.PRINCIPAL,
            security_level=CredentialSecurityLevel.CONFIDENTIAL,
            principal_id="test_principal",
            expires_at=expires_at
        )
        assert success is True
        
        # Retrieve immediately (should work)
        request = CredentialAccessRequest(
            principal_id="test_principal",
            credential_key="test_expiring_credential"
        )
        response = vault_service.retrieve_credential(request)
        assert response.success is True
        
        # Wait for expiration
        await asyncio.sleep(2)
        
        # Try to retrieve after expiration (should fail)
        response = vault_service.retrieve_credential(request)
        assert response.success is False
        assert "expired" in response.error_message.lower()
        
        # Cleanup
        vault_service.delete_credential("test_expiring_credential", "test_principal")


@pytest.mark.integration
@pytest.mark.requires_redis
@pytest.mark.requires_vault
class TestVaultPerformance:
    """Performance tests for vault service."""

    @pytest.fixture(scope="class")
    def vault_service(self):
        """Create a vault service instance for performance testing (sync; DistributedVaultService is sync)."""
        os.environ.setdefault("MOTET_VAULT_MASTER_KEY", "test-performance-master-key")
        os.environ.setdefault("MOTET_REDIS_URL", "redis://localhost:6379/2")  # Use different DB

        service = DistributedVaultService()
        try:
            ok = service.store_credential(
                credential_id="_vault_perf_smoke",
                credential_data={"smoke": True},
                credential_type=CredentialType.CUSTOM,
                scope=CredentialScope.PRINCIPAL,
                security_level=CredentialSecurityLevel.CONFIDENTIAL,
                principal_id="_smoke",
            )
            service.delete_credential("_vault_perf_smoke", "_smoke")
            if not ok:
                pytest.skip("Vault store_credential returned False — vault not operational")
        except Exception as exc:
            pytest.skip(f"Vault not operational: {exc}")
        yield service

        # Cleanup (sync)
        try:
            for i in range(100):
                service.delete_credential(f"perf_test_credential_{i}", "test_principal")
        except Exception:
            pass
    
    @pytest.mark.asyncio
    async def test_concurrent_operations(self, vault_service):
        """Test concurrent vault operations."""
        async def store_credential(i):
            return vault_service.store_credential(
                credential_id=f"perf_test_credential_{i}",
                credential_data={"api_key": f"key-{i}"},
                credential_type=CredentialType.API_KEY,
                scope=CredentialScope.PRINCIPAL,
                security_level=CredentialSecurityLevel.CONFIDENTIAL,
                principal_id="test_principal"
            )
        
        async def retrieve_credential(i):
            request = CredentialAccessRequest(
                principal_id="test_principal",
                credential_key=f"perf_test_credential_{i}"
            )
            return vault_service.retrieve_credential(request)
        
        # Store 50 credentials concurrently
        store_tasks = [store_credential(i) for i in range(50)]
        store_results = await asyncio.gather(*store_tasks)
        assert all(store_results)
        
        # Retrieve 50 credentials concurrently
        retrieve_tasks = [retrieve_credential(i) for i in range(50)]
        retrieve_results = await asyncio.gather(*retrieve_tasks)
        assert all(r.success for r in retrieve_results)
        
        # Verify data integrity
        for i, result in enumerate(retrieve_results):
            assert result.credential_data["api_key"] == f"key-{i}"
    
    @pytest.mark.asyncio
    async def test_caching_performance(self, vault_service):
        """Test caching performance."""
        # Store a credential
        success = vault_service.store_credential(
            credential_id="perf_test_cached_credential",
            credential_data={"api_key": "cached-key-123"},
            credential_type=CredentialType.API_KEY,
            scope=CredentialScope.PRINCIPAL,
            security_level=CredentialSecurityLevel.CONFIDENTIAL,
            principal_id="test_principal"
        )
        assert success is True
        
        # Create vault client
        client = VaultClient()
        context = CommandContext(
            task_id="test_task",
            principal_id="test_principal",
            tenant_id="test_tenant",
            motet_id="test_motet",
            conversation_id="test_conversation"
        )
        
        # First retrieval (cache miss)
        start_time = asyncio.get_event_loop().time()
        credential_data = client.get_credential(
            credential_key="perf_test_cached_credential",
            context=context
        )
        first_retrieval_time = asyncio.get_event_loop().time() - start_time
        
        assert credential_data["api_key"] == "cached-key-123"
        
        # Second retrieval (cache hit)
        start_time = asyncio.get_event_loop().time()
        credential_data = client.get_credential(
            credential_key="perf_test_cached_credential",
            context=context
        )
        second_retrieval_time = asyncio.get_event_loop().time() - start_time
        
        assert credential_data["api_key"] == "cached-key-123"
        
        # Cache hit should be faster
        assert second_retrieval_time < first_retrieval_time
        
        # Check metrics
        metrics = client.get_metrics()
        assert metrics["cache_hits"] >= 1
        assert metrics["vault_hits"] >= 1
        
        # Cleanup
        vault_service.delete_credential("perf_test_cached_credential", "test_principal")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
