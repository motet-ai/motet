"""
Motet - Memories CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    CLI commands for memory list/find/tag/forget/clear, inspection,
    consolidation, retrieval, and storing content via the Memories HTTP API.

Dependencies:
    - click: CLI framework
    - requests: API communication

Usage:
    motet-cli memories list --limit 10
    motet-cli memories find --tags important,reviewed
    motet-cli memories tag --memory-id mem-1 --tags important
    motet-cli memories forget --memory-id mem-1
    motet-cli memories clear --tag type:note   # prompts; or pass --yes
    motet-cli memories vector-list --tag conversation
    motet-cli memories inspect --limit 10
    motet-cli memories consolidate
    motet-cli memories retrieve --q "search"
    motet-cli memories store --content "Note"
    motet-cli memories store-dir ./docs

Notes:
    - Aligns with API structure (api/v1/memories.py)
    - Destructive clear prompts for confirmation (or --yes)
"""

from __future__ import annotations

import json
import os
from typing import Optional

import click
import requests

from ._api import api_request, api_url_option
from ._auth import get_api_headers


@click.group("memories")
def memories_group() -> None:
    """Memory operations."""
    pass


def _split_tags(tags: str) -> list[str]:
    return [t.strip() for t in (tags or "").split(",") if t.strip()]


def _echo_json_response(response: requests.Response) -> None:
    click.echo(json.dumps(response.json(), indent=2))


def _handle_api_errors(api_url: str, err: Exception) -> None:
    if isinstance(err, click.ClickException):
        raise err
    if isinstance(err, requests.exceptions.ConnectionError):
        click.echo(f"\n❌ Could not connect to API at {api_url}", err=True)
        click.echo("💡 Make sure the API server is running", err=True)
        return
    click.echo(f"❌ Error: {err}", err=True)


@memories_group.command("list")
@click.option("--limit", type=int, default=10, help="Maximum number of memories to return")
@click.option("--tag", default=None, help="Filter by tag (e.g. type:conversation)")
@click.option("--entity", default=None, help="Filter by entity (shorthand for entity: tag)")
@api_url_option()
def list_command(limit: int, tag: Optional[str], entity: Optional[str], api_url: str) -> None:
    """List recent memories (GET /api/v1/memories)."""
    try:
        params: dict[str, object] = {"limit": limit}
        if tag:
            params["tag"] = tag
        if entity:
            params["entity"] = entity
        url = f"{api_url.rstrip('/')}/api/v1/memories"
        response = api_request("GET", url, headers=get_api_headers(), params=params, timeout=30)
        _echo_json_response(response)
    except Exception as e:
        _handle_api_errors(api_url, e)


@memories_group.command("find")
@click.option("--tags", default="", help="Comma-separated tags to match")
@click.option("--match", "match_mode", type=click.Choice(["any", "all"]), default="any", help="Tag match mode")
@click.option("--limit", type=int, default=5, help="Maximum number of results")
@click.option("--conversation-id", default=None, help="Scope to a conversation")
@click.option("--types", default=None, help="Comma-separated memory types")
@click.option("--include-vector/--no-include-vector", default=False, help="Include vector embeddings")
@click.option("--scope", default=None, help="Memory scope: wm, stm, ltm, or both")
@api_url_option()
def find_command(
    tags: str,
    match_mode: str,
    limit: int,
    conversation_id: Optional[str],
    types: Optional[str],
    include_vector: bool,
    scope: Optional[str],
    api_url: str,
) -> None:
    """Find memories by tags (POST /api/v1/memories/find)."""
    try:
        body: dict[str, object] = {
            "tags": _split_tags(tags),
            "match": match_mode,
            "limit": limit,
            "include_vector": include_vector,
        }
        if conversation_id:
            body["conversation_id"] = conversation_id
        if types:
            body["types"] = _split_tags(types)
        if scope:
            body["scope"] = scope
        url = f"{api_url.rstrip('/')}/api/v1/memories/find"
        response = api_request("POST", url, headers=get_api_headers(), json=body, timeout=60)
        _echo_json_response(response)
    except Exception as e:
        _handle_api_errors(api_url, e)


