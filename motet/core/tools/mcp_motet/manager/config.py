"""
Motet - MCP Instance Manager Configuration Models

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Pydantic models and YAML/OAuth helpers for the MCP instance manager.
    Pool size / auto-scale fields are intentionally absent: instance
    keys are identity-derived, so replica pooling cannot work until a replica
    id exists (separate ADR).

Dependencies:
    - pydantic: service and instance records
    - yaml / json: config file load
    - protocol: enums

Usage:
    from motet.core.tools.mcp_motet.manager.config import MCPInstanceConfig

    cfg = MCPInstanceConfig(service_id="weather", command="mcp-weather")

Notes:
    - ``instances=0`` means discovery-only bootstrap (no second shared process).
    - ``instances`` omitted or 1 means one identity-scoped instance for
      non-USER services after discovery. Values greater than 1 are warned
      and treated as 1.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import os

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field

from motet.core.constants import DEFAULT_REDIS_URL
from motet.core.tools.mcp_motet.protocol import (
    CredentialScope,
    LifecycleDuration,
    StateModel,
    Visibility,
)

logger = structlog.get_logger(__name__)


class AuthType(str, Enum):
    """Authentication types for MCP services."""

    OAUTH2 = "oauth2"
    API_KEY = "api_key"
    SERVICE_ACCOUNT = "service_account"
    NONE = "none"


class ServiceAuthConfig(BaseModel):
    """Authentication configuration for an MCP service."""

    type: AuthType = AuthType.NONE
    provider: Optional[str] = None
    vault_credential_key: Optional[str] = None
    env_var: Optional[str] = None
    token_field: str = "access_token"
    scopes: List[str] = Field(default_factory=list)
    auth_url: Optional[str] = None
    token_url: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None


class MCPInstanceConfig(BaseModel):
    """Configuration for a single MCP service (no replica pool)."""

    service_id: str
    transport: str = "stdio"
    command: str
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    working_dir: Optional[str] = None

    start_server: bool = False
    base_url: Optional[str] = None
    port: Optional[int] = None
    streamable_http_sse: bool = False
    startup_timeout_seconds: int = 45
    startup_probe_interval_seconds: int = 2
    use_vault_token: bool = False
    vault_credential_key: Optional[str] = None
    token_field: str = "access_token"

    state_model: StateModel = StateModel.STATELESS
    credential_scope: CredentialScope = CredentialScope.MOTET
    visibility: Visibility = Visibility.MOTET
    lifecycle_duration: LifecycleDuration = LifecycleDuration.PERMANENT
    shared_state_allowed: bool = False
    # 0 = discovery-only; omit/1 = one shared instance for non-USER services.
    instances: Optional[int] = None

    health_check_interval: int = 10
    restart_on_failure: bool = True
    instance_timeout: int = 3600

    auth: Optional[ServiceAuthConfig] = None
    presentation: Optional[Dict[str, Any]] = None
    exec_image: Optional[str] = Field(
        default=None,
        description="Docker image for MCP stdio or start_server HTTP when using container backend.",
    )

    def discovery_only(self) -> bool:
        """True when bootstrap should not spawn a second non-discovery instance."""
        return self.instances == 0 or self.visibility == Visibility.USER


class CredentialCheckResult(BaseModel):
    """Result of credential check with auth status."""

    env_vars: Dict[str, str] = Field(default_factory=dict)
    auth_required: bool = False
    auth_config: Optional[ServiceAuthConfig] = None
    missing_credential_key: Optional[str] = None


class InstanceManagerConfig(BaseModel):
    """Main configuration for Instance Manager."""

    services: List[MCPInstanceConfig] = Field(default_factory=list)
    redis_url: str = DEFAULT_REDIS_URL
    metrics_port: int = 9090
    health_port: int = 9091
    log_level: str = "INFO"
    cleanup_interval: int = 60
    instance_timeout: int = 3600


class MCPInstance(BaseModel):
    """Represents a running MCP server instance."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    instance_id: str
    service_id: str
    context_id: str
    transport: Optional[Any] = None
    proxy: Optional[Any] = None
    process: Optional[Any] = None
    created_at: float = 0.0
    last_used: float = 0.0
    is_healthy: bool = True
    last_error: Optional[str] = None
    owns_http_singleton_process: bool = False
    http_singleton_owner_instance_id: Optional[str] = None


