"""
Motet - Deploy CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    CLI for bundle deployment — one-to-one with /api/v1/deploy.
    git-deploy (repo/branch/path), dir-deploy (zip local bundle), list, status,
    validate, propagate, rollback, undeploy, history.

Dependencies:
    - click: CLI framework
    - requests: API communication

Usage:
    motet-cli deploy git-deploy --repo-url URL --branch BRANCH --path PATH
    motet-cli deploy dir-deploy [PATH]   # zip local bundle, POST /api/v1/deploy/upload
    motet-cli deploy list
    motet-cli deploy status <bundle_id> --job-id <id>
    motet-cli deploy validate --repo-url URL --branch BRANCH --path PATH
    motet-cli deploy propagate <bundle_id>
    motet-cli deploy rollback <bundle_id> --version <bundle_version>
    motet-cli deploy undeploy <bundle_id>
    motet-cli deploy history <bundle_id>
"""

from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional

import click
import requests

from ._api import api_request, api_url_option
from ._auth import get_api_headers

UPLOAD_MAX_BYTES = 100 * 1024 * 1024  # 100MB, matches API limit


@click.group("deploy")
def deploy_group() -> None:
    """Bundle deployment (POST/GET /api/v1/deploy)."""
    pass


@deploy_group.command("git-deploy")
@click.option("--repo-url", required=True, help="Git repository URL")
@click.option("--branch", required=True, help="Branch, tag, or commit SHA to deploy")
@click.option("--path", required=True, help="Path within repo (bundle root)")
@click.option("--interactive", is_flag=True, help="Create deployment conversation for persona/narration")
@api_url_option()
def git_deploy_cmd(
    repo_url: str,
    branch: str,
    path: str,
    interactive: bool,
    api_url: str,
) -> None:
    """POST /api/v1/deploy — deploy from git (clone on server)."""
    try:
        url = f"{api_url.rstrip('/')}/api/v1/deploy"
        payload = {"repo_url": repo_url, "branch": branch, "path": path, "interactive": interactive}
        headers = get_api_headers()
        response = api_request("POST", url, headers=headers, json=payload, timeout=120)
        result = response.json()
        click.echo("✅ Deploy job accepted (202)")
        click.echo(f"   deploy_job_id: {result.get('deploy_job_id', '')}")
        click.echo(f"   bundle_id: {result.get('bundle_id', '')}")
        click.echo(f"   status_url: {result.get('status_url', '')}")
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"❌ Could not connect to API at {api_url}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise click.Abort()


def _zip_bundle_root(root: Path) -> bytes:
    """Zip bundle directory with manifest at root; honor ``.bundleignore``."""
    from .bundle import _is_bundleignored, _load_bundleignore_prefixes

    prefixes = _load_bundleignore_prefixes(root)
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in root.rglob("*"):
            if not file_path.is_file():
                continue
            arcname = str(file_path.relative_to(root))
            if _is_bundleignored(arcname, prefixes):
                continue
            zf.write(file_path, arcname)
    return buf.getvalue()


@deploy_group.command("dir-deploy")
@click.argument(
    "path",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    default=".",
)
@api_url_option()
def dir_deploy_cmd(path: str, api_url: str) -> None:
    """Zip local bundle and POST /api/v1/deploy/upload (no git on server)."""
    root = Path(path).resolve()
    manifest = root / "manifest.yaml"
    if not manifest.exists():
        click.echo(f"❌ No manifest.yaml in {root}", err=True)
        raise click.Abort()
    try:
        zip_bytes = _zip_bundle_root(root)
        if len(zip_bytes) > UPLOAD_MAX_BYTES:
            click.echo(
                f"❌ Bundle zip exceeds 100MB ({len(zip_bytes) // (1024 * 1024)} MB)",
                err=True,
            )
            raise click.Abort()
        url = f"{api_url.rstrip('/')}/api/v1/deploy/upload"
        headers = get_api_headers()
        files = {"bundle": ("bundle.zip", zip_bytes, "application/zip")}
        response = api_request("POST", url, headers=headers, files=files, timeout=120)
        result = response.json()
        click.echo("✅ Deploy job accepted (202)")
        click.echo(f"   deploy_job_id: {result.get('deploy_job_id', '')}")
        click.echo(f"   bundle_id: {result.get('bundle_id', '')}")
        click.echo(f"   status_url: {result.get('status_url', '')}")
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"❌ Could not connect to API at {api_url}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise click.Abort()