@memories_group.command("tag")
@click.option("--tags", required=True, help="Comma-separated tags to add or remove")
@click.option("--op", type=click.Choice(["add", "remove"]), default="add", help="Tag operation")
@click.option("--memory-id", "memory_ids", multiple=True, help="Memory ID to tag (repeatable)")
@click.option("--conversation-id", default=None, help="Tag all memories in this conversation")
@click.option("--filter-tag", default=None, help="Tag memories that already have this tag")
@api_url_option()
def tag_command(
    tags: str,
    op: str,
    memory_ids: tuple[str, ...],
    conversation_id: Optional[str],
    filter_tag: Optional[str],
    api_url: str,
) -> None:
    """Add or remove tags on memories (POST /api/v1/memories/tag)."""
    tag_list = _split_tags(tags)
    if not tag_list:
        raise click.UsageError("--tags must include at least one tag")
    if not memory_ids and not conversation_id and not filter_tag:
        raise click.UsageError("Provide --memory-id, --conversation-id, and/or --filter-tag")
    try:
        body: dict[str, object] = {"tags": tag_list, "op": op}
        if memory_ids:
            body["memory_ids"] = list(memory_ids)
        if conversation_id:
            body["conversation_id"] = conversation_id
        if filter_tag:
            body["filter_tag"] = filter_tag
        url = f"{api_url.rstrip('/')}/api/v1/memories/tag"
        response = api_request("POST", url, headers=get_api_headers(), json=body, timeout=60)
        _echo_json_response(response)
    except Exception as e:
        _handle_api_errors(api_url, e)


@memories_group.command("forget")
@click.option("--memory-id", "memory_ids", multiple=True, help="Memory ID to forget (repeatable)")
@click.option("--conversation-id", default=None, help="Forget memories in this conversation")
@click.option("--filter-tag", default=None, help="Forget memories that already have this tag")
@click.option("--tenant-id", default=None, help="Tenant store (admin may set another tenant)")
@click.option("--motet-id", default=None, help="Motet store (admin may set another motet)")
@api_url_option()
def forget_command(
    memory_ids: tuple[str, ...],
    conversation_id: Optional[str],
    filter_tag: Optional[str],
    tenant_id: Optional[str],
    motet_id: Optional[str],
    api_url: str,
) -> None:
    """Forget targeted memories (POST /api/v1/memories/forget)."""
    if not memory_ids and not conversation_id and not filter_tag:
        raise click.UsageError("Provide --memory-id, --conversation-id, and/or --filter-tag")
    try:
        body: dict[str, object] = {}
        if memory_ids:
            body["memory_ids"] = list(memory_ids)
        if conversation_id:
            body["conversation_id"] = conversation_id
        if filter_tag:
            body["filter_tag"] = filter_tag
        if tenant_id:
            body["tenant_id"] = tenant_id
        if motet_id:
            body["motet_id"] = motet_id
        url = f"{api_url.rstrip('/')}/api/v1/memories/forget"
        response = api_request("POST", url, headers=get_api_headers(), json=body, timeout=60)
        _echo_json_response(response)
    except Exception as e:
        _handle_api_errors(api_url, e)


@memories_group.command("clear")
@click.option("--type", "memory_type", default=None, help="Clear memories of this type only")
@click.option("--tag", default=None, help="Clear memories with this tag only")
@click.option("--clear-vector/--no-clear-vector", default=False, help="Also clear matching vector entries (requires --tag)")
@click.confirmation_option(prompt="Clear matching memories (or ALL if no type/tag filter)?")
@api_url_option()
def clear_command(
    memory_type: Optional[str],
    tag: Optional[str],
    clear_vector: bool,
    api_url: str,
) -> None:
    """Clear memories by type/tag or all (POST /api/v1/memories/clear)."""
    if clear_vector and not tag:
        raise click.UsageError("--clear-vector requires --tag")
    try:
        params: dict[str, object] = {"clear_vector": clear_vector}
        if memory_type:
            params["type"] = memory_type
        if tag:
            params["tag"] = tag
        url = f"{api_url.rstrip('/')}/api/v1/memories/clear"
        response = api_request("POST", url, headers=get_api_headers(), params=params, timeout=120)
        _echo_json_response(response)
    except Exception as e:
        _handle_api_errors(api_url, e)


