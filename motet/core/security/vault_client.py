"""
Motet - Vault Client

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    High-performance vault client for distributed workers in the Motet
    distributed framework. Provides local caching, automatic retry with
    exponential backoff, and integration with command context and principal
    system. Includes metrics collection and thread-safe operations.

Dependencies:
    - asyncio: Asynchronous vault operations
    - time: Timestamp and caching management
    - pydantic: Data validation and model definitions
    - structlog: Structured logging
    - typing: Type hints and annotations
    - Vault service and command context

Usage:
    from motet.core.security.vault_client import VaultClient, VaultClientConfig
    
    # Create client
    client = VaultClient(VaultClientConfig(cache_ttl_seconds=300))
    
    # Get credentials
    credentials = await client.get_credentials(
        principal_id="user123",
        credential_key="api_key",
        credential_type=CredentialType.API_KEY
    )

Notes:
    - Provides high-performance vault client for distributed workers
    - Includes local caching for frequently accessed credentials
    - Supports automatic retry with exponential backoff
    - Integrates with command context and principal system
    - Includes metrics collection and monitoring
    - Supports thread-safe operations and concurrent access
    - Integrates with distributed vault service and security system
"""


import asyncio
import os
import time
from typing import Dict, Any, Optional, List
from pydantic import BaseModel
import structlog

from .vault_service import (
    DistributedVaultService,
    CredentialAccessRequest,
    CredentialAccessResponse,
    CredentialType,
    CredentialScope,
    CredentialSecurityLevel
)
from motet.core.commands.base import CommandContext
from ..types import Principal
from ..workers.concurrency_primitives import worker_sleep

from .system_principals import SYSTEM_PRINCIPAL_VAULT_CLIENT

logger = structlog.get_logger(__name__)


class VaultClientConfig(BaseModel):
    """Configuration for the vault client."""
    cache_ttl_seconds: int = 300  # 5 minutes
    max_retries: int = 3
    retry_delay_seconds: float = 1.0
    enable_caching: bool = True
    enable_metrics: bool = True