@deploy_group.command("list")
@click.option("--motet-id", help="Filter by motet_id")
@click.option("--tenant-id", help="Filter by tenant_id")
@click.option("--worker-id", help="Filter by worker_id in targeting")
@click.option("--verbose", "-v", is_flag=True, help="Show full catalog (commands, tools) and per-worker state")
@api_url_option()
def list_cmd(
    motet_id: Optional[str],
    tenant_id: Optional[str],
    worker_id: Optional[str],
    verbose: bool,
    api_url: str,
) -> None:
    """GET /api/v1/deploy — list deployed bundles with catalog and worker state summary."""
    try:
        url = f"{api_url.rstrip('/')}/api/v1/deploy"
        params = {}
        if motet_id:
            params["motet_id"] = motet_id
        if tenant_id:
            params["tenant_id"] = tenant_id
        if worker_id:
            params["worker_id"] = worker_id
        headers = get_api_headers()
        response = api_request("GET", url, headers=headers, params=params, timeout=30)
        data = response.json()
        bundles = data.get("bundles", [])
        if not bundles:
            click.echo("No deployed bundles.")
            return
        click.echo(f"Bundles ({len(bundles)}):\n")
        for b in bundles:
            bid = b.get("bundle_id", "N/A")
            ver = b.get("bundle_version", "N/A")[:12] if b.get("bundle_version") else "N/A"
            status = b.get("status", "N/A")
            ref = b.get("bundle_ref", "")[:12] if b.get("bundle_ref") else ""
            catalog = b.get("catalog") or {}
            commands = catalog.get("commands", [])
            tools = catalog.get("tools", [])
            workflows = catalog.get("workflows", [])
            worker_state = b.get("worker_state") or {}
            workers_loaded = list(worker_state.keys())
            click.echo(f"  • {bid}")
            click.echo(f"    version={ver}  status={status}" + (f"  ref={ref}" if ref else ""))
            click.echo(f"    catalog: {len(commands)} commands, {len(tools)} tools, {len(workflows)} workflows")
            if workers_loaded:
                click.echo(f"    loaded on {len(workers_loaded)} worker(s): {', '.join(workers_loaded)}")
            else:
                click.echo(f"    loaded on 0 workers")
            if verbose:
                if commands:
                    click.echo(f"    commands: {', '.join(commands)}")
                if tools:
                    click.echo(f"    tools: {', '.join(tools)}")
                if workflows:
                    click.echo(f"    workflows: {', '.join(workflows)}")
                for wid, wst in worker_state.items():
                    w_commands = wst.get("commands", [])
                    w_tools = wst.get("tools", [])
                    loaded_at = wst.get("loaded_at", "N/A")
                    click.echo(f"    worker {wid}: {len(w_commands)} commands, {len(w_tools)} tools  (loaded_at={loaded_at})")
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"❌ Could not connect to API at {api_url}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise click.Abort()


@deploy_group.command("status")
@click.argument("bundle_id")
@click.option("--job-id", required=True, help="Deploy job ID (command_id) to poll")
@api_url_option()
def status_cmd(bundle_id: str, job_id: str, api_url: str) -> None:
    """GET /api/v1/deploy/{bundle_id}/status?job_id= — poll deploy job status."""
    try:
        url = f"{api_url.rstrip('/')}/api/v1/deploy/{bundle_id}/status"
        headers = get_api_headers()
        response = api_request("GET", url, headers=headers, params={"job_id": job_id}, timeout=30)
        result = response.json()
        click.echo(f"status: {result.get('status', 'N/A')}")
        click.echo(f"bundle_id: {result.get('bundle_id', '')}")
        click.echo(f"bundle_version: {result.get('bundle_version', '')}")
        for key in ("acked_workers", "failed_workers", "skipped_workers"):
            val = result.get(key, [])
            if val:
                click.echo(f"{key}: {val}")
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"❌ Could not connect to API at {api_url}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise click.Abort()


