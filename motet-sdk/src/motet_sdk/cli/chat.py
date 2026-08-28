"""
Motet - Chat CLI

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-06

Description:
    CLI commands for chatting with the AI agent. Persists conversation_id in
    ~/.motet/config.json so follow-up messages keep context. Optional flags
    mirror ChatRequest fields from POST /api/v1/chat (model flags map to
    overrides: model_provider, model_name, model_profile_name; artifact flags
    map to top-level RAG fields).

Dependencies:
    - click: CLI framework
    - requests: API communication

Usage:
    motet-cli chat --message "Hello"       # Chat with AI agent (keeps context)
    motet-cli chat --message "Hi" --stream # Stream response
    motet-cli chat --message "New topic" --new  # Start a new conversation
    motet-cli chat --message "Summarize" --artifact-id id1 --artifact-id id2
    motet-cli chat --message "Search docs" --artifact-rag-scope conversation
    motet-cli chat --message "Hello" --provider openai --model-name gpt-4o
    motet-cli chat --message "Hi" --model-profile my-profile

Notes:
    - Aligns with API structure (api/v1/chat.py)
    - Uses conversation_id from config for session continuity
"""

import uuid

import click

from ._api import api_request, api_url_option
from ._auth import get_api_headers
from ._config import get_chat_conversation_id, set_chat_conversation_id
from ._logging import logger


@click.command("chat")
@click.option("--message", "message_text", required=True, help="User message to send to the agent")
@click.option("--stream", is_flag=True, default=False, help="Stream response tokens")
@click.option("--new", "new_conversation", is_flag=True, help="Start a new conversation (ignore saved session)")
@click.option(
    "--provider",
    type=click.Choice(["mock", "openai", "anthropic"]),
    default=None,
    help="Model provider for this request (sent as overrides.model_provider).",
)
@click.option(
    "--model-name",
    default=None,
    help="Concrete model id for this request (overrides.model_name).",
)
@click.option(
    "--model-profile",
    default=None,
    help="Model profile name for routing (overrides.model_profile_name).",
)
@click.option(
    "--artifact-rag-scope",
    type=click.Choice(["conversation", "principal", "motet"]),
    default=None,
    help="Artifact RAG scope (broader scopes may require server authorization).",
)
@click.option(
    "--artifact-id",
    "artifact_ids",
    multiple=True,
    help="Artifact ID to restrict RAG to (repeat for multiple).",
)
@click.option(
    "--artifact-tag",
    "artifact_tags",
    multiple=True,
    help="Tag to narrow artifact RAG within scope (repeat for multiple).",
)
@click.option(
    "--artifact-collection-id",
    default=None,
    help="Optional artifact collection id for collection-scoped RAG.",
)
@click.option(
    "--allow-broader-artifact-rag-scope",
    is_flag=True,
    default=False,
    help="Allow principal/motet RAG scope when the server permits this request.",
)
@api_url_option()
def chat_command(
    message_text: str,
    stream: bool,
    new_conversation: bool,
    provider: str | None,
    model_name: str | None,
    model_profile: str | None,
    artifact_rag_scope: str | None,
    artifact_ids: tuple[str, ...],
    artifact_tags: tuple[str, ...],
    artifact_collection_id: str | None,
    allow_broader_artifact_rag_scope: bool,
    api_url: str,
) -> None:
    """Chat with the AI agent via API. Uses saved conversation ID to keep context between runs."""
    import requests

    try:
        url = f"{api_url.rstrip('/')}/api/v1/chat"
        conversation_id = None if new_conversation else get_chat_conversation_id()
        if not conversation_id:
            conversation_id = str(uuid.uuid4())
            set_chat_conversation_id(conversation_id)
        payload = {
            "messages": [{"role": "user", "content": message_text}],
            "stream": stream,
            "conversation_id": conversation_id,
            "surface_id": "cli",
        }
        overrides: dict[str, str] = {}
        if provider:
            overrides["model_provider"] = provider
        if model_name:
            overrides["model_name"] = model_name
        if model_profile:
            overrides["model_profile_name"] = model_profile
        if overrides:
            payload["overrides"] = overrides
        if artifact_rag_scope:
            payload["artifact_rag_scope"] = artifact_rag_scope
        if artifact_ids:
            payload["artifact_ids"] = list(artifact_ids)
        if artifact_tags:
            payload["artifact_tags"] = list(artifact_tags)
        if artifact_collection_id:
            payload["artifact_collection_id"] = artifact_collection_id
        if allow_broader_artifact_rag_scope:
            payload["allow_broader_artifact_rag_scope"] = True

        headers = get_api_headers()
        timeout = 300 if stream else 60
        response = api_request(
            "POST", url, headers=headers, json=payload, stream=stream, timeout=timeout
        )
        if stream:
            for line in response.iter_lines():
                if line:
                    try:
                        decoded = line.decode("utf-8")
                        if decoded.startswith("data: "):
                            token = decoded[6:]
                            if token != "[DONE]":
                                click.echo(token, nl=False)
                    except Exception:
                        pass
            click.echo()
        else:
            result = response.json()
            content = result.get("content") or result.get("response", {}).get("content", "")
            click.echo(content)
                
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"\n❌ Could not connect to API at {api_url}", err=True)
        click.echo(f"💡 Make sure the API server is running", err=True)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
