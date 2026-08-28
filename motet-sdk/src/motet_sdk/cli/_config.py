"""
Motet - CLI Config (default API URL etc.)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-02-24

Description:
    Reads/writes ~/.motet/config.json for default API URL and other CLI settings.
    Env MOTET_API_URL or MOTET_API_URL overrides the config file.
    Also supports optional local->container workspace path mapping for hot deploy.

Dependencies:
    - pathlib, json, os

Usage:
    from motet_sdk.cli._config import (
        get_default_api_url, get_cli_config, set_cli_config_value,
        map_local_path_to_worker_path,
    )
    url = get_default_api_url()
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_WORKSPACE_CONTAINER_ROOT = "/app"


def get_config_path() -> Path:
    """Path to ~/.motet/config.json."""
    return Path.home() / ".motet" / "config.json"


def get_cli_config() -> Dict[str, Any]:
    """Read config from ~/.motet/config.json. Returns dict, never None."""
    path = get_config_path()
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def set_cli_config_value(key: str, value: Any) -> None:
    """Set one key in config and write ~/.motet/config.json."""
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    config = get_cli_config()
    config[key] = value
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
    path.chmod(0o600)


def get_chat_conversation_id() -> str | None:
    """Current chat conversation ID for CLI session continuity (None if none set)."""
    return get_cli_config().get("chat_conversation_id")


def set_chat_conversation_id(conversation_id: str) -> None:
    """Set the current chat conversation ID (persisted for next motet-cli chat)."""
    set_cli_config_value("chat_conversation_id", conversation_id)


def get_default_api_url() -> str:
    """
    Default API URL for CLI: env MOTET_API_URL or MOTET_API_URL, then config api_url, then DEFAULT_API_URL.
    """
    url = os.getenv("MOTET_API_URL") or os.getenv("MOTET_API_URL")
    if url:
        return url.rstrip("/")
    config = get_cli_config()
    url = config.get("api_url")
    if url:
        return str(url).rstrip("/")
    return DEFAULT_API_URL


def map_local_path_to_worker_path(local_path: str) -> str | None:
    """
    Map a host-local path to its container-visible path using CLI setup config.

    Requires both config values:
      - workspace_host_root
      - workspace_container_root

    Returns:
      Mapped container path if local_path is under workspace_host_root; else None.
    """
    cfg = get_cli_config()
    host_root_raw = cfg.get("workspace_host_root")
    container_root_raw = cfg.get("workspace_container_root")
    if not host_root_raw or not container_root_raw:
        return None

    try:
        local = Path(local_path).expanduser().resolve()
        host_root = Path(str(host_root_raw)).expanduser().resolve()

        if local == host_root:
            relative = Path(".")
        elif host_root in local.parents:
            relative = local.relative_to(host_root)
        else:
            return None

        container_root = Path(str(container_root_raw))
        mapped = container_root / relative
        return str(mapped)
    except Exception:
        return None


def infer_default_workspace_mapping() -> tuple[str, str]:
    """
    Infer default workspace mapping for local Docker development.

    Returns:
      (workspace_host_root, workspace_container_root)
    """
    host_root = str(Path.cwd().expanduser().resolve())
    return host_root, DEFAULT_WORKSPACE_CONTAINER_ROOT