def _load_config_file(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load YAML/JSON MCP manager config; empty dict if missing."""
    if config_path is None:
        config_path = os.getenv(
            "MCP_INSTANCE_MANAGER_CONFIG", "config/mcp_instance_manager.yaml"
        )

    config_file = Path(config_path)
    if not config_file.exists():
        logger.warning("Config file not found", config_path=config_path)
        return {}

    try:
        with open(config_file, "r") as f:
            if config_file.suffix in [".yaml", ".yml"]:
                return yaml.safe_load(f) or {}
            if config_file.suffix == ".json":
                return json.load(f) or {}
            logger.warning("Unsupported config format", suffix=config_file.suffix)
            return {}
    except Exception as e:
        logger.error(
            "Failed to load config file",
            config_path=config_path,
            error=str(e),
            exc_info=True,
        )
        return {}


def get_oauth_providers_from_config(
    config_path: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Load OAuth2 provider configs keyed by service_id."""
    config_data = _load_config_file(config_path)
    providers: Dict[str, Dict[str, Any]] = {}
    for service in config_data.get("services", []):
        service_id = service.get("service_id")
        auth = service.get("auth")
        if not service_id or not auth:
            continue
        if auth.get("type") == "oauth2":
            providers[service_id] = {
                "type": "oauth2",
                "provider": auth.get("provider", service_id),
                "display_name": auth.get(
                    "display_name", service_id.replace("_", " ").title()
                ),
                "description": auth.get("description", ""),
                "vault_credential_key": auth.get("vault_credential_key"),
                "env_var": auth.get("env_var"),
                "token_field": auth.get("token_field", "access_token"),
                "scopes": auth.get("scopes", []),
                "auth_url": auth.get("auth_url"),
                "token_url": auth.get("token_url"),
                "revoke_url": auth.get("revoke_url"),
                "tokeninfo_url": auth.get("tokeninfo_url"),
                "token_transport": auth.get("token_transport"),
                "token_query_param": auth.get("token_query_param"),
            }
    logger.debug(
        "Loaded OAuth providers from config",
        count=len(providers),
        providers=list(providers.keys()),
    )
    return providers


def apply_stdio_discovery_bearer_placeholder(
    *,
    discovery_mode: bool,
    service_id: str,
    service_config: MCPInstanceConfig,
    env_vars: Dict[str, str],
) -> None:
    """Placeholder bearer so workspace-mcp can list tools during discovery."""
    if not discovery_mode or service_id != "google_workspace":
        return
    auth_cfg = service_config.auth
    if not auth_cfg or not auth_cfg.env_var:
        return
    if "--bearer-token-mode" not in list(service_config.args or []):
        return
    ev = auth_cfg.env_var
    raw = (env_vars.get(ev) or "").strip()
    templated = raw.startswith("${") and raw.endswith("}")
    if raw and not templated:
        return
    placeholder = (os.getenv("MOTET_GOOGLE_WORKSPACE_DISCOVERY_BEARER") or "").strip() or (
        "__MOTET_MCP_DISCOVERY_PLACEHOLDER__"
    )
    env_vars[ev] = placeholder
    logger.info(
        "mcp_discovery_stdio_bearer_placeholder",
        service_id=service_id,
        env_var=ev,
    )


def normalize_server_config_dict(service_id: str, server_conf: Dict[str, Any]) -> Dict[str, Any]:
    """Accept bundle ``servers`` dicts or YAML service entries."""
    data = dict(server_conf)
    data["service_id"] = service_id
    if "command" not in data:
        raise ValueError(f"MCP server config for {service_id} requires 'command'")
    data.pop("auto_scaling", None)
    data.pop("min_instances", None)
    data.pop("max_instances", None)
    instances = data.get("instances")
    if instances is not None:
        try:
            n = int(instances)
        except (TypeError, ValueError):
            n = 1
        if n > 1:
            logger.warning(
                "mcp_instances_pool_ignored",
                service_id=service_id,
                instances=n,
                note="ADR-0058 keys cannot replica-scale; treating as 1",
            )
            data["instances"] = 1
        else:
            data["instances"] = n
    return data
