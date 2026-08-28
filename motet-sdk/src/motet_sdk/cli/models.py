"""
Motet - Models CLI

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2025-11-20

Description:
    CLI commands for listing available AI models and their capabilities.

Dependencies:
    - click: CLI framework
    - requests: API communication

Usage:
    motet-cli models                       # List all models
    motet-cli models --provider openai     # Filter by provider

Notes:
    - Aligns with API structure (api/v1/models.py)
    - Uses API for consistency
"""

import click
import requests

from ._api import api_request, api_url_option
from ._auth import get_api_headers
from ._logging import logger


@click.command("models")
@click.option("--provider", default=None, help="Filter by provider")
@api_url_option()
def models_command(provider: str | None, api_url: str) -> None:
    """List available models and capabilities via API."""
    try:
        params = {"provider": provider} if provider else {}
        url = f"{api_url.rstrip('/')}/api/v1/models"
        headers = get_api_headers()
        response = api_request("GET", url, headers=headers, params=params, timeout=30)
        result = response.json()
        items = result.get("models", [])
        for spec in items:
            caps = ",".join(sorted(spec.get("capabilities", [])))
            provider_name = spec.get("provider", "unknown")
            model_name = spec.get("name", "unknown")
            max_out = spec.get("max_output_tokens", "N/A")
            click.echo(f"{provider_name}:{model_name}\t{caps}\tmax_out={max_out}")
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"\n❌ Could not connect to API at {api_url}", err=True)
        click.echo("💡 Make sure the API server is running", err=True)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
