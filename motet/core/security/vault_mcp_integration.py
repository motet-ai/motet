"""
Motet - Vault Mcp Integration

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Vault MCP Integration for the Motet distributed framework.
    Provides credential injection for MCP servers from vault storage.

    Added get_service_auth_config for OAuth prompt flow support.
    Returns auth configuration for services that require user authorization.

Dependencies:
    - typing: Type hints and annotations
    - pydantic: Data validation
    - yaml: Configuration file parsing
    - vault_client: Vault credential access
    - vault_service: Credential types and scopes

Usage:
    from motet.core.security.vault_mcp_integration import (
        VaultMCPIntegration,
        get_service_auth_config
    )
    
    # Get auth config for a service
    auth_config = get_service_auth_config("google_workspace")
    if auth_config and auth_config.get("type") == "oauth2":
        # Service requires OAuth authorization
        pass

Notes:
    - Provides credential injection for MCP servers
    - Supports OAuth, API key, and service account credentials
    - get_service_auth_config for OAuth prompt flow
    - Integrates with distributed architecture
"""


import os
import asyncio
from typing import Dict, Any, Optional, List, Union
from pydantic import BaseModel
import structlog

from .vault_client import VaultClient, get_vault_client
from .vault_service import CredentialType, CredentialScope, CredentialSecurityLevel

def _resolve_vault_client() -> VaultClient:
    """Return the appropriate vault client for the current worker mode.

    ADR-0095: local workers (MOTET_VAULT_RESOLVE_URL set) use HttpVaultClient
    which resolves credentials via the cloud API. Cloud workers use the
    standard VaultClient with direct envelope encryption.
    """
    if os.getenv("MOTET_VAULT_RESOLVE_URL", "").strip():
        from motet.core.edge.http_vault_client import HttpVaultClient
        return HttpVaultClient()  # type: ignore[return-value]
    return get_vault_client()

from .oauth_manager import (
    _make_oauth_client_credentials_key,
    _get_oauth_tokens_key_candidates
)
from motet.core.commands.base import CommandContext
from ..types import Principal

logger = structlog.get_logger(__name__)


class MCPCredentialMapping(BaseModel):
    """Mapping between MCP server and vault credentials."""
    mcp_server_id: str
    credential_keys: List[str]
    credential_types: List[CredentialType]
    required_scopes: List[CredentialScope]
    security_level: CredentialSecurityLevel
    auto_refresh: bool = True
    refresh_interval_seconds: int = 3600  # 1 hour


