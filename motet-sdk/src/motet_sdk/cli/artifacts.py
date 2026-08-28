"""
Motet - Artifacts CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-06

Description:
    CLI commands for managing artifacts (uploads, downloads, listing) and
    preparation-aware indexing operations exposed by the Artifacts HTTP API.
    Supports user uploads, tool artifacts, metadata/tag updates, bulk delete,
    indexing status, reindex, and durable per-artifact indexing policy.

Dependencies:
    - click: CLI framework
    - requests: API communication

Usage:
    motet-cli artifacts ls --source-artifact-id <video-id> --kind derived_video_poster
    motet-cli artifacts put ./file.txt
    motet-cli artifacts get <id> --out ./file.txt
    motet-cli artifacts metadata <id> --set key=value --tag jersey
    motet-cli artifacts rm-all                 # prompts; or pass --yes
    motet-cli artifacts indexing-status <id> [<id> ...]
    motet-cli artifacts reindex <id>
    motet-cli artifacts reindex-task <task_id>
    motet-cli artifacts strategies
    motet-cli artifacts plan ./file.json
    motet-cli artifacts indexing-policy <id> --disabled
"""

import json
import os
from datetime import datetime

import click
import requests

from ._api import api_request, api_url_option
from ._auth import get_api_headers
from ._logging import logger


@click.group("artifacts")
def artifacts_group() -> None:
    """Artifact storage management."""
    pass


def _scope_params(tenant_id: str | None = None, motet_id: str | None = None) -> dict[str, str]:
    """Build optional admin scope override params for artifact management endpoints."""

    params: dict[str, str] = {}
    if tenant_id:
        params["tenant_id"] = tenant_id
    if motet_id:
        params["motet_id"] = motet_id
    return params


@artifacts_group.command("ls")
@click.option("--kind", help="Filter by artifact kind (user_upload, tool_artifact, etc)")
@click.option("--conversation-id", help="Filter by conversation ID")
@click.option(
    "--source-artifact-id",
    help="Filter to derived artifacts for a given source artifact ID",
)
@click.option("--limit", type=int, default=20, help="Max items to return")
@click.option("--offset", type=int, default=0, help="Pagination offset")
@click.option("--tenant-id", default=None, help="Admin-only tenant scope override")
@click.option("--motet-id", default=None, help="Admin-only motet/environment scope override")
@api_url_option()
def list_artifacts(kind, conversation_id, source_artifact_id, limit, offset, tenant_id, motet_id, api_url):
    """List artifacts (metadata only)."""
    headers = get_api_headers()
    params = {"limit": limit, "offset": offset}
    if kind:
        params["kind"] = kind
    if conversation_id:
        params["conversation_id"] = conversation_id
    if source_artifact_id:
        params["source_artifact_id"] = source_artifact_id
    params.update(_scope_params(tenant_id=tenant_id, motet_id=motet_id))
    try:
        resp = api_request("GET", f"{api_url.rstrip('/')}/api/v1/artifacts", headers=headers, params=params)
        data = resp.json()
        items = data.get("items", [])
        if not items:
            click.echo("No artifacts found.")
            return
        click.echo(f"{'ID':<38} {'Kind':<15} {'Type':<20} {'Size':<10} {'Created'}")
        click.echo("-" * 100)
        for item in items:
            dt = datetime.fromtimestamp(item.get("created_at", 0)).strftime("%Y-%m-%d %H:%M:%S")
            click.echo(f"{item['id']:<38} {item.get('kind', 'unknown'):<15} {item['content_type'][:20]:<20} {item['bytes']:<10} {dt}")
    except click.ClickException:
        raise
    except requests.exceptions.RequestException as e:
        click.echo(f"Error listing artifacts: {str(e)}", err=True)
        if hasattr(e, "response") and e.response:
            click.echo(f"Details: {e.response.text}", err=True)


