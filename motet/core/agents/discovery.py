"""
Motet - Agent Discovery Services

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-31

Description:
    Shared discovery/synchronization services for agents. Provides
    helper functions to serialize agent configs (including model, loop limits,
    skills, and metadata), sync bundle-defined agent configs from shared
    catalogs/artifacts into local process registry state, and list role-filtered
    visible agents for API/command/tool callers.

Dependencies:
    - motet.core.agents.registry: AgentConfig and registry access
    - motet.core.bundles.deploy: bundle catalog/artifact helpers
    - motet.core.distributed.redis_manager: Redis client for shared catalog reads

Usage:
    from motet.core.agents.discovery import list_visible_agents
    agents = list_visible_agents(principal_roles=["admin"])

Notes:
    - Sync behavior is process-local; it updates the current process registry.
    - Listing behavior expects registry to already be hydrated during deploy/startup.
"""

from __future__ import annotations

import io
import tarfile
from typing import Any, Dict, List, Optional, Set

import structlog

from .registry import AgentConfig, get_agent_registry

logger = structlog.get_logger(__name__)


def serialize_agent_config(cfg: AgentConfig) -> Dict[str, Any]:
    """Convert AgentConfig to API-safe dict."""
    bundle_id = getattr(cfg, "bundle_id", None)
    bare_agent_id = str(getattr(cfg, "agent_id", "") or "")
    qualified_id = f"{bundle_id}.{bare_agent_id}" if bundle_id else f"core.{bare_agent_id}"
    tool_filter = getattr(cfg, "tool_filter", None)
    turn_hooks = getattr(cfg, "turn_hooks", None)
    config_surfaces = getattr(cfg, "allowed_surface_ids", None)
    effective_surfaces: Optional[List[str]] = None
    try:
        from motet.core.surfaces import resolve_effective_allowlist

        effective_surfaces = resolve_effective_allowlist(
            qualified_agent_id=qualified_id,
            config_allowed_surface_ids=(
                list(config_surfaces) if config_surfaces is not None else None
            ),
        )
    except Exception as e:
        logger.warning(
            "agent_surface_allowlist_resolve_failed",
            qualified_id=qualified_id,
            error=str(e),
        )
        if isinstance(config_surfaces, list) and config_surfaces:
            effective_surfaces = [str(x) for x in config_surfaces if str(x).strip()]
        else:
            effective_surfaces = None
    reasoning_effort = getattr(cfg, "reasoning_effort", None)
    metadata = getattr(cfg, "metadata", None)
    skill_ids = getattr(cfg, "skill_ids", None)
    return {
        "qualified_id": qualified_id,
        "agent_id": bare_agent_id,
        "bundle_id": bundle_id,
        "display_name": str(getattr(cfg, "display_name", "") or ""),
        "description": str(getattr(cfg, "description", "") or ""),
        "allowed_roles": list(getattr(cfg, "allowed_roles", ["*"]) or ["*"]),
        "selectable": bool(getattr(cfg, "selectable", True)),
        "aliases": list(getattr(cfg, "aliases", []) or []),
        "system_prompt": str(getattr(cfg, "system_prompt", "") or ""),
        "tool_filter": tool_filter.model_dump() if tool_filter is not None else {},
        "turn_hooks": turn_hooks.model_dump() if turn_hooks is not None else {},
        # null means all catalog surfaces (ADR-0083 / surfaces catalog)
        "allowed_surface_ids": effective_surfaces,
        "model_provider": getattr(cfg, "model_provider", None),
        "model_name": getattr(cfg, "model_name", None),
        "model_profile_name": getattr(cfg, "model_profile_name", None),
        "temperature": float(getattr(cfg, "temperature", 0.2)),
        "max_iterations": int(getattr(cfg, "max_iterations", 20)),
        "max_model_calls": getattr(cfg, "max_model_calls", None),
        "max_tools": int(getattr(cfg, "max_tools", 20)),
        "enable_thinking": bool(getattr(cfg, "enable_thinking", False)),
        "reasoning_effort": str(reasoning_effort) if reasoning_effort is not None else None,
        "conversation_id_prefix": getattr(cfg, "conversation_id_prefix", None),
        "metadata": dict(metadata) if isinstance(metadata, dict) else None,
        "skill_ids": list(skill_ids) if isinstance(skill_ids, list) else None,
        "skill_mode": str(getattr(cfg, "skill_mode", "allowlist") or "allowlist"),
        "skill_max_per_turn": int(getattr(cfg, "skill_max_per_turn", 3)),
        "output_contract": (
            cfg.output_contract.model_dump()
            if getattr(cfg, "output_contract", None) is not None
            else None
        ),
        "handoffs": list(getattr(cfg, "handoffs", None) or []),
    }


