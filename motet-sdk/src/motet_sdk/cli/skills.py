"""
Motet - Skills CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-29

Description:
    CLI commands for listing installed Agent Skills via /api/v1/skills.

Dependencies:
    - click: CLI framework
    - requests: API communication
    - motet_sdk.cli._api: api_request, api_url_option
    - motet_sdk.cli._auth: get_api_headers

Usage:
    motet-cli skills list
    motet-cli skills list --bundle-id skills-vendor-demo --json-output

Notes:
    - This command reports installed bundle-backed skills from API catalog data.
      Runtime activation still happens through model turns and core.activate_skill.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import click
import requests

from ._api import api_request, api_url_option, normalize_base_url
from ._auth import get_api_headers


@click.group("skills")
def skills_group() -> None:
    """Inspect installed Agent Skills."""
    pass


@skills_group.command("list")
@click.option("--bundle-id", default=None, help="Optional bundle_id filter")
@click.option("--tenant-id", default=None, help="Optional tenant_id visibility filter")
@click.option("--motet-id", default=None, help="Optional motet_id visibility filter")
@click.option("--json-output", "json_output", is_flag=True, help="Output raw JSON")
@api_url_option()
def list_skills(
    bundle_id: Optional[str],
    tenant_id: Optional[str],
    motet_id: Optional[str],
    json_output: bool,
    api_url: str,
) -> None:
    """List installed Agent Skills (GET /api/v1/skills)."""
    try:
        base = normalize_base_url(api_url)
        params = {}
        if bundle_id:
            params["bundle_id"] = bundle_id
        if tenant_id:
            params["tenant_id"] = tenant_id
        if motet_id:
            params["motet_id"] = motet_id
        response = api_request(
            "GET",
            f"{base}/api/v1/skills",
            headers=get_api_headers(),
            params=params or None,
            timeout=30,
        )
        data: dict[str, Any] = response.json()
        if json_output:
            click.echo(json.dumps(data, indent=2))
            return

        skills = data.get("skills", [])
        if not skills:
            click.echo("No skills found")
            return

        click.echo(f"Found {len(skills)} skill(s):\n")
        for skill in skills:
            skill_id = skill.get("skill_id") or "N/A"
            description = (skill.get("description") or "").strip()
            bundle = skill.get("bundle_id") or "N/A"
            capabilities = skill.get("runtime_capabilities") or []
            stack = skill.get("base_image_stack") or "platform default"

            click.echo(f"  - {skill_id}")
            click.echo(f"    Bundle: {bundle}")
            click.echo(f"    Runtime: {stack}")
            if capabilities:
                click.echo(f"    Capabilities: {', '.join(capabilities)}")
            if description:
                click.echo(f"    Description: {description}")
            click.echo()
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"\nCould not connect to API at {api_url}", err=True)
        click.echo("Make sure the API server is running", err=True)
        raise click.Abort()
    except Exception as exc:
        click.echo(f"Error listing skills: {exc}", err=True)
        raise click.Abort()


__all__ = ["skills_group"]