@artifacts_group.command("put")
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--kind", default="user_upload", help="Artifact kind (default: user_upload)")
@click.option("--prep-strategy", default=None, help="Preparation strategy override")
@click.option("--prep-hint", multiple=True, help="Preparation hint as key=value (repeatable)")
@api_url_option()
def upload_artifact(file_path, kind, prep_strategy, prep_hint, api_url):
    """Upload a file as an artifact."""
    headers = get_api_headers()
    if "Content-Type" in headers:
        del headers["Content-Type"]
    try:
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(file_path), f)}
            params = {"kind": kind}
            hints: dict[str, object] = {}
            if prep_strategy:
                hints["prep_strategy_id"] = prep_strategy
            extra: dict[str, str] = {}
            for item in prep_hint:
                if "=" not in item:
                    raise click.UsageError("--prep-hint values must be key=value")
                key, value = item.split("=", 1)
                extra[key] = value
            if extra:
                hints["extra"] = extra
            data = {"prep_hints": json.dumps(hints)} if hints else None
            resp = api_request(
                "POST",
                f"{api_url.rstrip('/')}/api/v1/artifacts",
                headers=headers,
                files=files,
                params=params,
                data=data,
            )
            result = resp.json()
            click.echo("Artifact uploaded successfully:")
            click.echo(json.dumps(result, indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.RequestException as e:
        click.echo(f"Error uploading artifact: {str(e)}", err=True)
        if hasattr(e, "response") and e.response:
            click.echo(f"Details: {e.response.text}", err=True)


@artifacts_group.command("get")
@click.argument("artifact_id")
@click.option("--out", type=click.Path(writable=True), help="Output file path (default: use artifact filename)")
@click.option("--tenant-id", default=None, help="Admin-only tenant scope override")
@click.option("--motet-id", default=None, help="Admin-only motet/environment scope override")
@api_url_option()
def download_artifact(artifact_id, out, tenant_id, motet_id, api_url):
    """Download artifact payload."""
    headers = get_api_headers()
    base = api_url.rstrip("/")
    params = _scope_params(tenant_id=tenant_id, motet_id=motet_id)
    try:
        if not out:
            meta_resp = api_request(
                "GET",
                f"{base}/api/v1/artifacts/{artifact_id}/metadata",
                headers=headers,
                params=params,
            )
            meta = meta_resp.json()
            out = meta.get("metadata", {}).get("filename") or f"{artifact_id}.dat"
        r = api_request(
            "GET",
            f"{base}/api/v1/artifacts/{artifact_id}/download",
            headers=headers,
            params=params,
            stream=True,
        )
        with open(out, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        click.echo(f"Downloaded artifact {artifact_id} to {out}")
    except click.ClickException:
        raise
    except requests.exceptions.RequestException as e:
        click.echo(f"Error downloading artifact: {str(e)}", err=True)
        if hasattr(e, "response") and e.response:
            click.echo(f"Details: {e.response.text}", err=True)


@artifacts_group.command("rm")
@click.argument("artifact_id")
@click.option("--tenant-id", default=None, help="Admin-only tenant scope override")
@click.option("--motet-id", default=None, help="Admin-only motet/environment scope override")
@api_url_option()
def delete_artifact(artifact_id, tenant_id, motet_id, api_url):
    """Delete an artifact."""
    headers = get_api_headers()
    try:
        api_request(
            "DELETE",
            f"{api_url.rstrip('/')}/api/v1/artifacts/{artifact_id}",
            headers=headers,
            params=_scope_params(tenant_id=tenant_id, motet_id=motet_id),
        )
        click.echo(f"Artifact {artifact_id} deleted.")
    except click.ClickException:
        raise
    except requests.exceptions.RequestException as e:
        click.echo(f"Error deleting artifact: {str(e)}", err=True)
        if hasattr(e, "response") and e.response:
            click.echo(f"Details: {e.response.text}", err=True)


@artifacts_group.command("info")
@click.argument("artifact_id")
@click.option("--tenant-id", default=None, help="Admin-only tenant scope override")
@click.option("--motet-id", default=None, help="Admin-only motet/environment scope override")
@api_url_option()
def get_metadata(artifact_id, tenant_id, motet_id, api_url):
    """Get artifact metadata."""
    headers = get_api_headers()
    try:
        resp = api_request(
            "GET",
            f"{api_url.rstrip('/')}/api/v1/artifacts/{artifact_id}/metadata",
            headers=headers,
            params=_scope_params(tenant_id=tenant_id, motet_id=motet_id),
        )
        click.echo(json.dumps(resp.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.RequestException as e:
        click.echo(f"Error fetching metadata: {str(e)}", err=True)
        if hasattr(e, "response") and e.response:
            click.echo(f"Details: {e.response.text}", err=True)


@artifacts_group.command("metadata")
@click.argument("artifact_id")
@click.option(
    "--set",
    "set_fields",
    multiple=True,
    help="Metadata field as key=value (repeatable; values are stored as strings)",
)
@click.option(
    "--set-json",
    "set_json",
    default=None,
    help='JSON object of metadata fields to merge (e.g. \'{"source":"memo"}\')',
)
@click.option("--tag", "artifact_tags", multiple=True, help="Artifact tag to set/merge (repeatable)")
@click.option(
    "--replace-tags/--merge-tags",
    default=False,
    help="With --tag: replace existing artifact_tags instead of merging (default: merge)",
)
@click.option("--tenant-id", default=None, help="Admin-only tenant scope override")
@click.option("--motet-id", default=None, help="Admin-only motet/environment scope override")
@api_url_option()
def patch_metadata(
    artifact_id,
    set_fields,
    set_json,
    artifact_tags,
    replace_tags,
    tenant_id,
    motet_id,
    api_url,
):
    """Merge artifact metadata and tags (PATCH /api/v1/artifacts/{id}/metadata)."""
    metadata: dict = {}
    if set_json:
        try:
            parsed = json.loads(set_json)
        except json.JSONDecodeError as e:
            raise click.UsageError(f"--set-json must be valid JSON: {e}") from e
        if not isinstance(parsed, dict):
            raise click.UsageError("--set-json must be a JSON object")
        metadata.update(parsed)
    for item in set_fields:
        if "=" not in item:
            raise click.UsageError("--set values must be key=value")
        key, value = item.split("=", 1)
        if not key:
            raise click.UsageError("--set key cannot be empty")
        metadata[key] = value
    if not metadata and not artifact_tags:
        raise click.UsageError("Provide at least one of --set, --set-json, or --tag")

    body: dict = {"metadata": metadata}
    if artifact_tags:
        body["artifact_tags"] = list(artifact_tags)
        body["merge_artifact_tags"] = not replace_tags

    headers = get_api_headers()
    headers.setdefault("Content-Type", "application/json")
    try:
        resp = api_request(
            "PATCH",
            f"{api_url.rstrip('/')}/api/v1/artifacts/{artifact_id}/metadata",
            headers=headers,
            params=_scope_params(tenant_id=tenant_id, motet_id=motet_id),
            json=body,
        )
        click.echo(json.dumps(resp.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.RequestException as e:
        click.echo(f"Error updating metadata: {str(e)}", err=True)
        if hasattr(e, "response") and e.response:
            click.echo(f"Details: {e.response.text}", err=True)


@artifacts_group.command("rm-all")
@click.confirmation_option(prompt="Delete ALL artifacts in the resolved scope?")
@click.option("--tenant-id", default=None, help="Admin-only tenant scope override")
@click.option("--motet-id", default=None, help="Admin-only motet/environment scope override")
@api_url_option()
def delete_all_artifacts(tenant_id, motet_id, api_url):
    """Delete all artifacts in the resolved scope (DELETE /api/v1/artifacts)."""
    headers = get_api_headers()
    try:
        resp = api_request(
            "DELETE",
            f"{api_url.rstrip('/')}/api/v1/artifacts",
            headers=headers,
            params=_scope_params(tenant_id=tenant_id, motet_id=motet_id),
            timeout=120,
        )
        click.echo(json.dumps(resp.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.RequestException as e:
        click.echo(f"Error bulk-deleting artifacts: {str(e)}", err=True)
        if hasattr(e, "response") and e.response:
            click.echo(f"Details: {e.response.text}", err=True)


@artifacts_group.command("indexing-status")
@click.argument("artifact_ids", nargs=-1, required=True)
@click.option("--tenant-id", default=None, help="Admin-only tenant scope override")
@click.option("--motet-id", default=None, help="Admin-only motet/environment scope override")
@api_url_option()
def indexing_status(artifact_ids, tenant_id, motet_id, api_url):
    """Get derived-text chunk indexing status for one or more artifacts."""
    headers = get_api_headers()
    params: list[tuple[str, str]] = [("artifact_id", artifact_id) for artifact_id in artifact_ids]
    scoped = _scope_params(tenant_id=tenant_id, motet_id=motet_id)
    params.extend(scoped.items())
    try:
        resp = api_request(
            "GET",
            f"{api_url.rstrip('/')}/api/v1/artifacts/indexing-status",
            headers=headers,
            params=params,
        )
        click.echo(json.dumps(resp.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.RequestException as e:
        click.echo(f"Error fetching indexing status: {str(e)}", err=True)
        if hasattr(e, "response") and e.response:
            click.echo(f"Details: {e.response.text}", err=True)


@artifacts_group.command("reindex")
@click.argument("artifact_id")
@click.option("--wait", is_flag=True, default=False, help="Block until the reindex command completes")
@click.option("--strategy", "strategy_id", default=None, help="Preparation strategy override")
@click.option("--tenant-id", default=None, help="Admin-only tenant scope override")
@click.option("--motet-id", default=None, help="Admin-only motet/environment scope override")
@api_url_option()
def reindex_artifact(artifact_id, wait, strategy_id, tenant_id, motet_id, api_url):
    """Queue preparation/indexing for an artifact."""
    headers = get_api_headers()
    params = _scope_params(tenant_id=tenant_id, motet_id=motet_id)
    if wait:
        params["wait"] = "true"
    if strategy_id:
        params["strategy_id"] = strategy_id
    try:
        resp = api_request(
            "POST",
            f"{api_url.rstrip('/')}/api/v1/artifacts/{artifact_id}/reindex",
            headers=headers,
            params=params,
            timeout=300 if wait else 30,
        )
        click.echo(json.dumps(resp.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.RequestException as e:
        click.echo(f"Error reindexing artifact: {str(e)}", err=True)
        if hasattr(e, "response") and e.response:
            click.echo(f"Details: {e.response.text}", err=True)


@artifacts_group.command("reindex-task")
@click.argument("task_id")
@api_url_option()
def reindex_task(task_id, api_url):
    """Get reindex task status."""
    headers = get_api_headers()
    try:
        resp = api_request(
            "GET",
            f"{api_url.rstrip('/')}/api/v1/artifacts/reindex-tasks/{task_id}",
            headers=headers,
        )
        click.echo(json.dumps(resp.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.RequestException as e:
        click.echo(f"Error fetching reindex task: {str(e)}", err=True)
        if hasattr(e, "response") and e.response:
            click.echo(f"Details: {e.response.text}", err=True)


@artifacts_group.command("indexing-policy")
@click.argument("artifact_id")
@click.option("--enabled/--disabled", "enabled", default=None, help="Enable or disable artifact text indexing")
@click.option("--disable-strategy", multiple=True, help="Preparation strategy ID to disable (repeatable)")
@click.option("--tenant-id", default=None, help="Admin-only tenant scope override")
@click.option("--motet-id", default=None, help="Admin-only motet/environment scope override")
@api_url_option()
def indexing_policy(artifact_id, enabled, disable_strategy, tenant_id, motet_id, api_url):
    """Update durable indexing eligibility for an artifact."""
    if enabled is None:
        raise click.UsageError("Pass either --enabled or --disabled")

    headers = get_api_headers()
    headers.setdefault("Content-Type", "application/json")
    try:
        resp = api_request(
            "PATCH",
            f"{api_url.rstrip('/')}/api/v1/artifacts/{artifact_id}/indexing-policy",
            headers=headers,
            params=_scope_params(tenant_id=tenant_id, motet_id=motet_id),
            json={
                "indexing_enabled": bool(enabled),
                **({"disable_strategies": list(disable_strategy)} if disable_strategy else {}),
            },
        )
        click.echo(json.dumps(resp.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.RequestException as e:
        click.echo(f"Error updating indexing policy: {str(e)}", err=True)
        if hasattr(e, "response") and e.response:
            click.echo(f"Details: {e.response.text}", err=True)


@artifacts_group.command("strategies")
@api_url_option()
def preparation_strategies(api_url):
    """List registered artifact preparation strategies."""
    headers = get_api_headers()
    try:
        resp = api_request(
            "GET",
            f"{api_url.rstrip('/')}/api/v1/artifacts/preparation/strategies",
            headers=headers,
        )
        click.echo(json.dumps(resp.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.RequestException as e:
        click.echo(f"Error fetching preparation strategies: {str(e)}", err=True)
        if hasattr(e, "response") and e.response:
            click.echo(f"Details: {e.response.text}", err=True)


@artifacts_group.command("plan")
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--content-type", default=None, help="Override detected content type")
@click.option("--kind", default="user_upload", help="Artifact kind for planning")
@click.option("--prep-strategy", default=None, help="Preparation strategy override")
@api_url_option()
def preparation_plan(file_path, content_type, kind, prep_strategy, api_url):
    """Dry-run preparation strategy selection for a local file."""
    import mimetypes

    headers = get_api_headers()
    headers.setdefault("Content-Type", "application/json")
    detected_type = content_type or mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    prep_hints = {"prep_strategy_id": prep_strategy} if prep_strategy else {}
    body = {
        "content_type": detected_type,
        "extension": os.path.splitext(file_path)[1],
        "filename": os.path.basename(file_path),
        "kind": kind,
        "bytes": os.path.getsize(file_path),
        "prep_hints": prep_hints,
    }
    try:
        resp = api_request(
            "POST",
            f"{api_url.rstrip('/')}/api/v1/artifacts/preparation/plan",
            headers=headers,
            json=body,
        )
        click.echo(json.dumps(resp.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.RequestException as e:
        click.echo(f"Error planning artifact preparation: {str(e)}", err=True)
        if hasattr(e, "response") and e.response:
            click.echo(f"Details: {e.response.text}", err=True)


