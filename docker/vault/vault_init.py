#!/usr/bin/env python3
"""
Motet - Vault Initialization Script

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-13

Description:
    Initializes the distributed vault service with default credentials and
    configuration during Docker startup. This script ensures Redis connectivity
    is available before storing credentials in the vault.

Dependencies:
    - motet.core.security.vault_service: Vault storage and encryption
    - motet.core.types: Principal models
    - redis: Connectivity via vault service
    - python-dotenv: Environment loading

Usage:
    python docker/vault/vault_init.py [--env-file .env] [--dry-run]

Notes:
    - Uses MOTET_VAULT_MASTER_KEY from environment for deterministic encryption
    - Retries Redis connectivity to avoid race conditions at startup
"""

import asyncio
import os
import sys
import argparse
import json
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from pathlib import Path

# Add the project root to the Python path
# From docker/vault/vault_init.py, go up 3 levels to reach project root
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from motet.core.security.vault_service import (
    DistributedVaultService,
    CredentialType,
    CredentialScope,
    CredentialSecurityLevel
)
from motet.core.types import Principal


class VaultInitializer:
    """Initializes the vault service with default credentials and configuration."""
    
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.vault = DistributedVaultService()
        self.initialized_credentials = []
    
    def initialize_vault(self) -> bool:
        """Initialize the vault with default credentials."""
        print("🔐 Initializing Distributed Vault Service...")
        
        try:
            # Check if vault is accessible
            self._check_vault_connectivity()
            
            # Initialize default credentials
            self._initialize_default_credentials()
            
            # Initialize MCP server credentials
            self._initialize_mcp_credentials()
            
            # Initialize system credentials
            self._initialize_system_credentials()
            
            print(f"✅ Vault initialization completed successfully!")
            print(f"   Initialized {len(self.initialized_credentials)} credentials")
            
            return True
            
        except Exception as e:
            print(f"❌ Vault initialization failed: {e}")
            return False
    
    def _check_vault_connectivity(self):
        """Check if the vault service is accessible."""
        print("   Checking vault connectivity...")
        
        if self.dry_run:
            print("   [DRY RUN] Skipping connectivity check")
            return
        
        # Try to access Redis (vault backend) with retries
        max_wait_seconds = int(os.getenv("MOTET_VAULT_INIT_REDIS_WAIT_SECONDS", "60"))
        deadline = time.time() + max_wait_seconds
        interval_seconds = 2.0

        while True:
            try:
                redis_client = self.vault.sync_redis_client
                redis_client.ping()
                print("   ✅ Redis connectivity confirmed")
                return
            except Exception as e:
                if time.time() >= deadline:
                    raise RuntimeError(f"Redis connectivity failed: {e}")
                print(f"   ⏳ Redis not ready yet, retrying in {interval_seconds:.1f}s...")
                time.sleep(interval_seconds)
                interval_seconds = min(interval_seconds * 1.5, 10.0)
    
    def _initialize_default_credentials(self):
        """Initialize default credentials from environment variables."""
        print("   Initializing default credentials...")
        
        # Default principal for system initialization
        principal_id = "system_admin"
        tenant_id = "default_tenant"
        motet_id = "default_motet"
        
        # OpenAI API Key
        openai_key = os.getenv("OPENAI_API_KEY") or os.getenv("MOTET_OPENAI_API_KEY")
        if openai_key:
            self._store_credential(
                credential_id="openai_api_key",
                credential_data={"api_key": openai_key},
                credential_type=CredentialType.API_KEY,
                scope=CredentialScope.PRINCIPAL,
                security_level=CredentialSecurityLevel.CONFIDENTIAL,
                principal_id=principal_id,
                tenant_id=tenant_id,
                motet_id=motet_id,
                description="OpenAI API key for system operations"
            )
        
        # Anthropic API Key
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if anthropic_key:
            self._store_credential(
                credential_id="anthropic_api_key",
                credential_data={"api_key": anthropic_key},
                credential_type=CredentialType.API_KEY,
                scope=CredentialScope.PRINCIPAL,
                security_level=CredentialSecurityLevel.CONFIDENTIAL,
                principal_id=principal_id,
                tenant_id=tenant_id,
                motet_id=motet_id,
                description="Anthropic API key for system operations"
            )
        
        # Google API Key
        google_key = os.getenv("GOOGLE_API_KEY")
        if google_key:
            self._store_credential(
                credential_id="google_api_key",
                credential_data={"api_key": google_key},
                credential_type=CredentialType.API_KEY,
                scope=CredentialScope.PRINCIPAL,
                security_level=CredentialSecurityLevel.CONFIDENTIAL,
                principal_id=principal_id,
                tenant_id=tenant_id,
                motet_id=motet_id,
                description="Google API key for system operations"
            )
    
    def _initialize_mcp_credentials(self):
        """Initialize MCP server credentials from environment variables."""
        print("   Initializing MCP server credentials...")
        
        principal_id = "system_admin"
        tenant_id = "default_tenant"
        motet_id = "default_motet"
        
        # Google Workspace OAuth Tokens
        google_access_token = os.getenv("GOOGLE_ACCESS_TOKEN")
        google_refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN")
        google_token_type = os.getenv("GOOGLE_TOKEN_TYPE", "Bearer")
        google_token_expiry = os.getenv("GOOGLE_TOKEN_EXPIRY")
        google_tokens_json = os.getenv("GOOGLE_TOKENS_JSON")
        
        if google_access_token and google_refresh_token:
            oauth_data = {
                "access_token": google_access_token,
                "refresh_token": google_refresh_token,
                "token_type": google_token_type,
                "expiry_date": google_token_expiry or (datetime.utcnow() + timedelta(hours=1)).isoformat()
            }
            
            if google_tokens_json:
                try:
                    tokens_data = json.loads(google_tokens_json)
                    oauth_data.update(tokens_data)
                except json.JSONDecodeError:
                    pass
            
            self._store_credential(
                credential_id="google_workspace_oauth",
                credential_data=oauth_data,
                credential_type=CredentialType.OAUTH_TOKEN,
                scope=CredentialScope.TENANT,
                security_level=CredentialSecurityLevel.CONFIDENTIAL,
                principal_id=principal_id,
                tenant_id=tenant_id,
                motet_id=motet_id,
                expires_at=datetime.utcnow() + timedelta(days=30),
                tags=["google", "workspace", "oauth"],
                description="Google Workspace OAuth tokens for MCP server"
            )
        
        # Google Service Account Key
        google_service_account_key = os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY_JSON")
        google_service_account_email = os.getenv("GOOGLE_SERVICE_ACCOUNT_EMAIL")
        google_impersonate_user = os.getenv("GOOGLE_IMPERSONATE_USER")
        
        if google_service_account_key:
            try:
                service_account_data = json.loads(google_service_account_key)
                if google_service_account_email:
                    service_account_data["client_email"] = google_service_account_email
                if google_impersonate_user:
                    service_account_data["impersonate_user"] = google_impersonate_user
                
                self._store_credential(
                    credential_id="google_service_account",
                    credential_data=service_account_data,
                    credential_type=CredentialType.SERVICE_ACCOUNT_KEY,
                    scope=CredentialScope.MOTET,
                    security_level=CredentialSecurityLevel.SECRET,
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                    motet_id=motet_id,
                    tags=["google", "service-account", "motet"],
                    description="Google Service Account key for MCP server"
                )
            except json.JSONDecodeError as e:
                print(f"   ⚠️ Invalid Google Service Account JSON: {e}")
        
        # GitHub Personal Access Token (official MCP expects GITHUB_PERSONAL_ACCESS_TOKEN)
        github_token = (
            os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")
            or os.getenv("GITHUB_TOKEN")
            or os.getenv("GITHUB_PERSONAL_TOKEN")
        )
        if github_token:
            self._store_credential(
                credential_id="github_personal_token",
                credential_data={"access_token": github_token, "token": github_token},
                credential_type=CredentialType.API_KEY,
                scope=CredentialScope.MOTET,
                security_level=CredentialSecurityLevel.CONFIDENTIAL,
                principal_id=principal_id,
                tenant_id=tenant_id,
                motet_id=motet_id,
                tags=["github", "api"],
                description="GitHub Personal Access Token for MCP server"
            )
        
        # AccuWeather API Key (for weather MCP server)
        accuweather_key = os.getenv("ACCUWEATHER_API_KEY")
        if accuweather_key:
            self._store_credential(
                credential_id="accuweather_api_key",
                credential_data={"api_key": accuweather_key},
                credential_type=CredentialType.API_KEY,
                scope=CredentialScope.PRINCIPAL,
                security_level=CredentialSecurityLevel.CONFIDENTIAL,
                principal_id=principal_id,
                tenant_id=tenant_id,
                motet_id=motet_id,
                tags=["accuweather", "weather", "api"],
                description="AccuWeather API key for weather MCP server"
            )
    
    def _initialize_system_credentials(self):
        """Initialize system-level credentials and configuration."""
        print("   Initializing system credentials...")
        
        principal_id = "system_admin"
        tenant_id = "default_tenant"
        motet_id = "default_motet"
        
        # System configuration
        system_config = {
            "vault_initialized": True,
            "initialization_time": datetime.utcnow().isoformat(),
            "version": "1.0.0",
            "environment": os.getenv("MOTET_ENVIRONMENT", "development")
        }
        
        self._store_credential(
            credential_id="system_config",
            credential_data=system_config,
            credential_type=CredentialType.CUSTOM,
            scope=CredentialScope.MOTET,
            security_level=CredentialSecurityLevel.INTERNAL,
            principal_id=principal_id,
            tenant_id=tenant_id,
            motet_id=motet_id,
            tags=["system", "configuration"],
            description="System configuration and initialization metadata"
        )
    
    def _store_credential(
        self,
        credential_id: str,
        credential_data: Dict[str, Any],
        credential_type: CredentialType,
        scope: CredentialScope,
        security_level: CredentialSecurityLevel,
        principal_id: str,
        tenant_id: str,
        motet_id: str,
        expires_at: Optional[datetime] = None,
        tags: Optional[list] = None,
        description: str = ""
    ):
        """Store a credential in the vault."""
        
        if self.dry_run:
            print(f"   [DRY RUN] Would store credential: {credential_id}")
            self.initialized_credentials.append(credential_id)
            return
        
        try:
            success = self.vault.store_credential(
                credential_id=credential_id,
                credential_data=credential_data,
                credential_type=credential_type,
                scope=scope,
                security_level=security_level,
                principal_id=principal_id,
                tenant_id=tenant_id,
                motet_id=motet_id,
                expires_at=expires_at,
                tags=tags or [],
                description=description
            )
            
            if success:
                print(f"   ✅ Stored credential: {credential_id}")
                self.initialized_credentials.append(credential_id)
            else:
                print(f"   ⚠️ Failed to store credential: {credential_id}")
                
        except Exception as e:
            print(f"   ❌ Error storing credential {credential_id}: {e}")
    
    def verify_initialization(self) -> bool:
        """Verify that the vault was initialized correctly."""
        print("   Verifying vault initialization...")
        
        if self.dry_run:
            print("   [DRY RUN] Skipping verification")
            return True
        
        try:
            # Check if system config exists
            from motet.core.security.vault_service import CredentialAccessRequest
            
            request = CredentialAccessRequest(
                principal_id="system_admin",
                tenant_id="default_tenant",
                motet_id="default_motet",
                credential_key="system_config"
            )
            
            response = self.vault.retrieve_credential(request)
            if response.success:
                print("   ✅ Vault initialization verified")
                return True
            else:
                print(f"   ❌ Vault verification failed: {response.error_message}")
                return False
                
        except Exception as e:
            print(f"   ❌ Vault verification error: {e}")
            return False


def main():
    """Main entry point for vault initialization."""
    parser = argparse.ArgumentParser(description="Initialize the distributed vault service")
    parser.add_argument("--env-file", help="Path to environment file to load")
    parser.add_argument("--dry-run", action="store_true", help="Perform a dry run without making changes")
    parser.add_argument("--verify-only", action="store_true", help="Only verify existing initialization")
    
    args = parser.parse_args()
    
    # Load environment file if specified
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file)
        print(f"Loaded environment from: {args.env_file}")
    
    # Set default environment variables
    os.environ.setdefault("MOTET_VAULT_MASTER_KEY", "docker-dev-master-key-change-in-production")
    os.environ.setdefault("MOTET_REDIS_URL", "redis://redis:6379/0")
    
    # Create initializer
    initializer = VaultInitializer(dry_run=args.dry_run)
    
    if args.verify_only:
        # Only verify existing initialization
        success = initializer.verify_initialization()
        sys.exit(0 if success else 1)
    else:
        # Initialize the vault
        success = initializer.initialize_vault()
        
        if success:
            # Verify initialization
            verification_success = initializer.verify_initialization()
            sys.exit(0 if verification_success else 1)
        else:
            sys.exit(1)


if __name__ == "__main__":
    main()
