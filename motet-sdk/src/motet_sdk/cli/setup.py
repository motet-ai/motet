"""
Motet - Setup CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-02-24

Description:
    Configure default API URL and other CLI settings (saved to ~/.motet/config.json).
    Use so you don't have to pass --api-url on every command.

Dependencies:
    - click, motet.cli._config

Usage:
    motet-cli setup set --api-url https://api.example.com
    motet-cli setup set --workspace-host-root /Users/me/projects/imf --workspace-container-root /app
    motet-cli setup show
    motet-cli setup doctor
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import click

from ._config import (
    get_config_path,
    get_cli_config,
    infer_default_workspace_mapping,
    set_cli_config_value,
)


@click.group("setup")
def setup_group() -> None:
    """Configure default API URL and other CLI settings (~/.motet/config.json)."""
    pass


@setup_group.command("set")
@click.option("--api-url", help="Default API base URL (e.g. https://api.example.com)")
@click.option(
    "--workspace-host-root",
    help="Host machine workspace root for local hot deploy path mapping (e.g. /Users/me/projects/imf)",
)
@click.option(
    "--workspace-container-root",
    help="Container workspace root corresponding to host root (e.g. /app)",
)
def set_cmd(
    api_url: str | None,
    workspace_host_root: str | None,
    workspace_container_root: str | None,
) -> None:
    """Set default API URL (and other options). Saves to ~/.motet/config.json."""
    if not api_url and not workspace_host_root and not workspace_container_root:
        raise click.UsageError(
            "Specify at least one option, e.g. --api-url https://api.example.com"
        )

    if bool(workspace_host_root) != bool(workspace_container_root):
        raise click.UsageError(
            "Set both --workspace-host-root and --workspace-container-root together."
        )

    if api_url:
        set_cli_config_value("api_url", api_url.rstrip("/"))
        click.echo(f"Saved. Default API URL: {api_url.rstrip('/')}")

    if workspace_host_root and workspace_container_root:
        host_root = str(Path(workspace_host_root).expanduser().resolve())
        container_root = workspace_container_root.rstrip("/")
        if not container_root:
            raise click.UsageError("--workspace-container-root must not be empty")
        set_cli_config_value("workspace_host_root", host_root)
        set_cli_config_value("workspace_container_root", container_root)
        click.echo(f"Saved. Workspace host root: {host_root}")
        click.echo(f"Saved. Workspace container root: {container_root}")

    click.echo("Use 'motet-cli setup show' to see current config.")


@setup_group.command("show")
def show_cmd() -> None:
    """Show current CLI config (from ~/.motet/config.json and env)."""
    import os
    path = get_config_path()
    click.echo(f"Config file: {path}")
    click.echo("")
    from ._config import get_default_api_url
    effective = get_default_api_url()
    from_env = os.getenv("MOTET_API_URL") or os.getenv("MOTET_API_URL")
    if from_env:
        click.echo(f"  Effective API URL: {effective}  (from MOTET_API_URL/MOTET_API_URL)")
    else:
        click.echo(f"  Effective API URL: {effective}")
    config = get_cli_config()
    if config:
        click.echo("  File contents:")
        for k, v in config.items():
            click.echo(f"    {k}: {v}")
    else:
        click.echo("  (no config file yet; use 'motet-cli setup set --api-url <url>')")


def _docker_mount_check(host_root: str, container_root: str) -> tuple[bool, str]:
    """
    Check whether any running Docker container has host_root -> container_root mount.

    Returns (ok, message). A skipped check (no docker or no running containers)
    is treated as ok=True with informational message.
    """
    try:
        ps = subprocess.run(
            ["docker", "ps", "-q"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return True, "Docker CLI not found; skipped mount verification."
    except Exception as e:
        return True, f"Docker check failed ({e}); skipped mount verification."

    if ps.returncode != 0:
        return True, "Docker not available; skipped mount verification."

    container_ids = [c.strip() for c in ps.stdout.splitlines() if c.strip()]
    if not container_ids:
        return True, "No running containers; skipped mount verification."

    host_path = str(Path(host_root).expanduser().resolve())
    for cid in container_ids:
        inspect = subprocess.run(
            ["docker", "inspect", cid],
            capture_output=True,
            text=True,
            check=False,
        )
        if inspect.returncode != 0 or not inspect.stdout.strip():
            continue
        try:
            payload = json.loads(inspect.stdout)
        except Exception:
            continue
        if not payload:
            continue
        obj = payload[0]
        mounts = obj.get("Mounts", []) or []
        for m in mounts:
            src = str(Path(str(m.get("Source", ""))).expanduser().resolve())
            dst = str(m.get("Destination", ""))
            if src == host_path and dst == container_root:
                name = str(obj.get("Name", "")).lstrip("/") or cid
                return True, f"Verified mount on container '{name}': {host_path} -> {container_root}"
    return False, f"No running container exposes mount {host_path} -> {container_root}"


@setup_group.command("doctor")
def doctor_cmd() -> None:
    """Validate CLI config and (if possible) Docker workspace mount mapping."""
    from ._config import get_default_api_url

    cfg = get_cli_config()
    api_url = get_default_api_url()
    host_root_raw = cfg.get("workspace_host_root")
    container_root_raw = cfg.get("workspace_container_root")

    click.echo("Motet CLI setup doctor")
    click.echo("")
    click.echo(f"  API URL: {api_url}")

    has_error = False

    if not host_root_raw and not container_root_raw:
        default_host_root, default_container_root = infer_default_workspace_mapping()
        click.echo("  ⚠️  Workspace mapping not configured.")
        click.echo(
            f"  ℹ️  Using inferred defaults for this workspace: {default_host_root} -> {default_container_root}"
        )
        click.echo(
            "     Persist these defaults with: "
            f"motet-cli setup set --workspace-host-root {default_host_root} --workspace-container-root {default_container_root}"
        )
        host_root_raw = default_host_root
        container_root_raw = default_container_root
    elif bool(host_root_raw) != bool(container_root_raw):
        click.echo("  ❌ Invalid config: workspace mapping is incomplete.")
        click.echo("     Set both workspace roots together.")
        has_error = True

    if host_root_raw and container_root_raw:
        host_root = str(Path(str(host_root_raw)).expanduser().resolve())
        container_root = str(container_root_raw).rstrip("/")
        click.echo(f"  Workspace host root: {host_root}")
        click.echo(f"  Workspace container root: {container_root}")

        if not Path(host_root).exists():
            click.echo("  ❌ Host root path does not exist.")
            has_error = True
        else:
            click.echo("  ✅ Host root path exists.")

        if not container_root.startswith("/"):
            click.echo("  ❌ Container root must be an absolute path (start with '/').")
            has_error = True
        else:
            click.echo("  ✅ Container root format looks valid.")

        ok, msg = _docker_mount_check(host_root, container_root)
        if ok:
            if "Verified mount" in msg:
                click.echo(f"  ✅ {msg}")
            else:
                click.echo(f"  ⚠️  {msg}")
        else:
            click.echo(f"  ❌ {msg}")
            has_error = True

    if has_error:
        raise SystemExit(1)


__all__ = ["setup_group"]