@deploy_group.command("validate")
@click.option("--repo-url", required=True, help="Git repository URL")
@click.option("--branch", required=True, help="Branch, tag, or commit SHA")
@click.option("--path", required=True, help="Path within repo (bundle root)")
@api_url_option()
def validate_cmd(repo_url: str, branch: str, path: str, api_url: str) -> None:
    """POST /api/v1/deploy/validate — validate bundle (lint only, SSE stream)."""
    try:
        url = f"{api_url.rstrip('/')}/api/v1/deploy/validate"
        payload = {"repo_url": repo_url, "branch": branch, "path": path}
        headers = get_api_headers()
        response = api_request("POST", url, headers=headers, json=payload, timeout=120, stream=True)
        click.echo("Lint stream (SSE):")
        for line in response.iter_lines(decode_unicode=True):
            if line and line.startswith("data:"):
                click.echo(f"  {line.strip()}")
        click.echo("✅ Validate stream finished")
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"❌ Could not connect to API at {api_url}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise click.Abort()


@deploy_group.command("propagate")
@click.argument("bundle_id")
@api_url_option()
def propagate_cmd(bundle_id: str, api_url: str) -> None:
    """POST /api/v1/deploy/{bundle_id}/propagate — retry reload on failed/skipped workers."""
    try:
        url = f"{api_url.rstrip('/')}/api/v1/deploy/{bundle_id}/propagate"
        headers = get_api_headers()
        response = api_request("POST", url, headers=headers, json={}, timeout=60)
        result = response.json()
        click.echo("✅ Propagate job accepted (202)")
        click.echo(f"   deploy_job_id: {result.get('deploy_job_id', '')}")
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"❌ Could not connect to API at {api_url}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise click.Abort()


@deploy_group.command("rollback")
@click.argument("bundle_id")
@click.option("--version", "bundle_version", required=True, help="Bundle version (git tree SHA) to restore")
@click.confirmation_option(prompt="Rollback this bundle to the specified version?")
@api_url_option()
def rollback_cmd(bundle_id: str, bundle_version: str, api_url: str) -> None:
    """POST /api/v1/deploy/{bundle_id}/rollback — rollback to a prior version."""
    try:
        url = f"{api_url.rstrip('/')}/api/v1/deploy/{bundle_id}/rollback"
        payload = {"bundle_version": bundle_version}
        headers = get_api_headers()
        response = api_request("POST", url, headers=headers, json=payload, timeout=60)
        result = response.json()
        click.echo("✅ Rollback job accepted (202)")
        click.echo(f"   bundle_id: {result.get('bundle_id')}")
        click.echo(f"   bundle_version: {result.get('bundle_version')}")
        click.echo(f"   deploy_job_id: {result.get('deploy_job_id')}")
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"❌ Could not connect to API at {api_url}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise click.Abort()


@deploy_group.command("undeploy")
@click.argument("bundle_id")
@click.confirmation_option(prompt="Undeploy this bundle from all workers?")
@api_url_option()
def undeploy_cmd(bundle_id: str, api_url: str) -> None:
    """DELETE /api/v1/deploy/{bundle_id} — undeploy a bundle."""
    try:
        url = f"{api_url.rstrip('/')}/api/v1/deploy/{bundle_id}"
        headers = get_api_headers()
        response = api_request("DELETE", url, headers=headers, timeout=60)
        result = response.json()
        click.echo("✅ Undeploy job accepted (202)")
        click.echo(f"   deploy_job_id: {result.get('deploy_job_id')}")
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"❌ Could not connect to API at {api_url}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise click.Abort()


@deploy_group.command("history")
@click.argument("bundle_id")
@api_url_option()
def history_cmd(bundle_id: str, api_url: str) -> None:
    """GET /api/v1/deploy/{bundle_id}/history — deploy history for a bundle."""
    try:
        url = f"{api_url.rstrip('/')}/api/v1/deploy/{bundle_id}/history"
        headers = get_api_headers()
        response = api_request("GET", url, headers=headers, timeout=30)
        result = response.json()
        history = result.get("history", [])
        click.echo(f"Deploy history for '{bundle_id}' ({len(history)} entries):\n")
        for entry in history:
            click.echo(f"  • {entry}")
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"❌ Could not connect to API at {api_url}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise click.Abort()
