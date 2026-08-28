"""
Motet - Device CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Device lifecycle CLI for edge worker runtime.

    registration saves WireGuard tunnel config alongside device
    profile. Start/stop orchestrate WireGuard sidecar + Celery worker via
    docker-compose.edge-worker.yml.

    (Phase D): ``device register --worker-id edge_app_builder_<app>``
    lets a remote app-builder instance register under the multi-app worker id
    instead of the derived ``edge_<uuid8>``.

Dependencies:
    - click: CLI framework
    - requests via motet_sdk.cli._api helpers
    - subprocess: Docker Compose command execution
    - pathlib/json/os: local profile persistence

Usage:
    motet-cli device register --device-name my-mac --read-path ~/Projects/foo \\
        --write-path ~/Projects/foo/out
    motet-cli device build
    motet-cli device start
    motet-cli device status
    motet-cli device logs --follow
    motet-cli device stop

    All three host bridges (clipboard, shell, process-control) are enabled by
    default.  Use ``--no-clipboard-bridge``, ``--no-shell-exec-bridge``, or
    ``--no-process-control-bridge`` to disable individually.  The shell and
    process-control bridges silently skip when the required allowlist env vars
    (MOTET_SHELL_BRIDGE_CWD_ALLOWLIST / MOTET_PROCESS_CONTROL_CWD_ALLOWLIST) are
    not set on the host.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import hashlib
import re
import sys
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import SplitResult, urlsplit, urlunsplit

import click


from ._api import api_request, api_url_option, normalize_base_url
from ._auth import get_api_headers

API_TIMEOUT_SECONDS = 30
DEFAULT_DEVICE_PROFILE = "default"


def _profiles_dir() -> Path:
    return Path.home() / ".motet" / "devices"


def _wireguard_config_dir(profile: str = DEFAULT_DEVICE_PROFILE) -> Path:
    """Per-profile WireGuard config directory so multiple profiles don't collide."""
    return _profiles_dir() / "wg" / profile


def _write_wireguard_config(wg_data: Dict[str, Any], profile: str = DEFAULT_DEVICE_PROFILE) -> Path:
    """Write a wg0.conf file from registration WireGuard data."""
    wg_dir = _wireguard_config_dir(profile)
    wg_dir.mkdir(parents=True, exist_ok=True)
    conf_path = wg_dir / "wg0.conf"
    lines = [
        "[Interface]",
        f"PrivateKey = {wg_data['client_private_key']}",
        f"Address = {wg_data['client_address']}",
        "",
        "[Peer]",
        f"PublicKey = {wg_data['server_public_key']}",
        f"Endpoint = {wg_data['server_endpoint']}",
        f"AllowedIPs = {wg_data.get('allowed_ips', '10.0.0.0/16')}",
        "PersistentKeepalive = 25",
    ]
    if wg_data.get("dns"):
        lines.insert(3, f"DNS = {wg_data['dns']}")
    conf_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    conf_path.chmod(0o600)
    return conf_path


def _profile_path(profile: str) -> Path:
    return _profiles_dir() / f"{profile}.json"


def _paths_compose_fragment_path(profile: str) -> Path:
    """Generated compose fragment for extra host directory mounts (read-only + read-write)."""
    d = _profiles_dir() / "compose"
    return d / f"{profile}.paths.yml"


def _normalize_host_access_paths(raw_paths: List[str], option_label: str) -> List[str]:
    """
    Resolve and deduplicate host paths for file access.

    Directories are used as-is. Files are replaced by their parent directory.
    """
    out: List[str] = []
    seen: set[str] = set()
    for raw in raw_paths:
        s = str(raw or "").strip()
        if not s:
            continue
        p = Path(s).expanduser()
        if not p.exists():
            raise click.ClickException(f"{option_label} path does not exist: {raw!r}")
        p = p.resolve()
        if p.is_file():
            p = p.parent
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _normalize_host_read_paths(raw_paths: List[str]) -> List[str]:
    """Backward-compatible wrapper for read-path validation."""
    return _normalize_host_access_paths(raw_paths, "--read-path")


def _normalize_host_write_paths(raw_paths: List[str]) -> List[str]:
    return _normalize_host_access_paths(raw_paths, "--write-path")


def _dedupe_read_write(read_norm: List[str], write_norm: List[str]) -> tuple[List[str], List[str]]:
    """Paths listed for both read and write get a single :rw mount under write only."""
    ws = set(write_norm)
    read_only = [p for p in read_norm if p not in ws]
    return read_only, write_norm


def _sync_paths_compose_fragment(
    profile: str, read_hosts: List[str], write_hosts: List[str]
) -> None:
    """Write or remove the per-profile compose fragment (/mnt/motet/read/* ro, /mnt/motet/write/* rw)."""
    frag = _paths_compose_fragment_path(profile)
    read_only, write_merged = _dedupe_read_write(read_hosts, write_hosts)
    volumes: List[str] = []
    for i, host in enumerate(read_only):
        volumes.append(f"{host}:/mnt/motet/read/{i}:ro")
    for j, host in enumerate(write_merged):
        volumes.append(f"{host}:/mnt/motet/write/{j}:rw")
    if not volumes:
        frag.unlink(missing_ok=True)
        return
    doc: Dict[str, Any] = {"services": {"worker": {"volumes": volumes}}}
    try:
        import yaml  # type: ignore[import-untyped]

        frag.parent.mkdir(parents=True, exist_ok=True)
        frag.write_text(yaml.dump(doc, default_flow_style=False), encoding="utf-8")
        frag.chmod(0o600)
    except Exception as e:
        raise click.ClickException(f"Failed to write paths compose fragment {frag}: {e}") from e


def _file_read_allowlist_for_mounts(read_only: List[str], write_hosts: List[str]) -> str:
    parts = ["/app"] + [f"/mnt/motet/read/{i}" for i in range(len(read_only))]
    parts += [f"/mnt/motet/write/{j}" for j in range(len(write_hosts))]
    return ",".join(parts)


def _file_write_allowlist_for_mounts(write_hosts: List[str]) -> str:
    parts = ["/app"] + [f"/mnt/motet/write/{j}" for j in range(len(write_hosts))]
    return ",".join(parts)


def _coerce_host_read_paths_from_profile(profile_payload: Dict[str, Any]) -> List[str]:
    raw = profile_payload.get("host_read_paths")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise click.ClickException(
            "Profile field host_read_paths must be a list of strings. "
            "Fix with `motet-cli device configure --read-path ...` or edit the JSON."
        )
    return [str(x).strip() for x in raw if str(x).strip()]


def _coerce_host_write_paths_from_profile(profile_payload: Dict[str, Any]) -> List[str]:
    raw = profile_payload.get("host_write_paths")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise click.ClickException(
            "Profile field host_write_paths must be a list of strings. "
            "Fix with `motet-cli device configure --write-path ...` or edit the JSON."
        )
    return [str(x).strip() for x in raw if str(x).strip()]