class VaultClient:
    """
    High-performance vault client for distributed workers.
    
    Features:
    - Local caching for frequently accessed credentials
    - Automatic retry with exponential backoff
    - Integration with command context and principal system
    - Metrics collection for monitoring
    - Thread-safe operations
    """
    
    def __init__(self, config: Optional[VaultClientConfig] = None):
        self.config = config or VaultClientConfig()
        self.vault_service = DistributedVaultService()
        self._local_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_timestamps: Dict[str, float] = {}
        self._access_metrics: Dict[str, int] = {}
        
    def _make_cache_key(self, principal_id: str, credential_key: str) -> str:
        """Generate cache key for local caching."""
        return f"{principal_id}:{credential_key}"
    
    def _is_cache_valid(self, cache_key: str) -> bool:
        """Check if cached credential is still valid."""
        if not self.config.enable_caching:
            return False
        
        if cache_key not in self._cache_timestamps:
            return False
        
        cache_age = time.time() - self._cache_timestamps[cache_key]
        return cache_age < self.config.cache_ttl_seconds
    
    def _get_from_cache(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """Get credential from local cache if valid."""
        if self._is_cache_valid(cache_key):
            if self.config.enable_metrics:
                self._access_metrics["cache_hits"] = self._access_metrics.get("cache_hits", 0) + 1
            return self._local_cache.get(cache_key)
        
        # Remove expired cache entry
        if cache_key in self._local_cache:
            del self._local_cache[cache_key]
        if cache_key in self._cache_timestamps:
            del self._cache_timestamps[cache_key]
        
        return None
    
    def _store_in_cache(self, cache_key: str, credential_data: Dict[str, Any]) -> None:
        """Store credential in local cache."""
        if self.config.enable_caching:
            self._local_cache[cache_key] = credential_data
            self._cache_timestamps[cache_key] = time.time()
    
    def get_credential(
        self,
        credential_key: str,
        context: CommandContext,
        credential_type: Optional[CredentialType] = None,
        required_scopes: Optional[List[CredentialScope]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Get a credential for the current command context.
        
        Args:
            credential_key: Key identifying the credential to retrieve
            context: Command execution context with principal information
            credential_type: Optional credential type for validation
            required_scopes: Optional list of required scopes
            
        Returns:
            Credential data dictionary or None if not found/unauthorized
        """
        try:
            # Check local cache first
            cache_key = self._make_cache_key(context.principal_id, credential_key)
            cached_credential = self._get_from_cache(cache_key)
            
            if cached_credential:
                logger.debug("Credential retrieved from local cache",
                           credential_key=credential_key,
                           principal_id=context.principal_id)
                if self.config.enable_metrics:
                    self._access_metrics["total_requests"] = self._access_metrics.get("total_requests", 0) + 1
                return cached_credential
            
            # Create access request
            request = CredentialAccessRequest(
                principal_id=context.principal_id,
                tenant_id=context.tenant_id,
                credential_key=credential_key,
                required_scopes=required_scopes or []
            )
            
            # Retry logic with exponential backoff
            last_exception = None
            for attempt in range(self.config.max_retries):
                try:
                    response = self.vault_service.retrieve_credential(request)
                    
                    if response.success and response.credential_data:
                        # Store in local cache
                        self._store_in_cache(cache_key, response.credential_data)
                        
                        # Update metrics
                        if self.config.enable_metrics:
                            self._access_metrics["vault_hits"] = self._access_metrics.get("vault_hits", 0) + 1
                            self._access_metrics["total_requests"] = self._access_metrics.get("total_requests", 0) + 1
                        
                        logger.info("Credential retrieved successfully",
                                   credential_key=credential_key,
                                   principal_id=context.principal_id,
                                   attempt=attempt + 1)
                        
                        return response.credential_data
                    else:
                        # Expected when callers fall back to env/config keys
                        # (e.g. deepseek_api_key via MOTET_DEEPSEEK_API_KEY).
                        logger.debug(
                            "Credential access denied or not found",
                            credential_key=credential_key,
                            principal_id=context.principal_id,
                            error=response.error_message,
                        )
                        return None
                
                except Exception as e:
                    last_exception = e
                    if attempt < self.config.max_retries - 1:
                        delay = self.config.retry_delay_seconds * (2 ** attempt)
                        logger.warning("Credential retrieval failed, retrying",
                                     credential_key=credential_key,
                                     principal_id=context.principal_id,
                                     attempt=attempt + 1,
                                     delay=delay,
                                     error=str(e))
                        worker_sleep(delay)
                    else:
                        logger.error("Credential retrieval failed after all retries",
                                   credential_key=credential_key,
                                   principal_id=context.principal_id,
                                   error=str(e))
            
            # Update metrics for failed requests
            if self.config.enable_metrics:
                self._access_metrics["vault_errors"] = self._access_metrics.get("vault_errors", 0) + 1
                self._access_metrics["total_requests"] = self._access_metrics.get("total_requests", 0) + 1
            
            return None
            
        except Exception as e:
            logger.error("Unexpected error in credential retrieval",
                        credential_key=credential_key,
                        principal_id=context.principal_id,
                        error=str(e),
                        exc_info=True)
            return None
    
    def get_bearer_token(
        self,
        service_name: str,
        context: CommandContext
    ) -> Optional[str]:
        """
        Convenience method to get a bearer token for a specific service.
        
        Args:
            service_name: Name of the service (e.g., "google_workspace", "github")
            context: Command execution context
            
        Returns:
            Bearer token string or None if not found
        """
        credential_data = self.get_credential(
            credential_key=f"{service_name}_bearer_token",
            context=context,
            credential_type=CredentialType.BEARER_TOKEN
        )
        
        if credential_data:
            return credential_data.get("access_token") or credential_data.get("token")
        
        return None
    
    def get_api_key(
        self,
        service_name: str,
        context: CommandContext
    ) -> Optional[str]:
        """
        Convenience method to get an API key for a specific service.
        
        Args:
            service_name: Name of the service (e.g., "openai", "anthropic")
            context: Command execution context
            
        Returns:
            API key string or None if not found
        """
        credential_data =  self.get_credential(
            credential_key=f"{service_name}_api_key",
            context=context,
            credential_type=CredentialType.API_KEY
        )
        
        if credential_data:
            return credential_data.get("api_key") or credential_data.get("key")
        
        return None
    
    def get_oauth_tokens(
        self,
        service_name: str,
        context: CommandContext
    ) -> Optional[Dict[str, Any]]:
        """
        Convenience method to get OAuth tokens for a specific service.
        
        Args:
            service_name: Name of the service (e.g., "google_workspace")
            context: Command execution context
            
        Returns:
            OAuth token data dictionary or None if not found
        """
        credential_data =  self.get_credential(
            credential_key=f"{service_name}_oauth_tokens",
            context=context,
            credential_type=CredentialType.OAUTH_TOKEN
        )
        
        return credential_data
    
    def store_credential(
        self,
        credential_key: str,
        credential_data: Dict[str, Any],
        context: CommandContext,
        credential_type: CredentialType = CredentialType.CUSTOM,
        scope: CredentialScope = CredentialScope.PRINCIPAL,
        security_level: CredentialSecurityLevel = CredentialSecurityLevel.CONFIDENTIAL
    ) -> bool:
        """
        Store a credential in the vault.
        
        Args:
            credential_key: Key identifying the credential
            credential_data: The credential data to store
            context: Command execution context
            credential_type: Type of credential
            scope: Access scope for the credential
            security_level: Security classification level
            
        Returns:
            True if credential was stored successfully
        """
        try:
            success =  self.vault_service.store_credential(
                credential_id=credential_key,
                credential_data=credential_data,
                credential_type=credential_type,
                scope=scope,
                security_level=security_level,
                principal_id=context.principal_id,
                tenant_id=context.tenant_id
            )
            
            if success:
                # Clear local cache for this credential
                cache_key = self._make_cache_key(context.principal_id, credential_key)
                if cache_key in self._local_cache:
                    del self._local_cache[cache_key]
                if cache_key in self._cache_timestamps:
                    del self._cache_timestamps[cache_key]
                
                logger.info("Credential stored successfully",
                           credential_key=credential_key,
                           principal_id=context.principal_id)
            
            return success
            
        except Exception as e:
            logger.error("Failed to store credential",
                        credential_key=credential_key,
                        principal_id=context.principal_id,
                        error=str(e))
            return False
    
    def delete_credential(
        self,
        credential_key: str,
        context: CommandContext
    ) -> bool:
        """
        Delete a credential from the vault.
        
        Args:
            credential_key: Key identifying the credential
            context: Command execution context with principal/tenant info
            
        Returns:
            True if credential was deleted, False if not found or error
        """
        try:
            # For global credentials (empty principal_id), use empty string
            # which matches how they were stored
            principal_id = context.principal_id or ""
            
            # Delete from vault storage
            success = self.vault_service.delete_credential(
                credential_id=credential_key,
                principal_id=principal_id,
                tenant_id=context.tenant_id,
            )
            
            # Clear from local cache (try both with and without principal)
            cache_keys_to_clear = [
                self._make_cache_key(principal_id, credential_key),
                self._make_cache_key("", credential_key),
            ]
            for cache_key in cache_keys_to_clear:
                if cache_key in self._local_cache:
                    del self._local_cache[cache_key]
                if cache_key in self._cache_timestamps:
                    del self._cache_timestamps[cache_key]
            
            logger.info("Credential deleted",
                       credential_key=credential_key,
                       principal_id=principal_id or SYSTEM_PRINCIPAL_VAULT_CLIENT,
                       success=success)
            
            return success
            
        except Exception as e:
            logger.error("Failed to delete credential",
                        credential_key=credential_key,
                        principal_id=context.principal_id,
                        error=str(e))
            return False
    
    def clear_cache(self, principal_id: Optional[str] = None) -> None:
        """
        Clear the local cache.
        
        Args:
            principal_id: Optional principal ID to clear cache for specific principal only
        """
        if principal_id:
            # Clear cache for specific principal
            keys_to_remove = [key for key in self._local_cache.keys() if key.startswith(f"{principal_id}:")]
            for key in keys_to_remove:
                del self._local_cache[key]
                if key in self._cache_timestamps:
                    del self._cache_timestamps[key]
        else:
            # Clear entire cache
            self._local_cache.clear()
            self._cache_timestamps.clear()
        
        logger.info("Cache cleared", principal_id=principal_id)

    def clear_cached_credential(self, principal_id: str, credential_key: str) -> None:
        """
        Clear a specific cached credential entry.

        This is safer than `clear_cache(principal_id)` when we only want to evict
        a single credential_key (e.g. oauth token keys) without wiping every
        cached credential for the principal.

        Notes:
        - VaultClient caches by "{principal_id}:{credential_key}" (see _make_cache_key).
        - Passing credential_key into clear_cache() is a common bug; clear_cache()
          expects a principal_id, not a credential_key.
        """
        cache_key = self._make_cache_key(principal_id, credential_key)
        removed = False
        if cache_key in self._local_cache:
            del self._local_cache[cache_key]
            removed = True
        if cache_key in self._cache_timestamps:
            del self._cache_timestamps[cache_key]
            removed = True or removed

        logger.info(
            "Cached credential cleared",
            principal_id=principal_id,
            credential_key=credential_key,
            removed=removed,
        )
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get client performance metrics."""
        if not self.config.enable_metrics:
            return {"metrics_disabled": True}
        
        total_requests = self._access_metrics.get("total_requests", 0)
        cache_hits = self._access_metrics.get("cache_hits", 0)
        vault_hits = self._access_metrics.get("vault_hits", 0)
        vault_errors = self._access_metrics.get("vault_errors", 0)
        
        cache_hit_rate = (cache_hits / total_requests * 100) if total_requests > 0 else 0
        error_rate = (vault_errors / total_requests * 100) if total_requests > 0 else 0
        
        return {
            "total_requests": total_requests,
            "cache_hits": cache_hits,
            "vault_hits": vault_hits,
            "vault_errors": vault_errors,
            "cache_hit_rate_percent": round(cache_hit_rate, 2),
            "error_rate_percent": round(error_rate, 2),
            "cache_size": len(self._local_cache),
            "cache_ttl_seconds": self.config.cache_ttl_seconds
        }


# Global vault client instance for easy access
_vault_client: Optional[VaultClient] = None


def get_vault_client(config: Optional[VaultClientConfig] = None) -> VaultClient:
    """
    Get the global vault client instance.

    ADR-0095: when MOTET_VAULT_RESOLVE_URL is set (local workers), returns an
    HttpVaultClient that resolves credentials via the cloud API.  This is the
    catch-all so every code path that calls get_vault_client() on a local
    worker avoids instantiating DistributedVaultService (which needs the
    master encryption key).
    
    Args:
        config: Optional configuration for the client
        
    Returns:
        VaultClient instance (or HttpVaultClient on local workers)
    """
    global _vault_client
    
    if _vault_client is None:
        if os.getenv("MOTET_VAULT_RESOLVE_URL", "").strip():
            from motet.core.edge.http_vault_client import HttpVaultClient
            _vault_client = HttpVaultClient()  # type: ignore[assignment]
        else:
            _vault_client = VaultClient(config)

    assert _vault_client is not None
    return _vault_client


# Convenience functions for common operations
def get_credential_for_context(
    credential_key: str,
    context: CommandContext,
    credential_type: Optional[CredentialType] = None
) -> Optional[Dict[str, Any]]:
    """
    Convenience function to get a credential for a command context.
    
    Args:
        credential_key: Key identifying the credential
        context: Command execution context
        credential_type: Optional credential type for validation
        
    Returns:
        Credential data or None if not found
    """
    client = get_vault_client()
    return  client.get_credential(credential_key, context, credential_type)


def get_bearer_token_for_context(
    service_name: str,
    context: CommandContext
) -> Optional[str]:
    """
    Convenience function to get a bearer token for a service.
    
    Args:
        service_name: Name of the service
        context: Command execution context
        
    Returns:
        Bearer token string or None if not found
    """
    client = get_vault_client()
    return  client.get_bearer_token(service_name, context)


def get_api_key_for_context(
    service_name: str,
    context: CommandContext
) -> Optional[str]:
    """
    Convenience function to get an API key for a service.
    
    Args:
        service_name: Name of the service
        context: Command execution context
        
    Returns:
        API key string or None if not found
    """
    client = get_vault_client()
    return  client.get_api_key(service_name, context)


# Export main classes and functions
__all__ = [
    'VaultClient',
    'VaultClientConfig',
    'get_vault_client',
    'get_credential_for_context',
    'get_bearer_token_for_context',
    'get_api_key_for_context'
]