def sync_bundle_agents_into_registry() -> int:
    """
    Sync bundle-defined agents from Redis catalogs/artifacts into local registry.

    Returns the number of agent configs registered from bundle catalogs.
    """
    try:
        from ..distributed.redis_manager import get_sync_redis_client
        from motet.core.bundles.deploy import (
            _extract_bundle_agents_configs,
            _fetch_artifact,
            _list_all_catalogs,
        )

        redis_client = get_sync_redis_client("agent_list_command")
        catalogs = _list_all_catalogs(redis_client) or {}
        registry = get_agent_registry()

        catalog_bundle_ids: Set[str] = set(catalogs.keys())
        local_bundle_ids: Set[str] = {
            str(getattr(cfg, "bundle_id", "") or "")
            for cfg in registry.list()
            if getattr(cfg, "bundle_id", None)
        }

        for stale_bundle_id in sorted(local_bundle_ids - catalog_bundle_ids):
            registry.unregister_bundle(stale_bundle_id)

        synced = 0
        for bundle_id, catalog in sorted(catalogs.items()):
            raw_configs = catalog.get("agent_configs") or []
            parsed_configs: List[Dict[str, Any]] = []

            if isinstance(raw_configs, list) and raw_configs:
                parsed_configs = [cfg for cfg in raw_configs if isinstance(cfg, dict)]
            else:
                bundle_version = str(catalog.get("bundle_version") or "").strip()
                if bundle_version:
                    artifact = _fetch_artifact(redis_client, bundle_id, bundle_version)
                    if artifact:
                        bundle_files: Dict[str, bytes] = {}
                        try:
                            with tarfile.open(fileobj=io.BytesIO(artifact), mode="r:gz") as tar:
                                for member in tar.getmembers():
                                    if not member.isfile():
                                        continue
                                    name = member.name
                                    if name.startswith("/") or ".." in name:
                                        continue
                                    fh = tar.extractfile(member)
                                    if fh is None:
                                        continue
                                    bundle_files[name] = fh.read()
                            parsed_configs = _extract_bundle_agents_configs(
                                bundle_id=bundle_id,
                                bundle_files=bundle_files,
                                strict=False,
                            )
                        except Exception as e:
                            logger.warning(
                                "agent_catalog_artifact_parse_failed",
                                bundle_id=bundle_id,
                                bundle_version=bundle_version,
                                error=str(e),
                            )

            registry.unregister_bundle(bundle_id)
            for raw_cfg in parsed_configs:
                candidate = dict(raw_cfg)
                candidate["bundle_id"] = bundle_id
                try:
                    registry.register_agent(AgentConfig(**candidate))
                    synced += 1
                except Exception as e:
                    logger.warning(
                        "agent_catalog_register_failed",
                        bundle_id=bundle_id,
                        error=str(e),
                    )
        return synced
    except Exception as e:
        logger.warning("agent_catalog_sync_failed", error=str(e))
        return 0


def principal_may_access_agent(
    agent_config: AgentConfig,
    principal_roles: Optional[List[str]] = None,
) -> bool:
    """Return whether a principal's roles may use or list an agent.

    Same predicate as ``agent_turn``: ``allowed_roles`` containing ``*``
    is unrestricted; otherwise the principal must share at least one role.
    """
    allowed_roles = list(getattr(agent_config, "allowed_roles", ["*"]) or ["*"])
    roles = list(principal_roles or [])
    if "*" in allowed_roles:
        return True
    return any(role in allowed_roles for role in roles)


def list_visible_agents(principal_roles: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Return role-filtered agent records from the current local registry state."""
    roles = list(principal_roles or [])
    out: List[Dict[str, Any]] = []
    for cfg in get_agent_registry().list():
        if not principal_may_access_agent(cfg, roles):
            continue
        out.append(serialize_agent_config(cfg))
    out.sort(key=lambda item: str(item.get("qualified_id", "")))
    return out


__all__ = [
    "serialize_agent_config",
    "sync_bundle_agents_into_registry",
    "list_visible_agents",
    "principal_may_access_agent",
]