def _prepare_local_fs_access(
    *,
    profile: str,
    profile_payload: Dict[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    """
    Normalize paths, sync compose fragment, return (MOTET_FILE_READ_ALLOWLIST,
    MOTET_FILE_WRITE_ALLOWLIST) or None for each when not constrained.
    """
    raw_read = _coerce_host_read_paths_from_profile(profile_payload)
    raw_write = _coerce_host_write_paths_from_profile(profile_payload)
    read_norm = _normalize_host_read_paths(raw_read) if raw_read else []
    write_norm = _normalize_host_write_paths(raw_write) if raw_write else []
    read_only, write_merged = _dedupe_read_write(read_norm, write_norm)
    _sync_paths_compose_fragment(profile, read_only, write_merged)

    read_allow: Optional[str] = None
    write_allow: Optional[str] = None
    if read_norm or write_norm:
        read_allow = _file_read_allowlist_for_mounts(read_only, write_merged)
    if write_norm:
        write_allow = _file_write_allowlist_for_mounts(write_merged)
    return read_allow, write_allow


def _load_profile(profile: str) -> Dict[str, Any]:
    p = _profile_path(profile)
    if not p.exists():
        raise click.ClickException(
            f"Device profile not found: {p}. Run `motet-cli device register --profile {profile}` first."
        )
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise click.ClickException(f"Failed to read device profile {p}: {e}") from e


def _save_profile(profile: str, payload: Dict[str, Any]) -> Path:
    p = _profile_path(profile)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    p.chmod(0o600)
    return p


def _get_local_compose_file() -> Path:
    env_path = os.environ.get("MOTET_DEVICE_COMPOSE_FILE")
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if p.exists():
            return p
        raise click.ClickException(f"MOTET_DEVICE_COMPOSE_FILE does not exist: {p}")

    candidate = Path.cwd()
    legacy_hit: Optional[Path] = None
    for _ in range(10):
        compose_path = candidate / "docker-compose.edge-worker.yml"
        if compose_path.exists():
            return compose_path
        legacy_path = candidate / "docker-compose.local-worker.yml"
        if legacy_path.exists() and legacy_hit is None:
            legacy_hit = legacy_path
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent

    if legacy_hit is not None:
        click.echo(
            "WARN: Found deprecated docker-compose.local-worker.yml; "
            "prefer docker-compose.edge-worker.yml (#197).",
            err=True,
        )
        return legacy_hit

    raise click.ClickException(
        "Could not locate docker-compose.edge-worker.yml. Set MOTET_DEVICE_COMPOSE_FILE or run from repo."
    )


def _resolve_mcp_config_path(compose_file: Path, mcp_config_path: str) -> Path:
    p = Path(mcp_config_path).expanduser()
    if p.is_absolute():
        return p
    return (compose_file.parent / p).resolve()


def _load_mcp_servers_from_config(path: Path, selected_service_ids: List[str]) -> List[Dict[str, Any]]:
    try:
        import yaml  # type: ignore[import-untyped]
    except Exception as e:
        raise click.ClickException(
            "PyYAML is required for --mcp-from-config. Install with: pip install pyyaml"
        ) from e

    if not path.exists():
        raise click.ClickException(f"MCP config file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        raise click.ClickException(f"Failed to parse MCP config {path}: {e}") from e

    services = raw.get("services") if isinstance(raw, dict) else None
    if not isinstance(services, list):
        raise click.ClickException(f"MCP config {path} must contain a top-level 'services' list")

    selected = {s.strip() for s in selected_service_ids if s.strip()}
    out: List[Dict[str, Any]] = []
    for entry in services:
        if not isinstance(entry, dict):
            continue
        service_id = str(entry.get("service_id") or "").strip()
        if not service_id:
            continue
        if selected and service_id not in selected:
            continue
        out.append(dict(entry))

    if selected:
        found = {str(x.get("service_id") or "").strip() for x in out}
        missing = sorted(s for s in selected if s not in found)
        if missing:
            raise click.ClickException(
                f"Requested --mcp-service values not found in config: {', '.join(missing)}"
            )

    return out


def _get_local_compose_override(compose_file: Path) -> Optional[Path]:
    override = compose_file.with_name("docker-compose.edge-worker.override.yml")
    if override.exists():
        return override
    legacy = compose_file.with_name("docker-compose.local-worker.override.yml")
    if legacy.exists():
        click.echo(
            "WARN: Found deprecated docker-compose.local-worker.override.yml; "
            "prefer docker-compose.edge-worker.override.yml (#197).",
            err=True,
        )
        return legacy
    return None


def _compose_binary() -> List[str]:
    docker_compose = subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if docker_compose.returncode == 0:
        return ["docker", "compose"]

    legacy = subprocess.run(
        ["docker-compose", "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if legacy.returncode == 0:
        return ["docker-compose"]
    raise click.ClickException("Neither 'docker compose' nor 'docker-compose' is available on PATH.")


def _compose_command(
    args: List[str],
    compose_file: Path,
    *,
    project_name: Optional[str] = None,
    profile: Optional[str] = None,
) -> List[str]:
    cmd = [*_compose_binary()]
    if project_name:
        cmd.extend(["-p", project_name])
    cmd.extend(["-f", str(compose_file)])
    override = _get_local_compose_override(compose_file)
    if override:
        cmd.extend(["-f", str(override)])
    if profile:
        frag = _paths_compose_fragment_path(profile)
        if frag.exists():
            cmd.extend(["-f", str(frag)])
    cmd.extend(args)
    return cmd


def _run_compose(cmd: List[str], *, env: Optional[Dict[str, str]] = None, stream: bool = True) -> str:
    if stream:
        result = subprocess.run(cmd, env=env, check=False)
        if result.returncode != 0:
            raise click.ClickException(f"Compose command failed with exit code {result.returncode}")
        return ""
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "unknown error").strip()
        raise click.ClickException(details)
    return (result.stdout or "").strip()


def _docker_image_exists(image: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _rewrite_localhost_for_container(url: str) -> str:
    """
    Rewrite localhost-style URLs for container-to-host access.

    In dockerized edge worker mode, `localhost` points at the container itself.
    Rewriting to `host.docker.internal` avoids common connectivity errors when the
    Motet API services run on the host machine.
    """
    raw = (url or "").strip()
    if not raw:
        return raw
    try:
        parsed = urlsplit(raw)
    except Exception:
        return raw
    hostname = parsed.hostname or ""
    if hostname not in {"localhost", "127.0.0.1", "::1"}:
        return raw

    userinfo = ""
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo = f"{userinfo}:{parsed.password}"
        userinfo += "@"
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{userinfo}host.docker.internal{port}"
    rewritten = SplitResult(
        scheme=parsed.scheme,
        netloc=netloc,
        path=parsed.path,
        query=parsed.query,
        fragment=parsed.fragment,
    )
    return urlunsplit(rewritten)


def _emit_rewrite_notice(label: str, original: str, rewritten: str) -> None:
    """Print a one-line warning when localhost URLs are rewritten for containers."""
    if not original or original == rewritten:
        return
    click.echo(
        f"  WARN: Rewrote {label} for container networking: {original} -> {rewritten}"
    )


def _is_wireguard_profile(profile_payload: Dict[str, Any]) -> bool:
    """True if the profile was registered with WireGuard tunnel config."""
    return bool(profile_payload.get("wireguard_config_dir") or profile_payload.get("valkey_url"))


def _effective_runtime_config(
    *,
    profile_payload: Dict[str, Any],
    principal_id: Optional[str],
    tenant_id: Optional[str],
    image: str,
    mcp_config_path: str,
    mcp_service_filter: str,
) -> Dict[str, str]:
    """Build environment dict for the edge worker container (ADR-0095 WireGuard mode)."""
    resolved_principal = principal_id or str(profile_payload.get("principal_id") or os.getenv("MOTET_PRINCIPAL_ID") or "")
    resolved_tenant = tenant_id or str(profile_payload.get("tenant_id") or os.getenv("MOTET_TENANT_ID") or "")

    if not resolved_principal:
        raise click.ClickException("Missing principal_id. Pass --principal-id or set MOTET_PRINCIPAL_ID.")
    if not resolved_tenant:
        raise click.ClickException("Missing tenant_id. Pass --tenant-id or set MOTET_TENANT_ID.")

    auth_token = str(profile_payload.get("device_token") or "")
    worker_id = str(profile_payload.get("worker_id") or "")
    device_id = str(profile_payload.get("device_id") or "")

    if not auth_token or not worker_id or not device_id:
        raise click.ClickException(
            "Profile missing required fields (device_token/worker_id/device_id). Re-run `motet-cli device register`."
        )

    valkey_url = str(profile_payload.get("valkey_url") or "")
    if not valkey_url:
        raise click.ClickException("Profile missing valkey_url. Re-register with a WireGuard-enabled server.")

    env: Dict[str, str] = {
        "MOTET_EDGE_WORKER_IMAGE": image,
        "MOTET_MCP_ENABLED": "true",
        "MCP_INSTANCE_MANAGER_CONFIG": mcp_config_path,
        "MOTET_EDGE_MCP_SERVICE_FILTER": mcp_service_filter,
        "MOTET_EDGE_AUTH_TOKEN": auth_token,
        "MOTET_EDGE_TENANT_ID": resolved_tenant,
        "MOTET_EDGE_PRINCIPAL_ID": resolved_principal,
        "MOTET_EDGE_COMMAND_SCOPE": str(profile_payload.get("command_scope") or "principal"),
        "MOTET_EDGE_DEVICE_ID": device_id,
        "MOTET_EDGE_WORKER_ID": worker_id,
        "MOTET_WORKER_ID": worker_id,
        "MOTET_EDGE_DEVICE_NAME": str(profile_payload.get("device_name") or "edge-device"),
        "MOTET_VALKEY_URL": valkey_url,
        "MOTET_WIREGUARD_CONFIG_DIR": str(profile_payload.get("wireguard_config_dir") or ""),
        "MOTET_VAULT_AUTH_TOKEN": auth_token,
    }

    vault_url = str(profile_payload.get("vault_resolve_url") or "")
    if vault_url:
        rewritten = _rewrite_localhost_for_container(vault_url)
        _emit_rewrite_notice("MOTET_VAULT_RESOLVE_URL", vault_url, rewritten)
        env["MOTET_VAULT_RESOLVE_URL"] = rewritten

    return env


def _slugify_project_fragment(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = text.strip("_")
    return text or "device"


def _project_name_for_profile(profile: str, profile_payload: Dict[str, Any]) -> str:
    """
    Build a stable per-device compose project name.

    This allows multiple local device stacks on the same host without container,
    network, or project name collisions.
    """
    device_hint = str(profile_payload.get("device_name") or profile or "device")
    seed = str(
        profile_payload.get("device_id")
        or profile_payload.get("worker_id")
        or profile
    )
    suffix = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
    return f"mr_{_slugify_project_fragment(device_hint)}_{suffix}"


def _local_bridge_docker_host() -> str:
    """Hostname the container uses to reach host bridges (Docker Desktop vs Linux)."""
    return (
        os.environ.get("MOTET_PROCESS_CONTROL_BRIDGE_DOCKER_HOST")
        or os.environ.get("MOTET_SHELL_BRIDGE_DOCKER_HOST")
        or os.environ.get("MOTET_CLIPBOARD_BRIDGE_DOCKER_HOST")
        or "host.docker.internal"
    ).strip()


# ---------------------------------------------------------------------------
# Generic bridge lifecycle helper
# ---------------------------------------------------------------------------

def _bridge_state_path(name: str, profile: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.@-]+", "_", (profile or "default").strip()).strip("_") or "default"
    return _profiles_dir() / f"{name}-{safe}.json"


def _stop_bridge(name: str, profile: str) -> None:
    """Stop a running host bridge process by reading its state file."""
    path = _bridge_state_path(name, profile)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pid = int(data.get("pid") or 0)
    except Exception:
        path.unlink(missing_ok=True)
        return
    if pid > 0:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pid = 0
        except PermissionError:
            pass
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _start_bridge(
    name: str,
    profile: str,
    module: str,
    token_env: str,
) -> tuple[Optional[str], Optional[str]]:
    """
    Spawn a host-side HTTP bridge subprocess.

    The bridge must print its TCP port as the first stdout line.
    Returns ``(url, token)`` or ``(None, None)`` on failure.
    """
    _stop_bridge(name, profile)
    token = secrets.token_urlsafe(32)
    cmd = [sys.executable, "-m", module]
    env = os.environ.copy()
    env[token_env] = token
    label = name.replace("-", " ")
    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
    except Exception as e:
        click.echo(f"  WARN: {label} bridge could not spawn: {e}")
        return None, None
    if proc.stdout is None:
        proc.kill()
        return None, None
    line = proc.stdout.readline()
    if not line:
        err_b = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        proc.kill()
        if err_b.strip():
            click.echo(f"  WARN: {label} bridge failed: {err_b.strip()[:300]}")
        else:
            click.echo(f"  WARN: {label} bridge failed to report port (empty stdout).")
        return None, None
    try:
        port = int(line.decode("utf-8").strip())
    except ValueError:
        err_b = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        proc.kill()
        click.echo(f"  WARN: {label} bridge invalid port line {line!r}: {err_b[:200]}")
        return None, None
    if proc.poll() is not None:
        err_b = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        click.echo(f"  WARN: {label} bridge exited early: {err_b.strip()[:300]}")
        return None, None
    state_path = _bridge_state_path(name, profile)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"pid": proc.pid, "port": port}), encoding="utf-8")
        state_path.chmod(0o600)
    except Exception:
        pass
    url = f"http://{_local_bridge_docker_host()}:{port}"
    return url, token


# ---------------------------------------------------------------------------
# Concrete bridge helpers (thin wrappers around the generic lifecycle)
# ---------------------------------------------------------------------------

_CLIPBOARD_BRIDGE = "clipboard-bridge"
_SHELL_BRIDGE = "shell-bridge"
_PROCESS_CONTROL_BRIDGE = "process-control-bridge"


def _stop_clipboard_bridge(profile: str) -> None:
    _stop_bridge(_CLIPBOARD_BRIDGE, profile)


def _stop_shell_bridge(profile: str) -> None:
    _stop_bridge(_SHELL_BRIDGE, profile)


def _stop_process_control_bridge(profile: str) -> None:
    _stop_bridge(_PROCESS_CONTROL_BRIDGE, profile)


def _start_clipboard_bridge(profile: str) -> tuple[Optional[str], Optional[str]]:
    return _start_bridge(
        _CLIPBOARD_BRIDGE, profile,
        module="motet_sdk.cli.clipboard_bridge",
        token_env="MOTET_CLIPBOARD_BRIDGE_TOKEN",
    )


def _start_shell_bridge(profile: str) -> tuple[Optional[str], Optional[str]]:
    cwd_allow = (os.environ.get("MOTET_SHELL_BRIDGE_CWD_ALLOWLIST") or "").strip()
    if not cwd_allow:
        return None, None
    return _start_bridge(
        _SHELL_BRIDGE, profile,
        module="motet_sdk.cli.shell_bridge",
        token_env="MOTET_SHELL_BRIDGE_TOKEN",
    )


def _process_control_cwd_allowlist_configured() -> bool:
    return bool(
        (os.environ.get("MOTET_PROCESS_CONTROL_CWD_ALLOWLIST") or "").strip()
        or (os.environ.get("MOTET_SHELL_BRIDGE_CWD_ALLOWLIST") or "").strip()
    )


def _start_process_control_bridge(profile: str) -> tuple[Optional[str], Optional[str]]:
    if not _process_control_cwd_allowlist_configured():
        return None, None
    return _start_bridge(
        _PROCESS_CONTROL_BRIDGE, profile,
        module="motet_sdk.cli.process_control_bridge",
        token_env="MOTET_PROCESS_CONTROL_BRIDGE_TOKEN",
    )


# ---------------------------------------------------------------------------
# Merge bridge env vars into the runtime config passed to Docker Compose
# ---------------------------------------------------------------------------

def _merge_shell_bridge_into_runtime(
    runtime: Dict[str, str],
    profile: str,
    *,
    enable: bool,
) -> None:
    if not enable:
        _stop_shell_bridge(profile)
        return
    if not (os.environ.get("MOTET_SHELL_BRIDGE_CWD_ALLOWLIST") or "").strip():
        click.echo(
            "  WARN: Shell bridge skipped: set MOTET_SHELL_BRIDGE_CWD_ALLOWLIST "
            "(comma-separated host directory paths) on the host to enable core.host_exec."
        )
        return
    url, tok = _start_shell_bridge(profile)
    if not url or not tok:
        click.echo("  WARN: Host shell bridge did not start; core.host_exec will be unavailable.")
        return
    runtime["MOTET_SHELL_BRIDGE_URL"] = url
    runtime["MOTET_SHELL_BRIDGE_TOKEN"] = tok
    runtime["MOTET_ENABLE_SHELL_EXEC"] = "1"


def _merge_process_control_bridge_into_runtime(
    runtime: Dict[str, str],
    profile: str,
    *,
    enable: bool,
) -> None:
    if not enable:
        _stop_process_control_bridge(profile)
        return
    if not _process_control_cwd_allowlist_configured():
        click.echo(
            "  WARN: Process-control bridge skipped: set "
            "MOTET_PROCESS_CONTROL_CWD_ALLOWLIST or MOTET_SHELL_BRIDGE_CWD_ALLOWLIST on the host to enable core.process_control."
        )
        return
    url, tok = _start_process_control_bridge(profile)
    if not url or not tok:
        click.echo("  WARN: Host process control bridge did not start.")
        return
    runtime["MOTET_PROCESS_CONTROL_BRIDGE_URL"] = url
    runtime["MOTET_PROCESS_CONTROL_BRIDGE_TOKEN"] = tok
    runtime["MOTET_ENABLE_PROCESS_CONTROL"] = "1"


def _merge_clipboard_bridge_into_runtime(
    runtime: Dict[str, str],
    profile: str,
    *,
    enable: bool,
) -> None:
    if not enable:
        _stop_clipboard_bridge(profile)
        return
    url, tok = _start_clipboard_bridge(profile)
    if url and tok:
        runtime["MOTET_CLIPBOARD_BRIDGE_URL"] = url
        runtime["MOTET_CLIPBOARD_BRIDGE_TOKEN"] = tok


def _edge_cleanup_headers(device_token: str) -> Dict[str, str]:
    """Prefer device token for readiness deregister; fall back to CLI API auth."""
    token = (device_token or "").strip()
    if token:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    return get_api_headers()


def _is_edge_cleanup_candidate(worker_id: str) -> bool:
    """True for canonical edge_* or legacy cloud_edge_* readiness ids."""
    wid = (worker_id or "").strip()
    return wid.startswith("edge_") or wid.startswith("cloud_edge_")


def _best_effort_remote_cleanup(
    *,
    profile_payload: Dict[str, Any],
    api_url: str,
    worker_cleanup_candidates: Optional[List[str]] = None,
) -> None:
    """
    Best-effort cleanup of remote state for a stopped edge device runtime.

    Uses device-scoped ``POST /api/v1/devices/workers/{id}/deregister`` so
    readiness/health entries clear without requiring ``motet-admin`` terminate.
    """
    worker_id = str(profile_payload.get("worker_id") or "").strip()
    device_token = str(profile_payload.get("device_token") or "").strip()
    if not worker_id:
        return
    base = normalize_base_url(api_url)
    headers = _edge_cleanup_headers(device_token)

    candidates: List[str] = []
    for candidate in (worker_cleanup_candidates or [worker_id, f"cloud_{worker_id}"]):
        c = str(candidate or "").strip()
        if c and c not in candidates:
            candidates.append(c)

    edge_candidates = [c for c in candidates if _is_edge_cleanup_candidate(c)]
    skipped = [c for c in candidates if c not in edge_candidates]
    for candidate in skipped:
        click.echo(
            f"  WARN: Skipping non-edge cleanup candidate {candidate} "
            "(device deregister only clears edge_* readiness)"
        )

    for candidate in edge_candidates:
        try:
            resp = api_request(
                "POST",
                f"{base}/api/v1/devices/workers/{candidate}/deregister",
                headers=headers,
                timeout=API_TIMEOUT_SECONDS,
            )
            body = resp.json() if resp.content else {}
            removed = bool(body.get("removed")) if isinstance(body, dict) else False
            canonical = (
                str(body.get("worker_id") or candidate)
                if isinstance(body, dict)
                else candidate
            )
            if removed:
                click.echo(f"  OK: Deregistered edge worker {canonical} from readiness")
            else:
                click.echo(
                    f"  OK: Edge worker {canonical} already absent from readiness"
                )
        except Exception as e:
            click.echo(
                "  WARN: Edge worker deregister skipped for "
                f"{candidate} (stale cleanup will remove later): {e}"
            )

    verify_ids = edge_candidates or candidates
    try:
        health = api_request(
            "GET",
            f"{base}/api/v1/workers/health",
            timeout=API_TIMEOUT_SECONDS,
        ).json()
        workers = health.get("worker_health", {}) if isinstance(health, dict) else {}
        # API normalizes cloud_edge_* → edge_*; check both forms.
        remaining: List[str] = []
        for candidate in verify_ids:
            normalized = (
                candidate[len("cloud_") :]
                if candidate.startswith("cloud_edge_")
                else candidate
            )
            if candidate in workers or normalized in workers:
                remaining.append(normalized if normalized in workers else candidate)
        if remaining:
            click.echo(
                "  WARN: Worker(s) still appear in /api/v1/workers/health: "
                f"{', '.join(remaining)} "
                "(may still be shutting down or stale pruning fallback)."
            )
        else:
            click.echo(
                "  OK: Worker cleanup verified; no matching edge worker ids remain in "
                "/api/v1/workers/health"
            )
    except Exception as e:
        click.echo(f"  WARN: Unable to verify worker health cleanup: {e}")


def _resolve_local_worker_cleanup_candidates(
    *,
    compose_file: Path,
    profile: str,
    profile_payload: Dict[str, Any],
) -> List[str]:
    """
    Derive worker ids to clean from readiness.

    Includes:
    - Profile id (`edge_*`)
    - Legacy Celery-prefixed id (`cloud_edge_*`)
    - Hostname-based id from a still-running edge worker container (`cloud_<hostname>`)
    """
    base_worker_id = str(profile_payload.get("worker_id") or "").strip()
    candidates: List[str] = []
    for c in (base_worker_id, f"cloud_{base_worker_id}" if base_worker_id else ""):
        if c and c not in candidates:
            candidates.append(c)

    # Best-effort capture of legacy hostname-based worker id before compose down.
    try:
        ps_cmd = _compose_command(
            ["--profile", "edge", "ps", "-q", "worker"],
            compose_file,
            project_name=_project_name_for_profile(profile, profile_payload),
            profile=profile,
        )
        container_id = _run_compose(ps_cmd, stream=False).strip()
        if container_id:
            inspect = subprocess.run(
                ["docker", "inspect", "-f", "{{.Config.Hostname}}", container_id],
                capture_output=True,
                text=True,
                check=False,
            )
            if inspect.returncode == 0:
                hostname = (inspect.stdout or "").strip()
                legacy = f"cloud_{hostname}" if hostname else ""
                if legacy and legacy not in candidates:
                    candidates.append(legacy)
    except Exception:
        # Non-fatal: fallback cleanup candidates still cover canonical local ids.
        pass

    return candidates


@click.group("device")
def device_group() -> None:
    """Manage edge worker device registration and runtime (ADR-0065/0095)."""
    pass


@device_group.command("register")
@click.option("--device-name", default="local-device", show_default=True, help="Human-readable device label.")
@click.option(
    "--worker-id",
    "worker_id",
    default=None,
    help=(
        "Explicit worker id (must start with 'edge_' and be unclaimed). "
        "Used by remote app-builder instances (edge_app_builder_<app>); "
        "default derives edge_<uuid8> from the device id."
    ),
)
@click.option("--profile", default=DEFAULT_DEVICE_PROFILE, show_default=True, help="Local CLI device profile name.")
@click.option("--principal-id", default=None, help="Principal id to persist for local runtime start.")
@click.option("--tenant-id", default=None, help="Tenant id to persist for local runtime start.")
@click.option(
    "--scope",
    "command_scope",
    default="principal",
    show_default=True,
    type=click.Choice(["principal", "tenant"], case_sensitive=False),
    help="Command acceptance scope. 'principal' = only your commands; 'tenant' = any user in your tenant.",
)
@click.option(
    "--read-path",
    "read_paths",
    multiple=True,
    help=(
        "Host directory (or file) readable inside the worker via core.file_read; "
        "repeatable. Stored only in the local profile (not sent to the API). "
        "Mounts read-only at /mnt/motet/read/N; MOTET_FILE_READ_ALLOWLIST includes these paths."
    ),
)
@click.option(
    "--write-path",
    "write_paths",
    multiple=True,
    help=(
        "Host directory writable inside the worker via core.file_write; "
        "repeatable. Stored only in the local profile (not sent to the API). "
        "Mounts read-write at /mnt/motet/write/N; MOTET_FILE_WRITE_ALLOWLIST is set."
    ),
)
@click.option("--print-only", is_flag=True, default=False, help="Print API response without writing profile.")
@api_url_option()
def register_device(
    device_name: str,
    worker_id: Optional[str],
    profile: str,
    principal_id: Optional[str],
    tenant_id: Optional[str],
    command_scope: str,
    read_paths: Tuple[str, ...],
    write_paths: Tuple[str, ...],
    print_only: bool,
    api_url: str,
) -> None:
    """Register a new device via API and save local runtime profile."""
    base = normalize_base_url(api_url)
    request_body: Dict[str, Any] = {
        "device_name": device_name,
        "command_scope": command_scope,
    }
    if worker_id:
        request_body["worker_id"] = worker_id
    r = api_request(
        "POST",
        f"{base}/api/v1/devices/register",
        headers=get_api_headers(),
        json=request_body,
        timeout=API_TIMEOUT_SECONDS,
    )
    payload = r.json()
    click.echo(json.dumps(payload, indent=2))
    if print_only:
        if read_paths:
            _normalize_host_read_paths(list(read_paths))
        if write_paths:
            _normalize_host_write_paths(list(write_paths))
        return

    credentials = payload.get("credentials") or {}
    runtime_profile: Dict[str, Any] = {
        "device_name": device_name,
        "device_id": payload.get("device_id"),
        "worker_id": payload.get("worker_id"),
        "device_token": credentials.get("device_token"),
        "principal_id": principal_id or payload.get("principal_id") or os.getenv("MOTET_PRINCIPAL_ID", ""),
        "tenant_id": tenant_id or payload.get("tenant_id") or os.getenv("MOTET_TENANT_ID", ""),
        "command_scope": payload.get("command_scope", command_scope),
    }

    wg_data = payload.get("wireguard")
    if wg_data and isinstance(wg_data, dict):
        runtime_profile["valkey_url"] = payload.get("valkey_url", "")
        runtime_profile["vault_resolve_url"] = payload.get("vault_resolve_url", "")
        runtime_profile["wireguard_config_dir"] = str(_wireguard_config_dir(profile))
        _write_wireguard_config(wg_data, profile)
        click.echo(f"\nWireGuard config written to: {_wireguard_config_dir(profile) / 'wg0.conf'}")
    else:
        raise click.ClickException(
            "Server did not return WireGuard configuration. "
            "Ensure the server has WireGuard configured (MOTET_WIREGUARD_SERVER_PUBLIC_KEY, "
            "MOTET_WIREGUARD_SERVER_ENDPOINT)."
        )

    if read_paths:
        runtime_profile["host_read_paths"] = _normalize_host_read_paths(list(read_paths))
    if write_paths:
        runtime_profile["host_write_paths"] = _normalize_host_write_paths(list(write_paths))

    path = _save_profile(profile, runtime_profile)
    click.echo(f"Saved device profile: {path}")
    click.echo("Next step: motet-cli device start")


@device_group.command("configure")
@click.option("--profile", default=DEFAULT_DEVICE_PROFILE, show_default=True, help="Device profile to update.")
@click.option(
    "--read-path",
    "read_paths",
    multiple=True,
    help="Host directory or file for core.file_read (repeatable). Replaces any previous list.",
)
@click.option(
    "--write-path",
    "write_paths",
    multiple=True,
    help="Host directory or file for core.file_write (repeatable). Replaces any previous list.",
)
@click.option(
    "--clear-read-paths",
    is_flag=True,
    default=False,
    help="Remove host_read_paths from the profile (compose fragment is regenerated on save).",
)
@click.option(
    "--clear-write-paths",
    is_flag=True,
    default=False,
    help="Remove host_write_paths from the profile (compose fragment is regenerated on save).",
)
def configure_device(
    profile: str,
    read_paths: Tuple[str, ...],
    write_paths: Tuple[str, ...],
    clear_read_paths: bool,
    clear_write_paths: bool,
) -> None:
    """Update local-only device settings (e.g. filesystem paths) without re-registering."""
    payload = _load_profile(profile)
    if clear_read_paths and read_paths:
        raise click.ClickException("Use either --clear-read-paths or --read-path, not both.")
    if clear_write_paths and write_paths:
        raise click.ClickException("Use either --clear-write-paths or --write-path, not both.")
    if not (clear_read_paths or clear_write_paths or read_paths or write_paths):
        raise click.ClickException(
            "Specify --read-path / --write-path (repeatable), --clear-read-paths, "
            "and/or --clear-write-paths."
        )
    if clear_read_paths:
        payload.pop("host_read_paths", None)
    if clear_write_paths:
        payload.pop("host_write_paths", None)
    if read_paths:
        payload["host_read_paths"] = _normalize_host_read_paths(list(read_paths))
    if write_paths:
        payload["host_write_paths"] = _normalize_host_write_paths(list(write_paths))

    _save_profile(profile, payload)
    _prepare_local_fs_access(profile=profile, profile_payload=payload)

    changes: List[str] = []
    if read_paths:
        changes.append(f"host_read_paths: {len(payload.get('host_read_paths') or [])} path(s)")
    elif clear_read_paths:
        changes.append("host_read_paths cleared")
    if write_paths:
        changes.append(f"host_write_paths: {len(payload.get('host_write_paths') or [])} path(s)")
    elif clear_write_paths:
        changes.append("host_write_paths cleared")
    click.echo(
        f"Updated profile {profile!r} ({'; '.join(changes)}). Restart the device runtime to apply."
    )


@device_group.command("list")
@api_url_option()
def list_devices(api_url: str) -> None:
    """List registered devices from API."""
    base = normalize_base_url(api_url)
    r = api_request(
        "GET",
        f"{base}/api/v1/devices",
        headers=get_api_headers(),
        timeout=API_TIMEOUT_SECONDS,
    )
    click.echo(json.dumps(r.json(), indent=2))


@device_group.command("revoke")
@click.argument("device_id")
@click.option("--profile", default=None, help="Optional profile to clear if it matches revoked device.")
@api_url_option()
def revoke_device(device_id: str, profile: Optional[str], api_url: str) -> None:
    """Revoke a device via API."""
    base = normalize_base_url(api_url)
    api_request(
        "DELETE",
        f"{base}/api/v1/devices/{device_id}",
        headers=get_api_headers(),
        timeout=API_TIMEOUT_SECONDS,
    )
    click.echo(json.dumps({"revoked": True, "device_id": device_id}, indent=2))

    if profile:
        try:
            existing = _load_profile(profile)
            if str(existing.get("device_id") or "") == device_id:
                p = _profile_path(profile)
                p.unlink(missing_ok=True)
                click.echo(f"Removed matching local profile: {p}")
        except Exception:
            pass


@device_group.command("start")
@click.option("--profile", default=DEFAULT_DEVICE_PROFILE, show_default=True, help="Device profile to run.")
@click.option("--build", is_flag=True, default=False, help="Build image before starting.")
@click.option("--principal-id", default=None, help="Override principal id.")
@click.option("--tenant-id", default=None, help="Override tenant id.")
@click.option("--image", default="motet-edge-worker:latest", show_default=True, help="Edge worker image.")
@click.option(
    "--mcp-from-config/--no-mcp-from-config",
    default=True,
    help="Load MCP server definitions from mcp_instance_manager.yaml.",
)
@click.option(
    "--mcp-config-path",
    default="config/mcp_instance_manager.yaml",
    show_default=True,
    help="Path to MCP manager YAML (repo-relative unless absolute).",
)
@click.option(
    "--mcp-service",
    "mcp_services",
    multiple=True,
    help="Optional service_id filter when using --mcp-from-config (repeatable).",
)
@click.option(
    "--no-clipboard-bridge",
    is_flag=True,
    default=False,
    help="Do not spawn the host clipboard bridge (core.clipboard_* will use in-container pyclip only).",
)
@click.option(
    "--no-shell-exec-bridge",
    is_flag=True,
    default=False,
    help=(
        "Do not spawn host shell bridge for core.host_exec. Enabled by default when "
        "MOTET_SHELL_BRIDGE_CWD_ALLOWLIST is set on the host."
    ),
)
@click.option(
    "--no-process-control-bridge",
    is_flag=True,
    default=False,
    help=(
        "Do not spawn host process-control bridge for core.process_control. Enabled by "
        "default when MOTET_PROCESS_CONTROL_CWD_ALLOWLIST or MOTET_SHELL_BRIDGE_CWD_ALLOWLIST is set."
    ),
)
def start_device_runtime(
    profile: str,
    build: bool,
    principal_id: Optional[str],
    tenant_id: Optional[str],
    image: str,
    mcp_from_config: bool,
    mcp_config_path: str,
    mcp_services: List[str],
    no_clipboard_bridge: bool,
    no_shell_exec_bridge: bool,
    no_process_control_bridge: bool,
) -> None:
    """Start local edge runtime (WireGuard tunnel + Celery worker, ADR-0095)."""
    compose_file = _get_local_compose_file()
    if not mcp_from_config:
        raise click.ClickException(
            "Legacy --mcp-servers-json startup path was removed. Use --mcp-from-config."
        )
    cfg_path = _resolve_mcp_config_path(compose_file, mcp_config_path)
    from_config = _load_mcp_servers_from_config(cfg_path, list(mcp_services))
    click.echo(f"Loaded {len(from_config)} MCP server config(s) from: {cfg_path}")
    if mcp_services:
        click.echo(f"Filtered services: {', '.join(mcp_services)}")
    service_filter = ",".join([s.strip() for s in mcp_services if s.strip()])
    profile_payload = _load_profile(profile)

    runtime = _effective_runtime_config(
        profile_payload=profile_payload,
        principal_id=principal_id,
        tenant_id=tenant_id,
        image=image,
        mcp_config_path="/app/config/mcp_instance_manager.yaml",
        mcp_service_filter=service_filter,
    )
    read_allow, write_allow = _prepare_local_fs_access(profile=profile, profile_payload=profile_payload)
    if read_allow:
        runtime["MOTET_FILE_READ_ALLOWLIST"] = read_allow
    if write_allow:
        runtime["MOTET_FILE_WRITE_ALLOWLIST"] = write_allow

    _merge_clipboard_bridge_into_runtime(
        runtime, profile, enable=not no_clipboard_bridge
    )
    _merge_shell_bridge_into_runtime(runtime, profile, enable=not no_shell_exec_bridge)
    _merge_process_control_bridge_into_runtime(
        runtime, profile, enable=not no_process_control_bridge
    )

    project_name = _project_name_for_profile(profile, profile_payload)

    click.echo("Mode: WireGuard tunnel (ADR-0095)")
    click.echo(f"  Valkey: {runtime.get('MOTET_VALKEY_URL', 'n/a')}")
    click.echo(f"  Vault:  {runtime.get('MOTET_VAULT_RESOLVE_URL', 'n/a')}")
    if read_allow:
        click.echo(f"  core.file_read allowlist: {read_allow}")
    if write_allow:
        click.echo(f"  core.file_write allowlist: {write_allow}")
    if runtime.get("MOTET_CLIPBOARD_BRIDGE_URL"):
        click.echo(f"  Host clipboard bridge: {runtime['MOTET_CLIPBOARD_BRIDGE_URL']}")
    if runtime.get("MOTET_SHELL_BRIDGE_URL"):
        click.echo(f"  Host shell bridge: {runtime['MOTET_SHELL_BRIDGE_URL']}")
    if runtime.get("MOTET_PROCESS_CONTROL_BRIDGE_URL"):
        click.echo(
            f"  Host process-control bridge: {runtime['MOTET_PROCESS_CONTROL_BRIDGE_URL']}"
        )

    cmd_args = ["--profile", "edge", "up", "-d", "--remove-orphans"]
    if build:
        cmd_args.append("--build")
    cmd = _compose_command(cmd_args, compose_file, project_name=project_name, profile=profile)
    click.echo(f"Using local compose project: {project_name}")
    click.echo(f"$ {' '.join(cmd)}")

    env = os.environ.copy()
    env.update(runtime)
    try:
        _run_compose(cmd, env=env, stream=True)
    except click.ClickException:
        _stop_clipboard_bridge(profile)
        _stop_shell_bridge(profile)
        _stop_process_control_bridge(profile)
        raise

    click.echo("\nLocal device runtime started.")
    click.echo("Use `motet-cli device logs --follow` to watch output.")


@device_group.command("build")
@click.option("--image", default="motet-edge-worker:latest", show_default=True, help="Image tag to build.")
@click.option(
    "--dockerfile",
    default="docker/images/edge-worker/Dockerfile",
    show_default=True,
    help="Path to Dockerfile relative to repo root.",
)
@click.option(
    "--context",
    default=".",
    show_default=True,
    help="Docker build context path relative to repo root.",
)
@click.option(
    "--bootstrap-base/--no-bootstrap-base",
    default=True,
    help="Auto-build motet-worker:latest base image if missing.",
)
@click.option(
    "--base-image",
    default="motet-worker:latest",
    show_default=True,
    help="Base image tag expected by edge-worker Dockerfile.",
)
@click.option(
    "--base-dockerfile",
    default="docker/images/worker/Dockerfile",
    show_default=True,
    help="Path to base worker Dockerfile relative to repo root.",
)
def build_device_runtime(
    image: str,
    dockerfile: str,
    context: str,
    bootstrap_base: bool,
    base_image: str,
    base_dockerfile: str,
) -> None:
    """Build edge worker image for device runtime."""
    repo_root = Path.cwd()
    dockerfile_path = (repo_root / dockerfile).resolve()
    context_path = (repo_root / context).resolve()
    base_dockerfile_path = (repo_root / base_dockerfile).resolve()

    if not dockerfile_path.exists():
        raise click.ClickException(f"Dockerfile not found: {dockerfile_path}")
    if not context_path.exists():
        raise click.ClickException(f"Build context not found: {context_path}")
    if bootstrap_base and not base_dockerfile_path.exists():
        raise click.ClickException(f"Base Dockerfile not found: {base_dockerfile_path}")

    if bootstrap_base and not _docker_image_exists(base_image):
        click.echo(f"Base image not found locally: {base_image}")
        base_cmd = [
            "docker",
            "build",
            "-f",
            str(base_dockerfile_path),
            "-t",
            base_image,
            str(context_path),
        ]
        click.echo(f"$ {' '.join(base_cmd)}")
        base_result = subprocess.run(base_cmd, check=False)
        if base_result.returncode != 0:
            raise click.ClickException(
                f"Base image bootstrap failed for {base_image} (exit code {base_result.returncode})"
            )
        click.echo(f"Bootstrapped base image: {base_image}")

    cmd = [
        "docker",
        "build",
        "-f",
        str(dockerfile_path),
        "-t",
        image,
        str(context_path),
    ]
    click.echo(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise click.ClickException(f"Device image build failed with exit code {result.returncode}")
    click.echo(f"Built image: {image}")


@device_group.command("stop")
@click.option("--profile", default=DEFAULT_DEVICE_PROFILE, show_default=True, help="Profile used for best-effort remote cleanup.")
@api_url_option()
def stop_device_runtime(profile: str, api_url: str) -> None:
    """Stop local edge runtime compose stack."""
    compose_file = _get_local_compose_file()
    worker_cleanup_candidates: Optional[List[str]] = None
    project_name: Optional[str] = None
    try:
        payload = _load_profile(profile)
        project_name = _project_name_for_profile(profile, payload)
        worker_cleanup_candidates = _resolve_local_worker_cleanup_candidates(
            compose_file=compose_file,
            profile=profile,
            profile_payload=payload,
        )
    except click.ClickException:
        payload = None

    cmd = _compose_command(
        ["--profile", "edge", "down", "--remove-orphans"],
        compose_file,
        project_name=project_name,
        profile=profile,
    )
    if project_name:
        click.echo(f"Using local compose project: {project_name}")
    click.echo(f"$ {' '.join(cmd)}")
    _run_compose(cmd, stream=True)
    _stop_clipboard_bridge(profile)
    _stop_shell_bridge(profile)
    _stop_process_control_bridge(profile)
    if not payload:
        return
    click.echo("Running best-effort remote cleanup...")
    _best_effort_remote_cleanup(
        profile_payload=payload,
        api_url=api_url,
        worker_cleanup_candidates=worker_cleanup_candidates,
    )


@device_group.command("status")
@click.option("--profile", default=DEFAULT_DEVICE_PROFILE, show_default=True, help="Profile to display.")
@api_url_option()
def device_status(profile: str, api_url: str) -> None:
    """Show local compose status and API device list for quick diagnostics."""
    compose_file = _get_local_compose_file()
    project_name: Optional[str] = None
    try:
        profile_payload = _load_profile(profile)
        project_name = _project_name_for_profile(profile, profile_payload)
    except click.ClickException:
        profile_payload = None
    ps_cmd = _compose_command(["ps"], compose_file, project_name=project_name, profile=profile)
    if project_name:
        click.echo(f"Using local compose project: {project_name}")
    click.echo(f"$ {' '.join(ps_cmd)}")
    out = _run_compose(ps_cmd, stream=False)
    if out:
        click.echo(out)

    click.echo("")
    try:
        if profile_payload is None:
            profile_payload = _load_profile(profile)
        click.echo("Device profile:")
        click.echo(json.dumps(profile_payload, indent=2))
    except click.ClickException as e:
        click.echo(str(e))

    base = normalize_base_url(api_url)
    try:
        r = api_request(
            "GET",
            f"{base}/api/v1/devices",
            headers=get_api_headers(),
            timeout=API_TIMEOUT_SECONDS,
        )
        click.echo("\nAPI devices:")
        click.echo(json.dumps(r.json(), indent=2))
    except Exception as e:
        click.echo(f"\nAPI device status unavailable: {e}")


@device_group.command("logs")
@click.argument("service", required=False)
@click.option("--follow", is_flag=True, default=False, help="Follow logs.")
@click.option("--tail", default=200, show_default=True, type=int, help="Lines to show.")
@click.option("--profile", default=DEFAULT_DEVICE_PROFILE, show_default=True, help="Profile used to resolve compose project.")
def device_logs(service: Optional[str], follow: bool, tail: int, profile: str) -> None:
    """Show logs for local edge runtime services."""
    compose_file = _get_local_compose_file()
    service_aliases = {
        "local-celery-worker": "worker",
        "tunnel": "wireguard",
    }
    selected_service = service_aliases.get(service, service) if service else None
    args: List[str] = ["logs", "--tail", str(max(0, tail))]
    if follow:
        args.append("--follow")
    if selected_service:
        args.append(selected_service)
    project_name: Optional[str] = None
    try:
        profile_payload = _load_profile(profile)
        project_name = _project_name_for_profile(profile, profile_payload)
    except click.ClickException:
        pass
    cmd = _compose_command(args, compose_file, project_name=project_name, profile=profile)
    if project_name:
        click.echo(f"Using local compose project: {project_name}")
    click.echo(f"$ {' '.join(cmd)}")
    _run_compose(cmd, stream=True)


@device_group.command("doctor")
@click.option("--profile", default=DEFAULT_DEVICE_PROFILE, show_default=True, help="Profile to validate.")
@api_url_option()
def device_doctor(profile: str, api_url: str) -> None:
    """Validate Docker, compose file, profile, and device API connectivity."""
    click.echo("Motet device doctor")
    click.echo("")

    docker_info = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        check=False,
    )
    if docker_info.returncode != 0:
        details = (docker_info.stderr or docker_info.stdout or "unknown error").strip()
        raise click.ClickException(f"Docker daemon unavailable: {details}")
    click.echo("  OK: Docker daemon reachable")

    compose_file = _get_local_compose_file()
    click.echo(f"  OK: Local compose file: {compose_file}")
    override = _get_local_compose_override(compose_file)
    click.echo(
        f"  OK: Local compose override: {override if override else 'not present (optional)'}"
    )

    try:
        payload = _load_profile(profile)
        required = ["device_id", "worker_id", "device_token"]
        required.append("valkey_url")
        missing = [k for k in required if not payload.get(k)]
        if missing:
            raise click.ClickException(
                f"Device profile '{profile}' missing fields: {', '.join(missing)}"
            )
        wg_conf = _wireguard_config_dir(profile) / "wg0.conf"
        if wg_conf.exists():
            click.echo(f"  OK: WireGuard config: {wg_conf}")
        else:
            click.echo(f"  WARN: WireGuard config not found: {wg_conf}")
        hrp = payload.get("host_read_paths")
        if isinstance(hrp, list) and hrp:
            click.echo(f"  OK: host_read_paths: {len(hrp)} extra mount(s) for core.file_read")
        hwp = payload.get("host_write_paths")
        if isinstance(hwp, list) and hwp:
            click.echo(f"  OK: host_write_paths: {len(hwp)} extra mount(s) for core.file_write")
        click.echo(f"  OK: Device profile '{profile}' loaded (WireGuard mode)")
    except click.ClickException as e:
        click.echo(f"  WARN: {e}")

    base = normalize_base_url(api_url)
    try:
        r = api_request(
            "GET",
            f"{base}/api/v1/devices",
            headers=get_api_headers(),
            timeout=API_TIMEOUT_SECONDS,
        )
        rows = r.json()
        count = len(rows) if isinstance(rows, list) else 0
        click.echo(f"  OK: API reachable; /api/v1/devices returned {count} rows")
    except Exception as e:
        click.echo(f"  WARN: API check failed: {e}")


@device_group.command("update")
@click.option("--profile", default=DEFAULT_DEVICE_PROFILE, show_default=True, help="Device profile to run after pull.")
@click.option("--image", default="motet-edge-worker:latest", show_default=True, help="Edge worker image tag.")
@click.option("--restart/--no-restart", default=True, help="Restart edge runtime after pull.")
@click.option(
    "--no-clipboard-bridge",
    is_flag=True,
    default=False,
    help="Do not spawn the host clipboard bridge on restart.",
)
@click.option(
    "--no-shell-exec-bridge",
    is_flag=True,
    default=False,
    help="Do not spawn the host shell bridge on restart.",
)
@click.option(
    "--no-process-control-bridge",
    is_flag=True,
    default=False,
    help="Do not spawn the host process-control bridge on restart.",
)
def update_device_runtime(
    profile: str,
    image: str,
    restart: bool,
    no_clipboard_bridge: bool,
    no_shell_exec_bridge: bool,
    no_process_control_bridge: bool,
) -> None:
    """Pull latest edge worker image and optionally recreate runtime."""
    click.echo(f"$ docker pull {image}")
    pull = subprocess.run(["docker", "pull", image], check=False)
    if pull.returncode != 0:
        raise click.ClickException(f"Failed to pull image {image}")

    if not restart:
        click.echo("Image pulled. Skipping restart (--no-restart).")
        return

    profile_payload = _load_profile(profile)
    runtime = _effective_runtime_config(
        profile_payload=profile_payload,
        principal_id=None,
        tenant_id=None,
        image=image,
        mcp_config_path=os.getenv("MCP_INSTANCE_MANAGER_CONFIG", "/app/config/mcp_instance_manager.yaml"),
        mcp_service_filter=os.getenv("MOTET_EDGE_MCP_SERVICE_FILTER", ""),
    )
    read_allow, write_allow = _prepare_local_fs_access(profile=profile, profile_payload=profile_payload)
    if read_allow:
        runtime["MOTET_FILE_READ_ALLOWLIST"] = read_allow
    if write_allow:
        runtime["MOTET_FILE_WRITE_ALLOWLIST"] = write_allow
    _merge_clipboard_bridge_into_runtime(
        runtime, profile, enable=not no_clipboard_bridge
    )
    _merge_shell_bridge_into_runtime(runtime, profile, enable=not no_shell_exec_bridge)
    _merge_process_control_bridge_into_runtime(
        runtime, profile, enable=not no_process_control_bridge
    )
    project_name = _project_name_for_profile(profile, profile_payload)
    compose_file = _get_local_compose_file()
    cmd = _compose_command(
        ["--profile", "edge", "up", "-d", "--remove-orphans"],
        compose_file,
        project_name=project_name,
        profile=profile,
    )
    click.echo(f"Using local compose project: {project_name}")
    if runtime.get("MOTET_CLIPBOARD_BRIDGE_URL"):
        click.echo(f"  Host clipboard bridge: {runtime['MOTET_CLIPBOARD_BRIDGE_URL']}")
    if runtime.get("MOTET_SHELL_BRIDGE_URL"):
        click.echo(f"  Host shell bridge: {runtime['MOTET_SHELL_BRIDGE_URL']}")
    if runtime.get("MOTET_PROCESS_CONTROL_BRIDGE_URL"):
        click.echo(
            f"  Host process-control bridge: {runtime['MOTET_PROCESS_CONTROL_BRIDGE_URL']}"
        )
    click.echo(f"$ {' '.join(cmd)}")
    env = os.environ.copy()
    env.update(runtime)
    try:
        _run_compose(cmd, env=env, stream=True)
    except click.ClickException:
        _stop_clipboard_bridge(profile)
        _stop_shell_bridge(profile)
        _stop_process_control_bridge(profile)
        raise
    click.echo("Runtime updated.")


__all__ = ["device_group"]