@memories_group.command("vector-list")
@click.option("--limit", type=int, default=10, help="Maximum number of results")
@click.option("--tag", default=None, help="Tag to filter by")
@click.option("--collection", default=None, help="Collection name to filter by")
@click.option("--entity", default=None, help="Entity ID to filter by")
@api_url_option()
def vector_list_command(
    limit: int,
    tag: Optional[str],
    collection: Optional[str],
    entity: Optional[str],
    api_url: str,
) -> None:
    """List memories from the vector store (GET /api/v1/memories/vector/list)."""
    try:
        params: dict[str, object] = {"limit": limit}
        if tag:
            params["tag"] = tag
        if collection:
            params["collection"] = collection
        if entity:
            params["entity"] = entity
        url = f"{api_url.rstrip('/')}/api/v1/memories/vector/list"
        response = api_request("GET", url, headers=get_api_headers(), params=params, timeout=30)
        _echo_json_response(response)
    except Exception as e:
        _handle_api_errors(api_url, e)


@memories_group.command("store")
@click.option("--content", required=True, help="Text to store as a memory")
@click.option("--type", "memory_type", default="note", help="Memory type (e.g. note, summary)")
@click.option("--tags", default="", help="Comma-separated tags")
@click.option("--conversation-id", default=None, help="Conversation id for scoping")
@click.option("--scope-type", default=None, help="global, principal, conversation, or task")
@click.option(
    "--long-term/--no-long-term",
    default=None,
    help="Force long-term vector indexing (omit both for server heuristics)",
)
@api_url_option()
def store_command(
    content: str,
    memory_type: str,
    tags: str,
    conversation_id: str | None,
    scope_type: str | None,
    long_term: bool | None,
    api_url: str,
) -> None:
    """Store one memory via ``POST /api/v1/memories/store``."""
    try:
        url = f"{api_url.rstrip('/')}/api/v1/memories/store"
        body: dict = {
            "content": content,
            "type": memory_type,
            "tags": _split_tags(tags),
        }
        if conversation_id:
            body["conversation_id"] = conversation_id
        if scope_type:
            body["scope_type"] = scope_type
        if long_term is not None:
            body["long_term"] = long_term
        headers = get_api_headers()
        response = api_request("POST", url, headers=headers, json=body, timeout=120)
        click.echo(json.dumps(response.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"\n❌ Could not connect to API at {api_url}", err=True)
        click.echo("💡 Make sure the API server is running", err=True)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)


@memories_group.command("store-dir")
@click.argument("directory", type=click.Path(exists=True, file_okay=False, path_type=str))
@click.option("--type", "memory_type", default="note", help="Memory type for each file")
@click.option("--tags", default="", help="Comma-separated tags applied to every item")
@click.option("--max-bytes", type=int, default=2_000_000, help="Skip files larger than this")
@click.option("--conversation-id", default=None, help="Conversation id for scoping")
@click.option("--scope-type", default=None, help="global, principal, conversation, or task")
@click.option(
    "--long-term/--no-long-term",
    "long_term_flag",
    default=True,
    help="Default long_term=true for directory imports (semantic recall); use --no-long-term to disable",
)
@api_url_option()
def store_dir_command(
    directory: str,
    memory_type: str,
    tags: str,
    max_bytes: int,
    conversation_id: str | None,
    scope_type: str | None,
    long_term_flag: bool,
    api_url: str,
) -> None:
    """Walk a directory and store each text file as a memory via the API."""
    base_tags = _split_tags(tags)
    url = f"{api_url.rstrip('/')}/api/v1/memories/store"
    headers = get_api_headers()
    stored = 0
    skipped = 0
    try:
        for root, _, files in os.walk(directory):
            for fn in files:
                path = os.path.join(root, fn)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    skipped += 1
                    continue
                if size > max_bytes:
                    skipped += 1
                    continue
                try:
                    with open(path, encoding="utf-8", errors="ignore") as f:
                        text = f.read()
                except OSError:
                    skipped += 1
                    continue
                if not text.strip():
                    skipped += 1
                    continue
                body: dict = {
                    "content": text,
                    "type": memory_type,
                    "tags": base_tags + ["cli_store_dir"],
                    "metadata": {"source": os.path.abspath(path)},
                    "long_term": long_term_flag,
                }
                if conversation_id:
                    body["conversation_id"] = conversation_id
                if scope_type:
                    body["scope_type"] = scope_type
                try:
                    api_request("POST", url, headers=headers, json=body, timeout=120)
                    stored += 1
                except Exception:
                    skipped += 1
        click.echo(json.dumps({"stored": stored, "skipped": skipped, "directory": os.path.abspath(directory)}, indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"\n❌ Could not connect to API at {api_url}", err=True)
        click.echo("💡 Make sure the API server is running", err=True)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)


