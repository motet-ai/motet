"""
Motet - Bundle CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Developer experience for bundles: init (scaffold), lint (run linter locally),
    and hot deploy for local Docker iteration. Use motet-cli deploy dir-deploy
    or deploy git-deploy for deployment. Respects optional ``.bundleignore``
    (host-only paths such as product ``cli/`` / ``deploy/``).

Dependencies:
    - click: CLI framework

Usage:
    motet-cli bundle init [PATH]     # Scaffold a new bundle directory
    motet-cli bundle lint [PATH]     # Run the bundle linter locally
    motet-cli bundle hot-deploy [PATH] [--no-watch]
                                    # Dev-only hot deploy via Mutagen sync (default: watch)
                                    #   --containers: cached auto-discovery (or live discovery)
                                    #   --remote-path: /tmp/imf_dev/<user>/<bundle_name>
                                    #   --disable-discovered-container-caching to ignore cache
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
from typing import Dict, List, Optional, Tuple

import click

from ._api import api_url_option
from ._config import (
    get_cli_config,
    set_cli_config_value,
)


@click.group("bundle")
def bundle_group() -> None:
    """Bundle developer experience: init, lint, hot-deploy (sync)."""
    pass


_BUNDLE_MANIFEST = """format_version: "1"
name: "{name}"
version: "0.1.0"
description: "{description}"
"""


@bundle_group.command("init")
@click.argument(
    "path",
    type=click.Path(exists=False, file_okay=False, dir_okay=True),
    default=".",
)
@click.option("--name", "-n", help="Bundle name (slug); default from directory name")
@click.option("--description", "-d", default="My bundle", help="Short description")
def init_cmd(path: str, name: Optional[str], description: str) -> None:
    """Scaffold a new bundle directory (manifest.yaml, commands/, tools/, agents/, workflows/, config/)."""
    root = Path(path).resolve()
    if root.exists() and any(root.iterdir()):
        click.echo(f"❌ Directory is not empty: {root}", err=True)
        raise SystemExit(1)
    slug = name or root.name.replace(" ", "-").lower()
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.yaml").write_text(
        _BUNDLE_MANIFEST.format(name=slug, description=description),
        encoding="utf-8",
    )
    for dirname in ("commands", "tools", "agents", "workflows", "config"):
        (root / dirname).mkdir(exist_ok=True)
        (root / dirname / ".gitkeep").touch()
    click.echo(f"✅ Bundle scaffolded at {root}")
    click.echo(f"   name: {slug}")
    click.echo(f"   Next: edit manifest.yaml and add commands in commands/, then motet-cli bundle lint")


def _load_bundleignore_prefixes(root: Path) -> List[str]:
    """
    Load path prefixes from ``.bundleignore`` (gitignore-like, prefix match).

    Lines are relative to the bundle root. A trailing ``/`` means a directory
    prefix (``cli/`` ignores ``cli/**``). Empty lines and ``#`` comments skip.
    Always ignores ``.git`` and ``__pycache__``.
    """
    prefixes: List[str] = [".git/", "__pycache__/"]
    ignore_file = root / ".bundleignore"
    if not ignore_file.is_file():
        return prefixes
    try:
        for raw in ignore_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # Normalize to forward-slash relative prefixes.
            norm = line.replace("\\", "/").lstrip("./")
            if not norm:
                continue
            if not norm.endswith("/") and (root / norm).is_dir():
                norm = f"{norm}/"
            prefixes.append(norm)
    except OSError:
        pass
    return prefixes


def _is_bundleignored(rel: str, prefixes: List[str]) -> bool:
    path = rel.replace("\\", "/")
    for prefix in prefixes:
        if prefix.endswith("/"):
            if path == prefix[:-1] or path.startswith(prefix):
                return True
        elif path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _read_bundle_files_local(root: Path) -> dict:
    """Read bundle files from a local directory into {rel_path: bytes}."""
    prefixes = _load_bundleignore_prefixes(root)
    files: dict = {}
    for fp in root.rglob("*"):
        if not fp.is_file():
            continue
        rel = str(fp.relative_to(root))
        if _is_bundleignored(rel, prefixes):
            continue
        files[rel] = fp.read_bytes()
    return files


def _collect_bundle_file_state(root: Path) -> Dict[str, Tuple[float, int]]:
    """Collect a lightweight fingerprint of bundle files for watch mode."""
    prefixes = _load_bundleignore_prefixes(root)
    state: Dict[str, Tuple[float, int]] = {}
    for fp in root.rglob("*"):
        if not fp.is_file():
            continue
        try:
            rel = str(fp.relative_to(root))
            if _is_bundleignored(rel, prefixes):
                continue
            stat = fp.stat()
            state[rel] = (stat.st_mtime, stat.st_size)
        except OSError:
            # File may be deleted/updated between glob and stat in active edits.
            continue
    return state


def _submit_hot_deploy(api_url: str, bundle_path: str, lint: bool) -> dict:
    """Submit hot deploy request and return parsed JSON payload."""
    from ._api import api_request
    from ._auth import get_api_headers

    base = api_url.rstrip("/")
    payload = {
        "bundle_path": bundle_path,
        "lint": lint,
    }
    r = api_request(
        "POST",
        f"{base}/api/v1/deploy/hot",
        headers=get_api_headers(),
        json=payload,
        timeout=120,
    )
    return r.json()


def _run_hot_deploy_loop(
    *,
    root: Path,
    worker_path: str,
    lint: bool,
    watch: bool,
    interval: float,
    api_url: str,
) -> None:
    """Run one-shot or watch-loop hot deploy flow."""

    def _run_once() -> None:
        data = _submit_hot_deploy(api_url, worker_path, lint)
        click.echo("✅ Hot deploy job accepted (202)")
        click.echo(f"   deploy_job_id: {data.get('deploy_job_id')}")
        click.echo(f"   bundle_id: {data.get('bundle_id')}")
        click.echo(f"   bundle_version: {data.get('bundle_version')}")
        click.echo(f"   status: {data.get('status')}")

    _run_once()
    if not watch:
        return

    click.echo(f"👀 Watching {root} for changes (interval={interval:.1f}s). Ctrl+C to stop.")
    previous_state = _collect_bundle_file_state(root)
    while True:
        time.sleep(max(interval, 0.1))
        current_state = _collect_bundle_file_state(root)
        if current_state != previous_state:
            click.echo("🔁 Change detected, hot deploying...")
            _run_once()
            previous_state = current_state


def _mutagen_run(args: List[str]) -> subprocess.CompletedProcess[str]:
    """Run one mutagen command and capture output."""
    return subprocess.run(
        ["mutagen", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _mutagen_session_name(prefix: str, container: str, remote_path: str) -> str:
    """Build a stable, mutagen-safe session name."""
    # Mutagen session names reject underscores; keep only alnum and hyphen.
    safe_container = re.sub(r"[^a-zA-Z0-9-]+", "-", container).strip("-") or "container"
    suffix = hashlib.sha1(remote_path.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{safe_container}-{suffix}"


def _ensure_mutagen_available() -> None:
    """Ensure mutagen CLI is available."""
    try:
        probe = _mutagen_run(["version"])
    except FileNotFoundError as e:
        raise click.ClickException(
            "Mutagen CLI not found. Install Mutagen to use hot-deploy:\n\n"
            "  macOS / Linux (Homebrew):\n"
            "    brew install mutagen-io/mutagen/mutagen\n\n"
            "  Or download from: https://github.com/mutagen-io/mutagen/releases\n\n"
            "Then run: motet-cli bundle hot-deploy ."
        ) from e
    if probe.returncode != 0:
        details = (probe.stderr or probe.stdout or "unknown error").strip()
        raise click.ClickException(f"Mutagen CLI is unavailable: {details}")


def _ensure_mutagen_sessions(
    *,
    local_path: Path,
    remote_path: str,
    containers: List[str],
    session_prefix: str,
) -> None:
    """Create or reuse mutagen sync sessions for all target containers."""
    for container in containers:
        session_name = _mutagen_session_name(session_prefix, container, remote_path)
        target = f"docker://{container}{remote_path}"
        create = _mutagen_run(["sync", "create", "--name", session_name, str(local_path), target])
        if create.returncode == 0:
            click.echo(f"🔄 Mutagen session created: {session_name} -> {target}")
            continue
        combined = f"{create.stdout}\n{create.stderr}".lower()
        if "already exists" in combined:
            click.echo(f"ℹ️ Reusing existing Mutagen session: {session_name}")
            continue
        details = (create.stderr or create.stdout or "unknown error").strip()
        raise click.ClickException(
            f"Failed to create Mutagen session '{session_name}' for container '{container}': {details}"
        )


def _ensure_remote_paths_exist(containers: List[str], remote_path: str) -> None:
    """Ensure remote sync path exists inside each target container."""
    for container in containers:
        cmd = [
            "docker",
            "exec",
            container,
            "sh",
            "-lc",
            f"mkdir -p {shlex.quote(remote_path)}",
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "unknown error").strip()
            raise click.ClickException(
                f"Failed to create remote path '{remote_path}' in container '{container}': {details}"
            )


def _derive_bundle_name(root: Path) -> str:
    """Best-effort bundle name from manifest.yaml, falling back to folder name."""
    manifest = root / "manifest.yaml"
    try:
        raw = manifest.read_text(encoding="utf-8")
    except Exception:
        return root.name

    match = re.search(r'^\s*name:\s*["\']?([A-Za-z0-9._-]+)["\']?\s*$', raw, flags=re.MULTILINE)
    if match:
        return match.group(1).strip()
    return root.name


def _default_remote_path(root: Path) -> str:
    """Construct a sensible default remote sync path."""
    username = (os.getenv("USER") or os.getenv("USERNAME") or "dev").strip() or "dev"
    bundle_name = _derive_bundle_name(root).strip() or root.name
    return f"/tmp/imf_dev/{username}/{bundle_name}"


def _discover_default_containers(api_url: str) -> List[str]:
    """Discover default target containers from worker health API."""
    from ._api import api_request, normalize_base_url
    from ._auth import get_api_headers

    base = normalize_base_url(api_url)
    response = api_request(
        "GET",
        f"{base}/api/v1/workers/health",
        headers=get_api_headers(),
        timeout=30,
    )
    payload = response.json()
    worker_health = payload.get("worker_health", {}) if isinstance(payload, dict) else {}
    if not isinstance(worker_health, dict) or not worker_health:
        raise click.ClickException(
            "Could not discover worker containers from /api/v1/workers/health. "
            "Provide --containers explicitly."
        )

    healthy = [wid for wid, meta in worker_health.items() if isinstance(meta, dict) and meta.get("healthy") is True]
    candidates = healthy or list(worker_health.keys())
    containers = [str(w).strip() for w in candidates if str(w).strip()]
    if not containers:
        raise click.ClickException("No worker containers discovered. Provide --containers explicitly.")
    return containers


def _get_cached_discovered_containers() -> List[str]:
    """Load cached discovered containers from CLI config."""
    cfg = get_cli_config()
    raw = cfg.get("hot_deploy_discovered_containers")
    if not isinstance(raw, list):
        return []
    return [str(c).strip() for c in raw if str(c).strip()]


def _cache_discovered_containers(containers: List[str]) -> None:
    """Persist discovered containers in CLI config."""
    set_cli_config_value("hot_deploy_discovered_containers", containers)


def _extract_imf_worker_id_from_env(env_items: List[str]) -> Optional[str]:
    """Extract MOTET_WORKER_ID from docker inspect env list."""
    for item in env_items:
        if item.startswith("MOTET_WORKER_ID="):
            value = item.split("=", 1)[1].strip()
            return value or None
    return None


def _list_running_docker_container_names() -> List[str]:
    """Return currently running Docker container names."""
    try:
        ps = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return []
    except Exception:
        return []

    if ps.returncode != 0:
        return []
    return [line.strip() for line in ps.stdout.splitlines() if line.strip()]


def _has_unresolved_container_names(original: List[str], resolved: List[str]) -> bool:
    """Return true when names that looked like containers were not found in Docker."""
    running = set(_list_running_docker_container_names())
    if not running:
        return False
    for original_name, resolved_name in zip(original, resolved):
        if original_name != resolved_name:
            continue
        if original_name in running:
            continue
        if original_name.startswith(("cloud_", "worker", "lifecycle_management")):
            continue
        return True
    return False


def _resolve_worker_ids_to_container_names(worker_ids_or_containers: List[str]) -> List[str]:
    """
    Resolve worker IDs (e.g. cloud_worker1) to Docker container names.

    Uses `docker ps` + `docker inspect` to map MOTET_WORKER_ID values to the
    corresponding running container name. Any unresolved entries are returned
    unchanged so explicit container names continue to work.
    """
    container_names = _list_running_docker_container_names()
    if not container_names:
        return worker_ids_or_containers

    worker_to_containers: Dict[str, List[str]] = {}
    for container in container_names:
        inspect = subprocess.run(
            ["docker", "inspect", container, "--format", "{{json .Config.Env}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if inspect.returncode != 0 or not inspect.stdout.strip():
            continue
        try:
            env_items = json.loads(inspect.stdout.strip())
        except Exception:
            continue
        if not isinstance(env_items, list):
            continue
        imf_worker_id = _extract_imf_worker_id_from_env([str(x) for x in env_items])
        if not imf_worker_id:
            continue
        # get_worker_id() prefixes MOTET_WORKER_ID with cloud_
        worker_to_containers.setdefault(f"cloud_{imf_worker_id}", []).append(container)
        worker_to_containers.setdefault(imf_worker_id, []).append(container)

    def _pick_best_container(candidates: List[str]) -> str:
        """
        Pick the best container when multiple map to the same worker ID.

        Prefer the local dev compose stack over test stacks when both are running.
        """
        if not candidates:
            return ""
        preferred_prefixes = ("motet_dev-", "imf_dev-", "imf-", "imf-test-")

        def _score(name: str) -> tuple[int, str]:
            prefix_rank = next((idx for idx, p in enumerate(preferred_prefixes) if name.startswith(p)), len(preferred_prefixes))
            return (prefix_rank, name)

        return sorted(candidates, key=_score)[0]

    resolved: List[str] = []
    for item in worker_ids_or_containers:
        if item in container_names:
            resolved.append(item)
        else:
            candidates = worker_to_containers.get(item, [])
            resolved.append(_pick_best_container(candidates) if candidates else item)
    return resolved


@bundle_group.command("lint")
@click.argument(
    "path",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    default=".",
)
def lint_cmd(path: str) -> None:
    """Run the bundle linter locally (same checks as deploy validate)."""
    root = Path(path).resolve()
    manifest = root / "manifest.yaml"
    if not manifest.exists():
        click.echo(f"❌ No manifest.yaml in {root}", err=True)
        raise SystemExit(1)
    try:
        from motet.core.bundles.deploy import _lint_bundle
        bundle_files = _read_bundle_files_local(root)
        passed, all_errors = _lint_bundle(bundle_files)
        for e in all_errors:
            prefix = "❌" if e.severity == "error" else "⚠️"
            click.echo(f"  {prefix} {e.file}:{e.line} — {e.message}")
        if not passed:
            raise SystemExit(1)
        click.echo("✅ Lint passed")
    except ImportError as e:
        click.echo(f"❌ Could not load linter: {e}", err=True)
        raise SystemExit(1)


@bundle_group.command("hot-deploy")
@click.argument(
    "path",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    default=".",
)
@click.option(
    "--containers",
    default=None,
    help="Comma-separated target container names for Mutagen sync (default: cached discovery, then auto-discover).",
)
@click.option(
    "--remote-path",
    default=None,
    help="Container path where the bundle is synced (default: /tmp/imf_dev/<user>/<bundle_name>).",
)
@click.option(
    "--session-prefix",
    default="imf-sync",
    show_default=True,
    help="Prefix used for generated Mutagen session names.",
)
@click.option(
    "--disable-discovered-container-caching",
    is_flag=True,
    default=False,
    help="Disable reading/writing cached auto-discovered containers; always discover when --containers is omitted.",
)
@click.option(
    "--lint/--no-lint",
    default=False,
    help="Run lint before hot reload (default: --no-lint for speed).",
)
@click.option(
    "--watch/--no-watch",
    default=True,
    help="Watch local files and hot deploy on change (default: --watch).",
)
@click.option(
    "--interval",
    default=1.0,
    type=float,
    show_default=True,
    help="Watch polling interval in seconds.",
)
@api_url_option()
def hot_deploy_cmd(
    path: str,
    containers: Optional[str],
    remote_path: Optional[str],
    session_prefix: str,
    disable_discovered_container_caching: bool,
    lint: bool,
    watch: bool,
    interval: float,
    api_url: str,
) -> None:
    """
    Dev-only hot deploy with Mutagen sync transport.

    Creates/reuses one Mutagen sync session per target container and then runs the
    standard hot deploy flow against the synced worker path.
    """
    root = Path(path).resolve()
    manifest = root / "manifest.yaml"
    if not manifest.exists():
        click.echo(f"❌ No manifest.yaml in {root}", err=True)
        raise SystemExit(1)

    if containers:
        parsed_containers = [c.strip() for c in containers.split(",") if c.strip()]
    else:
        parsed_containers = []
        cached_containers_were_stale = False
        if not disable_discovered_container_caching:
            parsed_containers = _get_cached_discovered_containers()
            if parsed_containers:
                click.echo(f"ℹ️ Using cached discovered containers: {', '.join(parsed_containers)}")
                resolved_cached_containers = _resolve_worker_ids_to_container_names(parsed_containers)
                if _has_unresolved_container_names(parsed_containers, resolved_cached_containers):
                    cached_containers_were_stale = True
                    click.echo(
                        "ℹ️ Cached discovered containers are no longer running; refreshing discovery."
                    )
                    parsed_containers = []
                else:
                    parsed_containers = resolved_cached_containers
        if not parsed_containers:
            parsed_containers = _discover_default_containers(api_url)
            click.echo(f"ℹ️ Auto-discovered containers: {', '.join(parsed_containers)}")
            if not disable_discovered_container_caching:
                _cache_discovered_containers(parsed_containers)
                if cached_containers_were_stale:
                    click.echo("ℹ️ Refreshed cached discovered containers for future hot-deploy runs.")
                else:
                    click.echo("ℹ️ Cached discovered containers for future hot-deploy runs.")

    resolved_containers = _resolve_worker_ids_to_container_names(parsed_containers)
    if resolved_containers != parsed_containers:
        click.echo(f"ℹ️ Resolved Docker containers: {', '.join(resolved_containers)}")
    parsed_containers = resolved_containers

    if not parsed_containers:
        raise click.UsageError("--containers must include at least one container name.")

    effective_remote_path = remote_path or _default_remote_path(root)
    if remote_path is None:
        click.echo(f"ℹ️ Using default remote path: {effective_remote_path}")

    if not effective_remote_path.startswith("/"):
        raise click.UsageError("--remote-path must be an absolute container path starting with '/'.")

    try:
        _ensure_mutagen_available()
        _ensure_remote_paths_exist(parsed_containers, effective_remote_path)
        _ensure_mutagen_sessions(
            local_path=root,
            remote_path=effective_remote_path,
            containers=parsed_containers,
            session_prefix=session_prefix,
        )
        click.echo(f"ℹ️ Using synced worker path: {effective_remote_path}")
        _run_hot_deploy_loop(
            root=root,
            worker_path=effective_remote_path,
            lint=lint,
            watch=watch,
            interval=interval,
            api_url=api_url,
        )
    except KeyboardInterrupt:
        click.echo("\nStopped watch mode.")
        return
    except click.ClickException:
        raise
    except Exception as e:
        click.echo(f"❌ Hot deploy failed: {e}", err=True)
        raise SystemExit(1)
