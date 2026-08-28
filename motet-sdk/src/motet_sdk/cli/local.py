"""
Motet - Local Docker Stack CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-27

Description:
    Local development stack commands for starting, stopping, restarting, and
    inspecting the distributed Docker Compose environment. ``local up`` and
    ``local recreate`` write Redis TLS material under ``tls/`` when that
    directory is missing so the ``redis-tls`` proxy can start on a clean clone.

Dependencies:
    - click: CLI framework
    - subprocess: compose/docker command execution
    - motet_sdk.cli._api: optional workers readiness check for doctor/status
    - motet_sdk.cli._auth: API headers for readiness checks

Usage:
    motet-cli local up
    motet-cli local down
    motet-cli local recreate
    motet-cli local recreate worker-inference
    motet-cli local restart worker-inference
    motet-cli local status
    motet-cli local logs --follow worker-lcm
    motet-cli local doctor

Notes:
    - Uses docker-compose.distributed.yml from the repository root.
    - ``local up`` pulls Motet-bearing images from MOTET_IMAGE_REGISTRY when the
      tag is not local. ``--build`` rebuilds those images from this tree.
    - Defaults to orphan cleanup on down/restart to avoid stale containers.
    - SSO login stores OAuth state in Redis through ``redis-tls``. A missing
      ``tls/`` directory leaves that hostname unresolvable while the proxy
      crash-loops; ``local up`` generates certificates before compose starts.
    - MCP servers started with MOTET_MCP_EXEC_BACKEND=docker are sibling containers on
      the Docker host, not Compose services; ``local down`` removes them via label
      motet.mcp after compose down unless ``--no-mcp-docker-cleanup`` is set.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
from typing import List

import click

from motet_sdk._version import get_version

from ._api import api_request, api_url_option, normalize_base_url
from ._auth import get_api_headers

DEFAULT_IMAGE_REGISTRY = "ghcr.io/motet-ai"


def _ensure_image_pin_env() -> None:
    """Set compose image pins from the product version when unset."""
    os.environ.setdefault("MOTET_IMAGE_REGISTRY", DEFAULT_IMAGE_REGISTRY)
    if not (os.environ.get("MOTET_IMAGE_TAG") or "").strip():
        os.environ["MOTET_IMAGE_TAG"] = f"v{get_version()}"


def _get_compose_file() -> Path:
    """Resolve compose file: env, then cwd/parents (repo root), then SDK-bundled stub."""
    env_path = os.environ.get("MOTET_COMPOSE_FILE") or os.environ.get("MOTET_COMPOSE_FILE")
    if env_path:
        p = Path(env_path).resolve()
        if p.exists():
            return p
    # When running from repo: look in cwd and parents for docker-compose.distributed.yml
    candidate = Path.cwd()
    for _ in range(10):
        repo_file = candidate / "docker-compose.distributed.yml"
        if repo_file.exists():
            return repo_file
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    # SDK-bundled developer compose (stub; often commented out — set MOTET_COMPOSE_FILE in repo)
    try:
        from importlib.resources import files
        p = files("motet_sdk.docker").joinpath("docker-compose.developer.yml")
        if p.is_file():
            return Path(str(p))
    except Exception:
        pass
    return Path.cwd() / "docker-compose.distributed.yml"  # fallback for error message


def _compose_binary() -> List[str]:
    """Prefer `docker compose`; fall back to `docker-compose`."""
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

    raise click.ClickException(
        "Neither 'docker compose' nor 'docker-compose' is available on PATH."
    )


def _compose_command(args: List[str]) -> List[str]:
    """Build compose command against distributed stack file."""
    _ensure_image_pin_env()
    compose_file = _get_compose_file()
    if not compose_file.exists():
        raise click.ClickException(
            f"Compose file not found: {compose_file}"
        )
    return [*_compose_binary(), "-f", str(compose_file), *args]


def _tls_material_present(repo_root: Path) -> bool:
    """Return True when stunnel can load a Redis server certificate from tls/."""
    tls_dir = repo_root / "tls"
    return (tls_dir / "redis.crt").is_file() and (tls_dir / "redis.key").is_file()


def _generate_minimal_tls_certs(tls_dir: Path) -> None:
    """Write a self-signed Redis server certificate with host openssl."""
    openssl = shutil.which("openssl")
    if openssl is None:
        raise click.ClickException(
            "openssl is required to generate tls/ certificates for redis-tls. "
            "Install openssl, or run docker/redis/generate-tls-certs.sh."
        )
    tls_dir.mkdir(parents=True, exist_ok=True)
    redis_key = tls_dir / "redis.key"
    redis_crt = tls_dir / "redis.crt"
    result = subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(redis_key),
            "-out",
            str(redis_crt),
            "-days",
            "365",
            "-subj",
            "/CN=redis-tls/O=Motet/C=US",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "unknown error").strip()
        raise click.ClickException(f"Failed to generate tls/ certificates: {details}")
    ca_crt = tls_dir / "ca.crt"
    if not ca_crt.exists():
        shutil.copyfile(redis_crt, ca_crt)
    redis_key.chmod(0o600)
    redis_crt.chmod(0o644)
    ca_crt.chmod(0o644)


def _ensure_local_tls_certs(repo_root: Path | None = None) -> None:
    """Create tls/ on a clean clone so redis-tls has certificates to load."""
    root = repo_root or _get_compose_file().parent
    if _tls_material_present(root):
        return
    script = root / "docker" / "redis" / "generate-tls-certs.sh"
    click.echo("Generating local Redis TLS certificates (tls/ is missing on a clean clone)...")
    if script.is_file() and shutil.which("bash") is not None:
        result = subprocess.run(
            ["bash", str(script)],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and _tls_material_present(root):
            return
        details = (result.stderr or result.stdout or "unknown error").strip()
        click.echo(f"generate-tls-certs.sh failed ({details}); trying openssl directly.")
    _generate_minimal_tls_certs(root / "tls")
    if not _tls_material_present(root):
        raise click.ClickException(
            "Failed to create tls/redis.crt and tls/redis.key required by redis-tls."
        )


def _cleanup_motet_mcp_docker_containers() -> int:
    """
    Force-remove containers tagged by the Motet MCP Docker backend.

    These are created via the Engine API inside worker containers, so they are not
    removed by ``docker compose down`` or ``--remove-orphans``.
    """
    ls = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=motet.mcp=1"],
        capture_output=True,
        text=True,
        check=False,
    )
    if ls.returncode != 0:
        return 0
    ids = [x.strip() for x in (ls.stdout or "").splitlines() if x.strip()]
    removed = 0
    for cid in ids:
        rm = subprocess.run(
            ["docker", "rm", "-f", cid],
            capture_output=True,
            text=True,
            check=False,
        )
        if rm.returncode == 0:
            removed += 1
    return removed


def _run_or_fail(cmd: List[str], stream: bool = False) -> str:
    """Run command and raise ClickException on failure.

    When stream=True, stdout/stderr are not captured so progress (e.g. build output)
    is shown live. On failure a generic message is raised; the user already saw the output.
    """
    if stream:
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            raise click.ClickException(
                "Docker Compose failed (see output above)."
            )
        return ""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "unknown error").strip()
        raise click.ClickException(details)
    return (result.stdout or "").strip()


def _workers_readiness_summary(api_url: str) -> str:
    """Return a one-line readiness summary for local status/doctor."""
    base = normalize_base_url(api_url)
    try:
        resp = api_request(
            "GET",
            f"{base}/api/v1/workers/readiness",
            headers=get_api_headers(),
            timeout=10,
        )
        payload = resp.json()
    except Exception as e:  # surfaced as warning text for status UX
        hint = ""
        if "404" in str(e):
            hint = " (check API logs for 'Failed to load API v1 routers'; ensure motet-api container started without import errors)"
        return f"workers readiness unavailable ({e}){hint}"

    stats = payload.get("system_stats", {}) if isinstance(payload, dict) else {}
    workers = payload.get("workers", {}) if isinstance(payload, dict) else {}
    total = stats.get("total_workers", len(workers) if isinstance(workers, dict) else 0)
    ready = stats.get("ready_workers", 0)
    has_lifecycle = isinstance(workers, dict) and "cloud_lifecycle_management" in workers
    lifecycle = "present" if has_lifecycle else "missing"
    return f"workers ready {ready}/{total}; lifecycle worker {lifecycle}"


def _open_url(url: str) -> None:
    """Open URL in default browser across supported platforms."""
    system = platform.system().lower()
    if system == "darwin":
        cmd = ["open", url]
    elif system == "windows":
        cmd = ["cmd", "/c", "start", "", url]
    else:
        cmd = ["xdg-open", url]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "unknown error").strip()
        raise click.ClickException(f"Failed to open browser for {url}: {details}")


def _wait_for_manage_ready(api_url: str, timeout_seconds: int) -> None:
    """Wait until API health endpoint is reachable or timeout expires."""
    base = normalize_base_url(api_url)
    deadline = time.time() + max(timeout_seconds, 1)
    last_error: str = ""

    while time.time() < deadline:
        try:
            api_request(
                "GET",
                f"{base}/health",
                headers=get_api_headers(),
                timeout=3,
            )
            return
        except Exception as e:
            last_error = str(e)
            time.sleep(1.0)

    raise click.ClickException(
        f"Timed out waiting for API health at {base}/health ({timeout_seconds}s). Last error: {last_error}"
    )


@click.group("local")
def local_group() -> None:
    """Manage the local distributed Docker stack."""
    pass


@local_group.command("up")
@click.option(
    "--build",
    is_flag=True,
    default=False,
    help="Rebuild Motet images from this tree. Default is pull-first from MOTET_IMAGE_REGISTRY.",
)
@click.option("--profile", "profiles", multiple=True, help="Compose profile(s) to enable.")
def up_cmd(build: bool, profiles: tuple[str, ...]) -> None:
    """Start the local stack (pull published images; --build rebuilds from source)."""
    _ensure_local_tls_certs()
    args: List[str] = ["up", "-d"]
    if build:
        args.append("--build")
    for profile in profiles:
        args.extend(["--profile", profile])
    cmd = _compose_command(args)
    click.echo(f"$ {' '.join(cmd)}")
    _run_or_fail(cmd, stream=True)


@local_group.command("down")
@click.option("--volumes", is_flag=True, default=False, help="Also remove named volumes.")
@click.option(
    "--remove-orphans/--no-remove-orphans",
    default=True,
    help="Remove orphan containers (default: enabled).",
)
@click.option(
    "--mcp-docker-cleanup/--no-mcp-docker-cleanup",
    default=True,
    show_default=True,
    help=(
        "After compose down, docker rm -f containers with label motet.mcp=1 "
        "(MCP sidecars are not Compose services)."
    ),
)
def down_cmd(volumes: bool, remove_orphans: bool, mcp_docker_cleanup: bool) -> None:
    """Stop local distributed stack."""
    args: List[str] = ["down"]
    if remove_orphans:
        args.append("--remove-orphans")
    if volumes:
        args.append("-v")
    cmd = _compose_command(args)
    click.echo(f"$ {' '.join(cmd)}")
    _run_or_fail(cmd, stream=True)
    if mcp_docker_cleanup:
        n = _cleanup_motet_mcp_docker_containers()
        if n:
            click.echo(
                f"Removed {n} Motet MCP Docker container(s) (label motet.mcp=1); "
                "these are not managed by Compose."
            )


@local_group.command("recreate")
@click.option(
    "--build",
    is_flag=True,
    default=False,
    help="Rebuild Motet images from this tree. Default is pull-first from MOTET_IMAGE_REGISTRY.",
)
@click.option("--profile", "profiles", multiple=True, help="Compose profile(s) to enable.")
@click.argument("services", nargs=-1)
def recreate_cmd(build: bool, profiles: tuple[str, ...], services: tuple[str, ...]) -> None:
    """Force-recreate containers in a single pass (faster than down+up).

    Optionally specify SERVICE names to recreate only those services.
    """
    _ensure_local_tls_certs()
    args: List[str] = ["up", "-d", "--force-recreate", "--remove-orphans"]
    if build:
        args.append("--build")
    for profile in profiles:
        args.extend(["--profile", profile])
    if services:
        args.extend(services)
    cmd = _compose_command(args)
    click.echo(f"$ {' '.join(cmd)}")
    _run_or_fail(cmd, stream=True)


@local_group.command("restart")
@click.argument("services", nargs=-1)
def restart_cmd(services: tuple[str, ...]) -> None:
    """Restart running containers in-place without recreating them.

    Sends stop+start to the specified services (or all if none given).
    Fast, but does not pick up image or config changes.
    """
    cmd = _compose_command(["restart", *services])
    click.echo(f"$ {' '.join(cmd)}")
    output = _run_or_fail(cmd)
    if output:
        click.echo(output)


@local_group.command("status")
@api_url_option()
def status_cmd(api_url: str) -> None:
    """Show container status and workers readiness summary."""
    cmd = _compose_command(["ps"])
    click.echo(f"$ {' '.join(cmd)}")
    output = _run_or_fail(cmd)
    if output:
        click.echo(output)
    click.echo("")
    click.echo(f"Readiness: {_workers_readiness_summary(api_url)}")


@local_group.command("logs")
@click.argument("service", required=False)
@click.option("--follow", is_flag=True, default=False, help="Follow logs.")
@click.option("--tail", default=200, show_default=True, type=int, help="Number of lines to show.")
def logs_cmd(service: str | None, follow: bool, tail: int) -> None:
    """Show stack logs for all services or one service."""
    args: List[str] = ["logs", "--tail", str(max(tail, 0))]
    if follow:
        args.append("--follow")
    if service:
        args.append(service)
    cmd = _compose_command(args)
    click.echo(f"$ {' '.join(cmd)}")
    # Intentionally stream in foreground for logs UX.
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        raise click.ClickException(f"Logs command failed with exit code {proc.returncode}")


@local_group.command("doctor")
@api_url_option()
def doctor_cmd(api_url: str) -> None:
    """Validate Docker/compose availability and workers readiness."""
    click.echo("Motet local doctor")
    click.echo("")

    # Docker daemon reachability
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

    # Compose availability + stack visibility
    ps_cmd = _compose_command(["ps", "--format", "json"])
    result = subprocess.run(
        ps_cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "unknown error").strip()
        raise click.ClickException(f"Compose stack check failed: {details}")

    services = []
    raw = (result.stdout or "").strip()
    if raw:
        try:
            services = json.loads(raw)
        except Exception:
            services = []
    count = len(services) if isinstance(services, list) else 0
    click.echo(f"  OK: Compose stack query succeeded ({count} services listed)")
    click.echo(f"  Readiness: {_workers_readiness_summary(api_url)}")


@local_group.command("manage")
@click.option(
    "--url",
    default="http://localhost:8000/manage",
    show_default=True,
    help="Manage UI URL to open.",
)
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Wait for API health before opening browser.",
)
@click.option(
    "--timeout",
    default=60,
    show_default=True,
    type=int,
    help="Seconds to wait when --wait is enabled.",
)
@click.option(
    "--print-only",
    is_flag=True,
    default=False,
    help="Only print the URL; do not open browser.",
)
@api_url_option()
def manage_cmd(url: str, wait: bool, timeout: int, print_only: bool, api_url: str) -> None:
    """Open the local manage UI in a browser."""
    click.echo(f"Manage UI: {url}")
    if print_only:
        return

    if wait:
        click.echo(f"Waiting for API health (timeout={timeout}s)...")
        _wait_for_manage_ready(api_url, timeout)

    _open_url(url)
    click.echo("Opened browser.")


__all__ = ["local_group"]