@memories_group.command("inspect")
@click.option("--limit", type=int, default=5)
@api_url_option()
def inspect_command(limit: int, api_url: str) -> None:
    """Print memory/vector inspection summary via API."""
    try:
        url = f"{api_url.rstrip('/')}/api/v1/memories/inspect"
        headers = get_api_headers()
        response = api_request("GET", url, headers=headers, params={"limit": limit}, timeout=30)
        click.echo(json.dumps(response.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"\n❌ Could not connect to API at {api_url}", err=True)
        click.echo("💡 Make sure the API server is running", err=True)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)


@memories_group.command("consolidate")
@api_url_option()
def consolidate_command(api_url: str) -> None:
    """Trigger memory consolidation via API."""
    try:
        url = f"{api_url.rstrip('/')}/api/v1/memories/consolidate"
        headers = get_api_headers()
        response = api_request("POST", url, headers=headers, timeout=60)
        result = response.json()
        click.echo(f"promoted: {result.get('promoted', 0)}")
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"\n❌ Could not connect to API at {api_url}", err=True)
        click.echo("💡 Make sure the API server is running", err=True)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)


@memories_group.command("retrieve")
@click.option("--q", required=True, help="Query text")
@click.option("--top-k", type=int, default=5)
@click.option("--tag", default=None)
@click.option("--collection", default=None)
@click.option("--entity", default=None)
@api_url_option()
def retrieve_command(q: str, top_k: int, tag: str | None, collection: str | None, entity: str | None, api_url: str) -> None:
    """Search memories using semantic search via API."""
    try:
        params = {"q": q, "top_k": top_k}
        if tag:
            params["tag"] = tag
        if collection:
            params["collection"] = collection
        if entity:
            params["entity"] = entity
        url = f"{api_url.rstrip('/')}/api/v1/memories/search"
        headers = get_api_headers()
        response = api_request("GET", url, headers=headers, params=params, timeout=30)
        click.echo(json.dumps(response.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"\n❌ Could not connect to API at {api_url}", err=True)
        click.echo("💡 Make sure the API server is running", err=True)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)


@memories_group.command("retrieval-eval")
@click.option("--corpus-file", required=True, help="Path to JSONL corpus: {id, text, tags?}")
@click.option("--queries-file", required=True, help="Path to JSONL queries: {q, relevant_ids?}")
@click.option("--top-k", type=int, default=5)
@api_url_option()
def retrieval_eval_command(corpus_file: str, queries_file: str, top_k: int, api_url: str) -> None:
    """Run a retrieval evaluation via API."""
    try:
        with open(corpus_file, "r") as f:
            corpus = [json.loads(line) for line in f]
        with open(queries_file, "r") as f:
            queries = [json.loads(line) for line in f]
        url = f"{api_url.rstrip('/')}/api/v1/memories/search/eval"
        body = {"corpus": corpus, "queries": queries, "top_k": top_k}
        headers = get_api_headers()
        response = api_request("POST", url, headers=headers, json=body, timeout=300)
        click.echo(json.dumps(response.json(), indent=2))
    except click.ClickException:
        raise
    except requests.exceptions.ConnectionError:
        click.echo(f"\n❌ Could not connect to API at {api_url}", err=True)
        click.echo("💡 Make sure the API server is running", err=True)
    except FileNotFoundError as e:
        click.echo(f"❌ File not found: {e}", err=True)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)