class VaultMCPIntegration:
    """
    Integration layer between vault service and MCP tools.
    
    Features:
    - Automatic credential injection for MCP servers
    - Environment variable mapping for MCP compatibility
    - Credential refresh and rotation support
    - Principal-based access control
    - Audit logging for MCP credential access
    """
    
    def __init__(self, vault_client: Optional[VaultClient] = None):
        self.vault_client = vault_client or _resolve_vault_client()
        self.credential_mappings: Dict[str, MCPCredentialMapping] = {}
        self._initialize_default_mappings()
    
    def _initialize_default_mappings(self):
        """Initialize default credential mappings for common MCP servers."""
        
        # Google Workspace MCP Server
        google_mapping = MCPCredentialMapping(
            mcp_server_id="google_workspace",
            credential_keys=[
                "oauth:client_credentials:google_workspace",
                "oauth:tokens:google_workspace:global",
                "oauth:tokens:google_workspace:default",
                "oauth:tokens:google_workspace",
                "google_service_account_key",
                "google_bearer_token"
            ],
            credential_types=[
                CredentialType.API_KEY,
                CredentialType.OAUTH_TOKEN,
                CredentialType.OAUTH_TOKEN,
                CredentialType.OAUTH_TOKEN,
                CredentialType.SERVICE_ACCOUNT_KEY,
                CredentialType.BEARER_TOKEN
            ],
            required_scopes=[CredentialScope.GLOBAL, CredentialScope.PRINCIPAL, CredentialScope.TENANT],
            security_level=CredentialSecurityLevel.CONFIDENTIAL,
            auto_refresh=True,
            refresh_interval_seconds=3600
        )
        self.credential_mappings["google_workspace"] = google_mapping
        
        # GitHub MCP Server
        github_mapping = MCPCredentialMapping(
            mcp_server_id="github",
            credential_keys=["github_personal_token", "github_oauth_token"],
            credential_types=[CredentialType.API_KEY, CredentialType.OAUTH_TOKEN],
            required_scopes=[CredentialScope.PRINCIPAL],
            security_level=CredentialSecurityLevel.CONFIDENTIAL
        )
        self.credential_mappings["github"] = github_mapping
        
        # OpenAI MCP Server
        openai_mapping = MCPCredentialMapping(
            mcp_server_id="openai",
            credential_keys=["openai_api_key"],
            credential_types=[CredentialType.API_KEY],
            required_scopes=[CredentialScope.PRINCIPAL, CredentialScope.TENANT],
            security_level=CredentialSecurityLevel.CONFIDENTIAL
        )
        self.credential_mappings["openai"] = openai_mapping
        
        # Anthropic MCP Server
        anthropic_mapping = MCPCredentialMapping(
            mcp_server_id="anthropic",
            credential_keys=["anthropic_api_key"],
            credential_types=[CredentialType.API_KEY],
            required_scopes=[CredentialScope.PRINCIPAL, CredentialScope.TENANT],
            security_level=CredentialSecurityLevel.CONFIDENTIAL
        )
        self.credential_mappings["anthropic"] = anthropic_mapping

        # Zoom MCP Server (ADR-0060)
        zoom_mapping = MCPCredentialMapping(
            mcp_server_id="zoom",
            credential_keys=[
                "oauth:tokens:zoom",  # Base key for user tokens (auto-expanded by _get_oauth_tokens_key_candidates)
                "oauth:client_credentials:zoom",
                "zoom_access_token",  # Legacy/Direct env var support
                "zoom_api_key"
            ],
            credential_types=[
                CredentialType.OAUTH_TOKEN,
                CredentialType.OAUTH_TOKEN,
                CredentialType.BEARER_TOKEN,
                CredentialType.API_KEY
            ],
            required_scopes=[CredentialScope.PRINCIPAL, CredentialScope.TENANT, CredentialScope.GLOBAL],
            security_level=CredentialSecurityLevel.CONFIDENTIAL,
            auto_refresh=True
        )
        self.credential_mappings["zoom"] = zoom_mapping
    
    def register_mcp_credential_mapping(self, mapping: MCPCredentialMapping) -> None:
        """Register a new credential mapping for an MCP server."""
        self.credential_mappings[mapping.mcp_server_id] = mapping
        logger.info("Registered MCP credential mapping",
                   mcp_server_id=mapping.mcp_server_id,
                   credential_keys=mapping.credential_keys)
    
    def get_mcp_environment_variables(
        self,
        mcp_server_id: str,
        context: CommandContext
    ) -> Dict[str, str]:
        """
        Get environment variables for an MCP server with credentials from vault.
        
        Credential lookup order (most specific first):
        1. Per-tenant-per-user: {key}_{tenant_id}_{principal_id}
        2. Per-user: {key}_{principal_id}
        3. Per-tenant: {key}_{tenant_id}
        4. Global: {key}_global or {key}
        
        Args:
            mcp_server_id: ID of the MCP server
            context: Command execution context
            
        Returns:
            Dictionary of environment variables for the MCP server
        """
        try:
            mapping = self.credential_mappings.get(mcp_server_id)
            if not mapping:
                logger.warning("No credential mapping found for MCP server",
                              mcp_server_id=mcp_server_id)
                return {}
            
            env_vars = {}
            principal_id = context.principal_id
            tenant_id = context.tenant_id
            motet_id = context.motet_id  # ADR-0058: Include motet_id for credential lookup
            
            logger.info("🔑 Vault credential lookup context",
                       mcp_server_id=mcp_server_id,
                       principal_id=principal_id or "(empty)",
                       tenant_id=tenant_id or "(empty)",
                       motet_id=motet_id or "(empty)")
            
            # Retrieve credentials from vault
            for i, base_credential_key in enumerate(mapping.credential_keys):
                credential_type = mapping.credential_types[i] if i < len(mapping.credential_types) else CredentialType.CUSTOM
                
                # Build list of keys to try in order of specificity
                # For OAuth tokens, try per-user/per-tenant/per-motet keys first (ADR-0058)
                keys_to_try = []
                # Check if this is an OAuth tokens key (new colon-separated format)
                is_oauth_tokens = (
                    base_credential_key.startswith("oauth:tokens:") or
                    base_credential_key == "oauth:tokens"
                )
                is_oauth_client = base_credential_key.startswith("oauth:client_credentials:")
                
                if is_oauth_tokens:
                    # OAuth tokens - use helper function to generate keys in new format (ADR-0058)
                    keys_to_try = _get_oauth_tokens_key_candidates(mcp_server_id, tenant_id, motet_id, principal_id)
                    logger.info("🔑 OAuth token key candidates",
                               mcp_server_id=mcp_server_id,
                               keys_to_try=keys_to_try,
                               tenant_id=tenant_id or "(empty)",
                               motet_id=motet_id or "(empty)",
                               principal_id=principal_id or "(empty)")
                elif is_oauth_client:
                    # OAuth client credentials - use helper function
                    keys_to_try.append(_make_oauth_client_credentials_key(mcp_server_id))
                else:
                    # Non-OAuth credentials - use original key
                    keys_to_try.append(base_credential_key)
                
                # Try each key until we find credentials
                credential_data = None
                found_key = None
                for credential_key in keys_to_try:
                    credential_data = self.vault_client.get_credential(
                        credential_key=credential_key,
                        context=context,
                        credential_type=credential_type,
                        required_scopes=mapping.required_scopes
                    )
                    if credential_data:
                        found_key = credential_key
                        break
                
                if credential_data:
                    if found_key is None:
                        logger.error(
                            "Credential data present but found_key is None",
                            mcp_server_id=mcp_server_id,
                            principal_id=principal_id,
                            tenant_id=tenant_id,
                        )
                        continue
                    # Map credential data to environment variables
                    env_vars.update(self._map_credential_to_env_vars(
                        found_key, credential_data, mcp_server_id
                    ))
                    logger.debug("Found credential for MCP server",
                               mcp_server_id=mcp_server_id,
                               credential_key=found_key,
                               principal_id=principal_id,
                               tenant_id=tenant_id)
                else:
                    logger.warning("Credential not found in vault (tried all scopes)",
                                  mcp_server_id=mcp_server_id,
                                  base_credential_key=base_credential_key,
                                  keys_tried=keys_to_try,
                                  principal_id=principal_id,
                                  tenant_id=tenant_id)
            
            logger.info("Retrieved MCP environment variables from vault",
                       mcp_server_id=mcp_server_id,
                       principal_id=principal_id,
                       tenant_id=tenant_id,
                       env_var_count=len(env_vars))
            
            return env_vars
            
        except Exception as e:
            logger.error("Failed to get MCP environment variables from vault",
                        mcp_server_id=mcp_server_id,
                        principal_id=context.principal_id,
                        tenant_id=context.tenant_id,
                        error=str(e))
            return {}
    
    def _map_credential_to_env_vars(
        self,
        credential_key: str,
        credential_data: Dict[str, Any],
        mcp_server_id: str
    ) -> Dict[str, str]:
        """
        Map credential data to environment variables for MCP server (ADR-0057 Phase 4).
        
        Uses auth config from YAML to determine env_var and token_field generically.
        """
        env_vars = {}
        
        # Get auth config from YAML
        auth_config = get_service_auth_config(mcp_server_id)
        
        # Check for OAuth client credentials (new colon-separated format)
        is_client_creds = credential_key.startswith("oauth:client_credentials:")
        
        # Check for OAuth tokens (new colon-separated format)
        is_oauth_tokens = credential_key.startswith("oauth:tokens:")
        
        if is_client_creds:
            # OAuth client credentials - extract client_id and client_secret
            # These are used by OAuth manager, not directly by MCP servers
            if "client_id" in credential_data:
                # Use service-specific env var if configured, otherwise generic
                client_id_var = f"{mcp_server_id.upper().replace('-', '_')}_OAUTH_CLIENT_ID"
                env_vars[client_id_var] = credential_data["client_id"]
            if "client_secret" in credential_data:
                client_secret_var = f"{mcp_server_id.upper().replace('-', '_')}_OAUTH_CLIENT_SECRET"
                env_vars[client_secret_var] = credential_data["client_secret"]
            # Enable insecure transport for development (OAuth over HTTP)
            env_vars["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
                
        elif is_oauth_tokens and auth_config:
            # OAuth tokens - use auth config from YAML to determine env_var and token_field
            env_var = auth_config.get("env_var")
            token_field = auth_config.get("token_field", "access_token")
            
            if env_var and token_field:
                token_value = credential_data.get(token_field)
                if token_value:
                    env_vars[env_var] = token_value
                    logger.info("Setting OAuth token as environment variable",
                               mcp_server_id=mcp_server_id,
                               env_var=env_var,
                               credential_key=credential_key)
                else:
                    logger.warning("Token field not found in credential data",
                                 mcp_server_id=mcp_server_id,
                                 token_field=token_field,
                                 credential_key=credential_key,
                                 available_keys=list(credential_data.keys()))
            else:
                logger.warning("Auth config missing env_var or token_field",
                             mcp_server_id=mcp_server_id,
                             auth_config=auth_config)
        
        elif auth_config and auth_config.get("type") == "api_key":
            # API key services (e.g., Slack, GitHub, OpenAI, Anthropic) - use auth config from YAML
            env_var = auth_config.get("env_var")
            token_field = auth_config.get("token_field", "token")
            
            if env_var:
                # Try multiple field names for flexibility
                token_value = None
                for field in [token_field, "api_key", "key", "token", "access_token"]:
                    if field in credential_data:
                        token_value = credential_data[field]
                        break
                
                # If credential_data is a string, use it directly
                if not token_value and isinstance(credential_data, str):
                    token_value = credential_data
                
                if token_value:
                    env_vars[env_var] = token_value
                    logger.info("Setting API key as environment variable",
                               mcp_server_id=mcp_server_id,
                               env_var=env_var,
                               credential_key=credential_key)
                else:
                    logger.warning("Could not extract API key from credential data",
                                 mcp_server_id=mcp_server_id,
                                 credential_key=credential_key,
                                 data_type=type(credential_data).__name__)
        
        # Handle legacy credential keys (service account, direct bearer token)
        # These don't have auth config but are still supported
        elif credential_key.endswith("_service_account_key") or credential_key.endswith(":service_account_key"):
            # Service account key format - generic handling
            if isinstance(credential_data, dict):
                import json
                service_account_var = f"{mcp_server_id.upper().replace('-', '_')}_SERVICE_ACCOUNT_KEY_JSON"
                env_vars[service_account_var] = json.dumps(credential_data)
                
                # Extract email if available
                if "client_email" in credential_data:
                    email_var = f"{mcp_server_id.upper().replace('-', '_')}_SERVICE_ACCOUNT_EMAIL"
                    env_vars[email_var] = credential_data["client_email"]
            else:
                service_account_var = f"{mcp_server_id.upper().replace('-', '_')}_SERVICE_ACCOUNT_KEY_JSON"
                env_vars[service_account_var] = str(credential_data)
        
        elif credential_key.endswith("_bearer_token") or credential_key.endswith(":bearer_token"):
            # Direct bearer token format - use auth config if available, otherwise generic
            if auth_config and auth_config.get("env_var"):
                env_var = auth_config["env_var"]
            else:
                # Fallback to service-specific env var
                env_var = f"{mcp_server_id.upper().replace('-', '_')}_BEARER_TOKEN"
            
            # Try multiple token field names
            token_value = None
            for field in ["token", "access_token"]:
                if field in credential_data:
                    token_value = credential_data[field]
                    break
            
            if not token_value and isinstance(credential_data, str):
                token_value = credential_data
            
            if token_value:
                env_vars[env_var] = token_value
            else:
                logger.warning("Could not extract bearer token from credential data",
                             credential_key=credential_key,
                             data_type=type(credential_data).__name__)
                
                logger.info("✅ Setting GOOGLE_BEARER_TOKEN for workspace-mcp bearer token mode",
                           credential_key=credential_key)
        
        else:
            # Generic mapping for unknown MCP servers
            for key, value in credential_data.items():
                if isinstance(value, str):
                    env_vars[f"{mcp_server_id.upper()}_{key.upper()}"] = value
        
        return env_vars
    
    def store_mcp_credential(
        self,
        mcp_server_id: str,
        credential_key: str,
        credential_data: Dict[str, Any],
        context: CommandContext,
        credential_type: Optional[CredentialType] = None,
        scope: Optional[CredentialScope] = None
    ) -> bool:
        """
        Store a credential for an MCP server in the vault.
        
        Args:
            mcp_server_id: ID of the MCP server
            credential_key: Key identifying the credential
            credential_data: The credential data to store
            context: Command execution context
            credential_type: Optional credential type
            scope: Optional access scope
            
        Returns:
            True if credential was stored successfully
        """
        try:
            # Get mapping to determine default values
            mapping = self.credential_mappings.get(mcp_server_id)
            
            if mapping:
                credential_type = credential_type or mapping.credential_types[0] if mapping.credential_types else CredentialType.CUSTOM
                scope = scope or mapping.required_scopes[0] if mapping.required_scopes else CredentialScope.PRINCIPAL
                security_level = mapping.security_level
            else:
                credential_type = credential_type or CredentialType.CUSTOM
                scope = scope or CredentialScope.PRINCIPAL
                security_level = CredentialSecurityLevel.CONFIDENTIAL
            
            success =  self.vault_client.store_credential(
                credential_key=credential_key,
                credential_data=credential_data,
                context=context,
                credential_type=credential_type,
                scope=scope,
                security_level=security_level
            )
            
            if success:
                logger.info("MCP credential stored successfully",
                           mcp_server_id=mcp_server_id,
                           credential_key=credential_key,
                           principal_id=context.principal_id)
            
            return success
            
        except Exception as e:
            logger.error("Failed to store MCP credential",
                        mcp_server_id=mcp_server_id,
                        credential_key=credential_key,
                        principal_id=context.principal_id,
                        error=str(e))
            return False
    
    def refresh_mcp_credentials(
        self,
        mcp_server_id: str,
        context: CommandContext
    ) -> bool:
        """
        Refresh credentials for an MCP server.
        
        Args:
            mcp_server_id: ID of the MCP server
            context: Command execution context
            
        Returns:
            True if credentials were refreshed successfully
        """
        try:
            mapping = self.credential_mappings.get(mcp_server_id)
            if not mapping or not mapping.auto_refresh:
                return False
            
            # Clear cache for this MCP server's credentials
            for credential_key in mapping.credential_keys:
                self.vault_client.clear_cache(context.principal_id)
            
            # Re-retrieve credentials to trigger refresh
            env_vars =  self.get_mcp_environment_variables(mcp_server_id, context)
            
            logger.info("MCP credentials refreshed",
                       mcp_server_id=mcp_server_id,
                       principal_id=context.principal_id,
                       env_var_count=len(env_vars))
            
            return len(env_vars) > 0
            
        except Exception as e:
            logger.error("Failed to refresh MCP credentials",
                        mcp_server_id=mcp_server_id,
                        principal_id=context.principal_id,
                        error=str(e))
            return False
    
    def get_supported_mcp_servers(self) -> List[str]:
        """Get list of MCP servers with registered credential mappings."""
        return list(self.credential_mappings.keys())
    
    def get_mcp_credential_mapping(self, mcp_server_id: str) -> Optional[MCPCredentialMapping]:
        """Get credential mapping for a specific MCP server."""
        return self.credential_mappings.get(mcp_server_id)


# Global integration instance
_vault_mcp_integration: Optional[VaultMCPIntegration] = None


def get_vault_mcp_integration() -> VaultMCPIntegration:
    """Get the global vault MCP integration instance."""
    global _vault_mcp_integration
    
    if _vault_mcp_integration is None:
        _vault_mcp_integration = VaultMCPIntegration()
    
    return _vault_mcp_integration


# Convenience functions for MCP tool integration
def get_mcp_env_vars_from_vault(
    mcp_server_id: str,
    context: CommandContext
) -> Dict[str, str]:
    """
    Convenience function to get MCP environment variables from vault.
    
    Args:
        mcp_server_id: ID of the MCP server
        context: Command execution context
        
    Returns:
        Dictionary of environment variables
    """
    integration = get_vault_mcp_integration()
    return  integration.get_mcp_environment_variables(mcp_server_id, context)


def store_mcp_credential_in_vault(
    mcp_server_id: str,
    credential_key: str,
    credential_data: Dict[str, Any],
    context: CommandContext
) -> bool:
    """
    Convenience function to store MCP credential in vault.
    
    Args:
        mcp_server_id: ID of the MCP server
        credential_key: Key identifying the credential
        credential_data: The credential data to store
        context: Command execution context
        
    Returns:
        True if credential was stored successfully
    """
    integration = get_vault_mcp_integration()
    return  integration.store_mcp_credential(
        mcp_server_id, credential_key, credential_data, context
    )


# Service auth configuration for ADR-0057 OAuth prompt flow
def get_service_auth_config(service_id: str) -> Optional[Dict[str, Any]]:
    """
    Get auth configuration for an MCP service from mcp_instance_manager.yaml (ADR-0057 Phase 4).
    
    YAML is the source of truth - no hardcoded fallbacks.
    
    Args:
        service_id: MCP service identifier
        
    Returns:
        Auth configuration dict or None if service not found or has no auth config
    """
    try:
        import yaml
        import os
        from pathlib import Path
        
        config_path = os.environ.get(
            "MCP_INSTANCE_CONFIG_PATH",
            "config/mcp_instance_manager.yaml"
        )
        
        # Also check common paths
        if not os.path.exists(config_path):
            alt_paths = [
                "/app/config/mcp_instance_manager.yaml",
                "config/mcp_instance_manager.yaml",
                "../config/mcp_instance_manager.yaml",
            ]
            for alt_path in alt_paths:
                if os.path.exists(alt_path):
                    config_path = alt_path
                    break
        
        if not os.path.exists(config_path):
            logger.debug("Config file not found",
                        service_id=service_id,
                        config_path=config_path)
            return None
        
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
        
        services = config.get("services", [])
        for service in services:
            if service.get("service_id") == service_id:
                auth_config = service.get("auth")
                if auth_config:
                    logger.debug("Found auth config in YAML",
                                service_id=service_id,
                                auth_type=auth_config.get("type"))
                    return auth_config
                else:
                    logger.debug("Service found but has no auth config",
                                service_id=service_id)
                    return None
        
        logger.debug("Service not found in config",
                    service_id=service_id)
        return None
        
    except Exception as e:
        logger.warning("Could not load auth config from YAML",
                      service_id=service_id,
                      error=str(e))
        return None


# Add method to VaultMCPIntegration class (setattr avoids pyright unknown-attribute on class)
setattr(
    VaultMCPIntegration,
    "get_service_auth_config",
    staticmethod(get_service_auth_config),
)


# Export main classes and functions
__all__ = [
    'VaultMCPIntegration',
    'MCPCredentialMapping',
    'get_vault_mcp_integration',
    'get_mcp_env_vars_from_vault',
    'store_mcp_credential_in_vault',
    'get_service_auth_config',
]

