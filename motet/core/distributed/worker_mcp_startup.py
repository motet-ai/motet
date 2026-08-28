"""
Motet - Worker MCP Startup

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Worker-side MCP startup module: starts the MCP tool watcher
    that subscribes to the sibling MCPInstanceManager's lifecycle signals
    and registers tools as services come online.

    The MCPInstanceManager runs as a sibling deployment
    (``mcp-manager`` compose service in dev / edge, sidecar pod in
    cloud k8s) and is discovered via ``MOTET_MCP_MANAGER_ENDPOINT``. The
    routing prefix on Redis Streams / PUB-SUB / readiness sets is
    ``MOTET_MCP_MANAGER_ID``.

Dependencies:
    - structlog: Structured logging and observability
    - threading: Watcher daemon thread

Usage:
    from motet.core.distributed.worker_mcp_startup import ensure_mcp_watcher_started
    ensure_mcp_watcher_started(worker_id, tool_registry)

Notes:
    - The watcher is the only worker-side MCP component; it runs in every
      Celery process (parent + children) and is PID-aware.
    - Manager process lifecycle is owned by the orchestrator (compose / k8s).
"""

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, cast

import structlog
import yaml

logger = structlog.get_logger(__name__)

_MCP_MANAGER_BINDING_RETRY_ATTEMPTS = 10
_MCP_MANAGER_BINDING_RETRY_INTERVAL_SECONDS = 1.0


def _resolve_manager_id(worker_id: Optional[str] = None) -> Optional[str]:
    """
    Resolve the MCP manager id (ADR-0105 §R2/§R3).

    Order of precedence:
    1. ``MOTET_MCP_MANAGER_ID`` env var (the canonical knob — set by the
       orchestrator template alongside the sibling manager service).
    2. ``Config.mcp_manager_id`` (same source, surfaced through the typed
       settings schema).
    3. Synthesized fallback ``mcp-{worker_id}`` for tests / single-process
       invocations that have a worker_id but no orchestrator template.

    Returns ``None`` when MCP is enabled but neither env nor config is set
    AND no worker_id is available — callers must hard-fail when this happens.
    """
    raw = os.getenv("MOTET_MCP_MANAGER_ID", "").strip()
    if raw:
        return raw

    try:
        from motet.core.config import Config

        cfg_id = (Config().mcp_manager_id or "").strip()
        if cfg_id:
            return cfg_id
    except Exception as exc:
        logger.debug("mcp_manager_id_config_lookup_failed", error=str(exc))

    if worker_id:
        synth = f"mcp-{worker_id}"
        logger.debug(
            "mcp_manager_id_synthesized_from_worker_id",
            worker_id=worker_id,
            manager_id=synth,
            note=(
                "MOTET_MCP_MANAGER_ID and Config.mcp_manager_id are both unset; "
                "synthesizing from worker_id for back-compat. Set MOTET_MCP_MANAGER_ID "
                "explicitly in the orchestrator template (ADR-0105 §R2)."
            ),
        )
        return synth

    return None


def _require_manager_endpoint_when_enabled() -> None:
    """
    Hard-fail at watcher startup if MCP is enabled but the orchestrator
    template forgot to set ``MOTET_MCP_MANAGER_ENDPOINT`` /
    ``MOTET_MCP_MANAGER_ID`` (ADR-0105 §R0).

    The error points operators directly at the docker-compose / Helm chart
    manager service rather than letting them debug a silent BLPOP hang.
    """
    if os.getenv("MOTET_MCP_ENABLED", "false").lower() != "true":
        return

    endpoint = os.getenv("MOTET_MCP_MANAGER_ENDPOINT", "").strip()
    manager_id = _resolve_manager_id()

    missing: List[str] = []
    if not endpoint:
        missing.append("MOTET_MCP_MANAGER_ENDPOINT")
    if not manager_id:
        missing.append("MOTET_MCP_MANAGER_ID")

    if missing:
        raise RuntimeError(
            "MCP is enabled (MOTET_MCP_ENABLED=true) but the sibling MCP "
            f"manager is not configured. Missing: {', '.join(missing)}. "
            "Per ADR-0105, workers no longer spawn an in-process MCP manager; "
            "the manager runs as a sibling 'mcp-manager' service "
            "(docker compose) or sidecar pod (k8s). Add the service to your "
            "compose/Helm template and inject MOTET_MCP_MANAGER_ENDPOINT "
            "(service DNS name) and MOTET_MCP_MANAGER_ID (the routing "
            "prefix, e.g. 'mcp-local-default') into the worker container. "
            "See docs/architecture/decisions/"
            "ADR-0105-decouple-mcp-instance-manager-from-worker-lifecycle.md."
        )


# Tokens from MCP identifiers that carry no intent/discovery signal.
_MCP_NAME_STOPWORDS = frozenset({
    # Grammatical filler common in tool names ("list_docs_in_folder")
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into",
    "is", "it", "my", "of", "on", "or", "the", "to", "with",
    # Plumbing tokens common in server/tool ids
    "mcp", "server", "tool", "tools", "api",
})
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9]+")


def _mcp_name_keywords(service_id: str, tool_name: str) -> List[str]:
    """
    Derive baseline keywords by tokenizing MCP identifiers.

    MCP tool names are almost universally verb-noun phrases in snake_case,
    kebab-case, dotted, or camelCase form ("send_gmail_message",
    "list-docs-in-folder", "playTrack"), and the service id carries the domain
    noun ("spotify", "linear"). Splitting on those boundaries gives every
    server meaningful keywords with zero configuration — the curated
    service-specific branches in _infer_mcp_tool_capabilities then only add
    synonym enrichment on top. Stopwords, single characters, and numeric
    tokens are dropped.
    """
    tokens: List[str] = []
    for raw in (service_id, tool_name):
        spaced = _CAMEL_BOUNDARY_RE.sub(" ", raw or "")
        for token in _NON_ALNUM_RE.split(spaced):
            token = token.lower()
            if len(token) < 2 or token.isdigit() or token in _MCP_NAME_STOPWORDS:
                continue
            tokens.append(token)
    return list(dict.fromkeys(tokens))


def _infer_mcp_tool_capabilities(service_id: str, tool_name: str, description: str) -> Tuple[List[str], List[str]]:
    """Infer keywords and data_types for MCP tools based on PRIMARY function in tool name.
    
    This enables registry-based tool discovery to find MCP tools by keyword matching.
    Keywords are determined PRIMARILY by the tool NAME to avoid keyword pollution.

    Every tool gets a generic baseline from name tokenization
    (_mcp_name_keywords), so tools from arbitrary MCP servers are never
    keyword-less. The service-specific branches below layer curated synonyms
    on top (e.g. gmail → "email"/"inbox"), which tokenization cannot infer.
    """
    # Generic baseline: works for any server, known or not.
    keywords = _mcp_name_keywords(service_id, tool_name)
    data_types = []
    tool_lower = tool_name.lower()
    
    # Google Workspace tools - extract specific keywords based on PRIMARY function (tool name)
    if "google_workspace" in service_id:
        data_types.extend(["google_workspace", "documents", "productivity", "cloud"])
        keywords.append("google")
        
        # Gmail tools - ONLY if "gmail" is in the tool name
        if "gmail" in tool_lower:
            keywords.extend(["gmail", "email", "message", "inbox"])
            if "search" in tool_lower:
                keywords.extend(["search", "find"])
            if "send" in tool_lower or "draft" in tool_lower:
                keywords.extend(["send", "compose", "draft"])
            if "label" in tool_lower:
                keywords.extend(["label", "tag", "organize"])
            if "thread" in tool_lower:
                keywords.append("thread")
        
        # Drive tools - ONLY if "drive" or "file" or "folder" is in the tool name
        elif "drive" in tool_lower or "_file" in tool_lower or "folder" in tool_lower:
            keywords.extend(["drive", "file", "storage"])
            if "search" in tool_lower:
                keywords.extend(["search", "find", "query"])
            if "list" in tool_lower:
                keywords.extend(["list", "browse", "recent"])
            if "create" in tool_lower:
                keywords.extend(["create", "upload", "new"])
            if "get" in tool_lower or "content" in tool_lower:
                keywords.extend(["get", "read", "retrieve", "open"])
            if "permission" in tool_lower or "access" in tool_lower:
                keywords.extend(["permission", "share", "access"])
        
        # Docs tools - ONLY if "docs" or "doc" is in the tool name
        elif "docs" in tool_lower or "doc" in tool_lower:
            keywords.extend(["docs", "document", "text", "writing"])
            if "search" in tool_lower:
                keywords.extend(["search", "find", "recent"])
            if "create" in tool_lower:
                keywords.extend(["create", "new"])
            if "get" in tool_lower or "content" in tool_lower:
                keywords.extend(["read", "retrieve"])
        
        # Sheets tools - ONLY if "sheet" or "spreadsheet" is in the tool name
        elif "sheet" in tool_lower or "spreadsheet" in tool_lower:
            keywords.extend(["sheets", "spreadsheet", "table", "data"])
            if "search" in tool_lower:
                keywords.extend(["search", "find"])
            if "create" in tool_lower:
                keywords.extend(["create", "new"])
            if "get" in tool_lower or "read" in tool_lower:
                keywords.extend(["read", "retrieve"])
            if "modify" in tool_lower or "update" in tool_lower:
                keywords.extend(["edit", "update"])
        
        # Slides tools - ONLY if "slide" or "presentation" is in the tool name
        elif "slide" in tool_lower or "presentation" in tool_lower:
            keywords.extend(["slides", "presentation", "deck"])
            if "create" in tool_lower:
                keywords.extend(["create", "new"])
            if "get" in tool_lower:
                keywords.extend(["read", "retrieve"])
        
        # Calendar tools - ONLY if "calendar" or "event" is in the tool name
        elif "calendar" in tool_lower or "event" in tool_lower:
            keywords.extend(["calendar", "event", "meeting", "schedule"])
            if "list" in tool_lower or "get" in tool_lower:
                keywords.extend(["list", "retrieve"])
            if "create" in tool_lower:
                keywords.extend(["create", "new", "schedule"])
            if "modify" in tool_lower or "update" in tool_lower:
                keywords.extend(["edit", "update"])
        
        # Tasks tools - ONLY if "task" is in the tool name
        elif "task" in tool_lower:
            keywords.extend(["tasks", "todo", "checklist"])
            if "list" in tool_lower:
                keywords.extend(["list", "retrieve"])
            if "create" in tool_lower:
                keywords.extend(["create", "new"])
        
        # Chat tools - ONLY if "chat" or "space" or "message" is in the tool name (and NOT gmail)
        elif "chat" in tool_lower or "space" in tool_lower or ("message" in tool_lower and "gmail" not in tool_lower):
            keywords.extend(["chat", "space", "conversation"])
            if "send" in tool_lower:
                keywords.extend(["send", "post"])
            if "search" in tool_lower:
                keywords.extend(["search", "find"])
        
        # Forms tools - ONLY if "form" is in the tool name
        elif "form" in tool_lower:
            keywords.extend(["forms", "survey", "questionnaire"])
            if "create" in tool_lower:
                keywords.extend(["create", "new"])
            if "response" in tool_lower:
                keywords.extend(["response", "answer", "submission"])
        
        # Search tools - ONLY if "search" is in the tool name AND it's a general search tool
        elif "search" in tool_lower and "custom" in tool_lower:
            keywords.extend(["search", "find", "query", "web"])
    
    # Weather tools
    if "weather" in service_id or "weather" in tool_lower:
        data_types.extend(["weather", "forecast"])
        keywords.extend(["weather", "forecast", "temperature", "conditions"])
    
    # Slack tools
    if "slack" in service_id or "slack" in tool_lower:
        data_types.extend(["slack", "messaging", "collaboration"])
        keywords.extend(["slack", "message", "channel", "post", "chat"])
    
    # Playwright/browser tools
    if "playwright" in service_id or "browser" in tool_lower:
        data_types.extend(["browser", "web", "automation"])
        keywords.extend(["browser", "navigate", "click", "screenshot", "web"])
    
    # Web search tools - detect by service_id or tool name
    if ("web" in service_id.lower() and "search" in service_id.lower()) or \
       ("search" in tool_lower and ("web" in tool_lower or "internet" in tool_lower)):
        data_types.extend(["web", "search", "information", "current", "news", "facts"])
        keywords.extend([
            "search", "find", "lookup", "web", "internet", "current", "recent",
            "information", "data", "facts", "news", "popular", "attractions",
            "events", "activities", "tourism", "travel", "guide", "what", "where",
            "when", "how", "location", "places", "restaurants", "businesses"
        ])
    
    # Remove duplicates while preserving order
    data_types = list(dict.fromkeys(data_types))
    keywords = list(dict.fromkeys(keywords))
    
    return keywords, data_types


def _load_configured_service_ids() -> List[str]:
    """
    Load MCP service IDs from YAML or legacy JSON config (ADR-0069).
    Returns list of service_id strings. Empty if no config or no services.
    """
    config_path = os.getenv("MCP_INSTANCE_MANAGER_CONFIG", "/app/config/mcp_instance_manager.yaml")
    config_file = Path(config_path)
    if config_file.exists():
        try:
            with open(config_file, "r") as f:
                config_data = yaml.safe_load(f)
            if config_data and "services" in config_data:
                return [s.get("service_id") for s in config_data["services"] if s.get("service_id")]
        except Exception as e:
            logger.warning("mcp_load_config_failed", path=config_path, error=str(e))
        return []
    mcp_servers_json = os.getenv("MOTET_MCP_SERVERS_JSON", "{}")
    try:
        mcp_servers = json.loads(mcp_servers_json)
        return list(mcp_servers.keys()) if isinstance(mcp_servers, dict) else []
    except Exception as e:
        logger.warning("mcp_load_json_config_failed", error=str(e))
        return []


def _discover_and_register_tools_for_service(
    service_id: str,
    tool_registry: Any,
    worker_id: str,
) -> int:
    """
    Discover tools for one MCP service and register them (ADR-0069).
    Returns number of tools registered.
    """
    config_path = os.getenv("MCP_INSTANCE_MANAGER_CONFIG", "/app/config/mcp_instance_manager.yaml")
    config_file = Path(config_path)
    if config_file.exists():
        try:
            with open(config_file, "r") as f:
                config_data = yaml.safe_load(f)
        except Exception as e:
            logger.warning("mcp_watcher_load_config_failed", path=config_path, error=str(e))
            return 0
        mcp_servers = {}
        if config_data and "services" in config_data:
            for service_config in config_data["services"]:
                sid = service_config.get("service_id")
                if sid:
                    mcp_servers[sid] = {
                        "transport": service_config.get("transport", "stdio"),
                        "endpoint": service_config.get("command", ""),
                        "args": service_config.get("args", []),
                        "env": service_config.get("env", {}),
                        "visibility": service_config.get("visibility", "motet"),
                        "lifecycle_duration": service_config.get("lifecycle_duration", "permanent"),
                        "presentation": service_config.get("presentation"),
                    }
    else:
        mcp_servers_json = os.getenv("MOTET_MCP_SERVERS_JSON", "{}")
        try:
            mcp_servers = json.loads(mcp_servers_json)
        except Exception:
            mcp_servers = {}
    service_config = mcp_servers.get(service_id) if isinstance(mcp_servers, dict) else None
    if not service_config:
        logger.debug("mcp_watcher_service_not_in_config", service_id=service_id)
        return 0

    from motet.core.tools.mcp_motet.client.motet_mcp_client import get_motet_mcp_client
    from motet.core.tools.mcp_motet.protocol import Visibility, LifecycleDuration

    motet_manager = get_motet_mcp_client()
    visibility_enum = Visibility(service_config.get("visibility", "motet"))
    lifecycle_enum = LifecycleDuration(service_config.get("lifecycle_duration", "permanent"))
    discovery_tenant = "discovery-tenant"
    discovery_principal = "discovery-user" if visibility_enum == Visibility.USER else None
    discovery_conversation = "discovery-conversation" if lifecycle_enum == LifecycleDuration.CONVERSATION else None
    discovery_session = "discovery-session" if lifecycle_enum == LifecycleDuration.SESSION else None
    discovery_task = "discovery-task" if lifecycle_enum == LifecycleDuration.TASK else None
    discovery_timeout = int(os.getenv("MOTET_MCP_DISCOVERY_TIMEOUT_PARENT_SECONDS", "25"))

    try:
        tools_list = motet_manager.list_tools(
            service_id=service_id,
            target_worker_id=worker_id,
            tenant_id=discovery_tenant,
            principal_id=discovery_principal,
            visibility=visibility_enum,
            lifecycle=lifecycle_enum,
            conversation_id=discovery_conversation,
            task_id=discovery_task,
            session_id=discovery_session,
            timeout_seconds=discovery_timeout,
        )
    except Exception as e:
        logger.warning(
            "mcp_watcher_discover_failed",
            service_id=service_id,
            worker_id=worker_id,
            error=str(e),
        )
        return 0

    if not tools_list or tools_list.get("error") or not tools_list.get("tools"):
        err = (tools_list or {}).get("error") if isinstance(tools_list, dict) else None
        logger.warning(
            "mcp_watcher_no_tools",
            service_id=service_id,
            worker_id=worker_id,
            error=err,
            response_keys=list((tools_list or {}).keys()) if isinstance(tools_list, dict) else None,
        )
        return 0

    tools = tools_list["tools"]
    tool_count = 0
    registered_tool_names: List[str] = []
    for tool_schema in tools:
        mcp_tool_name = tool_schema.get("name", "unknown")
        tool_name = f"mcp.{service_id}.{mcp_tool_name}"

        def create_mcp_tool_wrapper(svc_id: str, tool_nm: str):
            def mcp_tool_wrapper(**kwargs) -> Dict[str, Any]:
                return motet_manager.call_tool(service_id=svc_id, tool_name=tool_nm, params=kwargs)
            return mcp_tool_wrapper

        tool_func = create_mcp_tool_wrapper(service_id, mcp_tool_name)
        tool_description = tool_schema.get("description", "")
        keywords, data_types = _infer_mcp_tool_capabilities(service_id, mcp_tool_name, tool_description)
        tool_lower = mcp_tool_name.lower()
        service_lower = service_id.lower()
        is_web_search = (
            ("web" in service_lower and "search" in service_lower)
            or ("search" in tool_lower and ("web" in tool_lower or "internet" in tool_lower))
        )
        tool_priority = 5 if is_web_search else 10
        tool_category = "search" if is_web_search else "mcp"
        presentation = None
        try:
            svc_pres = service_config.get("presentation") if isinstance(service_config, dict) else None
            if isinstance(svc_pres, dict):
                _default_raw = svc_pres.get("default")
                default_pres = _default_raw if isinstance(_default_raw, dict) else {}
                _tools_raw = svc_pres.get("tools")
                tool_overrides: Dict[str, Any] = _tools_raw if isinstance(_tools_raw, dict) else {}
                _tpres_raw = tool_overrides.get(mcp_tool_name)
                tool_pres: Dict[str, Any] = _tpres_raw if isinstance(_tpres_raw, dict) else {}
                merged = {}
                merged.update(default_pres or {})
                merged.update(tool_pres or {})
                presentation = merged or None
        except Exception:
            presentation = None

        try:
            tool_registry.register(
                name=tool_name,
                description=tool_description,
                func=tool_func,
                tool_schema=tool_schema,
                triggers=[],
                priority=tool_priority,
                category=tool_category,
                keywords=keywords,
                data_types=data_types,
                required_capabilities=(
                    ["TOOL_EXECUTION", "HTTP_OPERATIONS"]
                    if tool_category in {"http", "search"}
                    else ["TOOL_EXECUTION"]
                ),
                max_retries=3,
                retry_backoff_seconds=1.0,
                default_timeout_seconds=30,
                suggested_max_calls=None,
                contextualize_observation=None,
                presentation=presentation,
            )
            tool_count += 1
            registered_tool_names.append(tool_name)
        except Exception as reg_err:
            logger.warning("mcp_watcher_register_tool_failed", tool_name=tool_name, error=str(reg_err))

    logger.info(
        "mcp_watcher_service_registered",
        service_id=service_id,
        worker_id=worker_id,
        tool_count=tool_count,
    )

    # ADR-0069: Notify FunctionDiscoveryVectorStore for incremental indexing
    if registered_tool_names and _on_tools_added_callback is not None:
        try:
            _on_tools_added_callback(service_id, registered_tool_names)
        except Exception as cb_err:
            logger.warning(
                "mcp_watcher_discovery_index_callback_failed",
                service_id=service_id,
                error=str(cb_err),
            )

    return tool_count


def _unregister_tools_for_service(service_id: str, tool_registry: Any) -> int:
    """Unregister all tools for an MCP service (ADR-0069). Returns count removed."""
    prefix = f"mcp.{service_id}."
    if not hasattr(tool_registry, "unregister_by_prefix"):
        return 0
    removed = tool_registry.unregister_by_prefix(prefix)

    # ADR-0069: Notify FunctionDiscoveryVectorStore for incremental removal
    if removed > 0 and _on_tools_removed_callback is not None:
        try:
            _on_tools_removed_callback(service_id)
        except Exception as cb_err:
            logger.warning(
                "mcp_watcher_discovery_index_remove_callback_failed",
                service_id=service_id,
                error=str(cb_err),
            )

    return removed


def _should_publish_mcp_readiness_to_redis() -> bool:
    """
    Whether the MCP watcher should write tool counts to WorkerReadinessService.

    ``is_celery_parent_process()`` alone is wrong for gevent/threads (Celery often
    sets helper env vars so the real worker is classified as non-parent) and for
    prefork children (they own the tool registry that receives MCP registrations).
    """
    try:
        from motet.core.workers.parent_coordinator import is_celery_parent_process
        from motet.core.workers.worker_utils import detect_worker_pool_type

        pool = detect_worker_pool_type()
        if pool in ("gevent", "eventlet", "threads"):
            return True
        if pool == "fork":
            return not is_celery_parent_process()
        return is_celery_parent_process()
    except Exception:
        return True


def _update_watcher_readiness(worker_id: str, tool_registry: Any) -> None:
    """Update worker readiness with current tool counts (ADR-0069)."""
    try:
        if not _should_publish_mcp_readiness_to_redis():
            logger.debug(
                "mcp_watcher_readiness_skip_non_publishing_process",
                worker_id=worker_id,
            )
            return
        from motet.core.distributed.worker_readiness import get_readiness_service
        tools = tool_registry.list_items()
        tool_count = len(tools)
        mcp_tool_count = len([t for t in tools if str(t).startswith("mcp.")])
        serialized_tools = []
        for tool_name, tool_info in tools.items():
            serialized_tools.append({
                "name": tool_name,
                "description": getattr(tool_info, "description", "No description"),
                "category": getattr(tool_info, "category", "unknown"),
                "keywords": getattr(tool_info, "keywords", []),
                "data_types": getattr(tool_info, "data_types", []),
                "priority": getattr(tool_info, "priority", 0),
                "cost_class": getattr(tool_info, "cost_class", None),
                "is_mcp": tool_name.startswith("mcp."),
            })
        readiness_service = get_readiness_service()
        readiness_service.update_worker_tools(
            worker_id=worker_id,
            tools=serialized_tools,
            tool_count=tool_count,
            mcp_tool_count=mcp_tool_count,
        )
        readiness_service.mark_worker_ready(
            worker_id=worker_id,
            tool_count=tool_count,
            mcp_tool_count=mcp_tool_count,
            warmup_duration_ms=0,
        )
        logger.debug(
            "mcp_watcher_readiness_updated",
            worker_id=worker_id,
            tool_count=tool_count,
            mcp_tool_count=mcp_tool_count,
        )
    except Exception as e:
        logger.warning("mcp_watcher_readiness_update_failed", worker_id=worker_id, error=str(e))


# ADR-0069: Process-local flag so we start the watcher only once per process.
# IMPORTANT: Must be PID-aware because fork() children inherit the parent's
# memory (including this flag set to True) but do NOT inherit threads.  Without
# the PID check a forked child would think the watcher is running when it isn't.
_mcp_watcher_started_pid: Optional[int] = None

# ADR-0069: Callbacks for incremental vector-index updates.
# Set by _create_worker_context() after FunctionDiscoveryVectorStore is created.
# Called by the watcher when MCP tools are added or removed.
# NOTE: After fork(), the child inherits stale parent callbacks via COW.  The
# child's _create_worker_context() overwrites them with correct closures before
# the child's watcher has discovered any tools (safe due to reconciliation pass).
_on_tools_added_callback: Optional[Callable[[str, List[str]], None]] = None  # (service_id, tool_names)
_on_tools_removed_callback: Optional[Callable[[str], None]] = None  # (service_id,)


def set_discovery_index_callbacks(
    on_added: Optional[Callable[[str, List[str]], None]] = None,
    on_removed: Optional[Callable[[str], None]] = None,
) -> None:
    """
    Register callbacks for incremental function-discovery indexing (ADR-0069).

    Called from ``_create_worker_context()`` after the ``FunctionDiscoveryVectorStore``
    is created.  The watcher thread invokes these callbacks whenever MCP tools
    are added or removed, enabling incremental hybrid-index updates without a
    full re-index.

    Args:
        on_added: ``(service_id, tool_names) -> None`` — index newly registered tools.
        on_removed: ``(service_id) -> None`` — remove tools for a service from the index.
    """
    global _on_tools_added_callback, _on_tools_removed_callback
    _on_tools_added_callback = on_added
    _on_tools_removed_callback = on_removed
    logger.info(
        "mcp_discovery_index_callbacks_set",
        on_added=on_added is not None,
        on_removed=on_removed is not None,
    )


def ensure_mcp_watcher_started(worker_id: str, tool_registry: Any) -> None:
    """
    Start the MCP tool watcher daemon thread once per process (ADR-0069).

    PID-aware: after ``fork()`` the child inherits the parent's flag but NOT its
    threads, so we compare against ``os.getpid()`` to ensure each process starts
    its own watcher exactly once.

    ADR-0105 §R2: the watcher subscribes on the manager-keyed signal channel
    (``motet:mcp:signals:{manager_id}``)
    and reads the manager-keyed readiness set
    (``motet:mcp:ready_services:{manager_id}``). The ``worker_id`` argument
    stays for telemetry/log labeling per ADR-0105 §R3 (bootstrap-attribution),
    but is no longer the routing key.
    """
    global _mcp_watcher_started_pid
    current_pid = os.getpid()
    if _mcp_watcher_started_pid == current_pid:
        return
    if os.getenv("MOTET_MCP_ENABLED", "false").lower() != "true":
        return

    # ADR-0105 §R0: hard-fail if MCP is enabled but the orchestrator template
    # forgot to wire the sibling manager. Better than a silent BLPOP hang.
    _require_manager_endpoint_when_enabled()

    manager_id = _resolve_manager_id(worker_id)
    if not manager_id:
        # _require_manager_endpoint_when_enabled() above should have raised
        # already; defensive belt-and-braces.
        raise RuntimeError(
            "MOTET_MCP_ENABLED=true but MOTET_MCP_MANAGER_ID is unset and "
            "no worker_id is available to synthesize from. See ADR-0105 §R2."
        )

    _mcp_watcher_started_pid = current_pid

    # ADR-0105 §R3: publish worker→manager_id binding so the /managers/status
    # API can compute served_workers per manager (the sibling manager cannot
    # derive this from anonymous Redis Stream traffic).
    try:
        from motet.core.distributed.worker_readiness import WorkerReadinessService

        readiness = WorkerReadinessService()
        bound = readiness.update_worker_mcp_manager_binding(worker_id, manager_id)
        if not bound:
            threading.Thread(
                target=_retry_worker_manager_binding_publish,
                args=(worker_id, manager_id),
                daemon=True,
                name="mcp-manager-binding-retry",
            ).start()
    except Exception as e:
        logger.warning(
            "mcp_manager_binding_publish_failed",
            worker_id=worker_id,
            manager_id=manager_id,
            error=str(e),
        )

    # Clear stale parent callbacks inherited via fork() so the watcher doesn't
    # invoke closures that reference the parent's (now-stale) vector store.
    global _on_tools_added_callback, _on_tools_removed_callback
    _on_tools_added_callback = None
    _on_tools_removed_callback = None

    t = threading.Thread(
        target=_mcp_tool_watcher,
        args=(worker_id, tool_registry, manager_id),
        daemon=True,
        name="mcp-tool-watcher",
    )
    t.start()
    logger.info(
        "mcp_watcher_thread_started",
        worker_id=worker_id,
        manager_id=manager_id,
        pid=current_pid,
    )


def _retry_worker_manager_binding_publish(worker_id: str, manager_id: str) -> None:
    """Retry the worker→manager binding until worker readiness state exists."""

    from motet.core.distributed.worker_readiness import WorkerReadinessService

    readiness = WorkerReadinessService()
    for attempt in range(1, _MCP_MANAGER_BINDING_RETRY_ATTEMPTS + 1):
        time.sleep(_MCP_MANAGER_BINDING_RETRY_INTERVAL_SECONDS)
        try:
            if readiness.update_worker_mcp_manager_binding(worker_id, manager_id):
                logger.info(
                    "mcp_manager_binding_publish_recovered",
                    worker_id=worker_id,
                    manager_id=manager_id,
                    attempt=attempt,
                )
                return
        except Exception as exc:
            logger.warning(
                "mcp_manager_binding_retry_failed",
                worker_id=worker_id,
                manager_id=manager_id,
                attempt=attempt,
                error=str(exc),
                exc_info=True,
            )
    logger.warning(
        "mcp_manager_binding_retry_exhausted",
        worker_id=worker_id,
        manager_id=manager_id,
        attempts=_MCP_MANAGER_BINDING_RETRY_ATTEMPTS,
    )


def _mcp_watcher_parallel_max_workers(num_services: int) -> int:
    """Pool size for parallel MCP discovery in the watcher thread (default 1 = sequential)."""
    raw = os.getenv("MOTET_MCP_WATCHER_DISCOVERY_MAX_WORKERS", "").strip()
    if raw:
        return max(1, min(int(raw), max(1, num_services)))
    return 1


def _discover_services_parallel(
    service_ids: List[str],
    tool_registry: Any,
    worker_id: str,
    registered_services: Set[str],
    configured_services: Optional[Set[str]],
    *,
    phase: str,
) -> bool:
    """
    Run MCP discovery (list_tools + registry.register) for many services at once.

    ToolRegistry.register uses an internal lock, so concurrent registration is safe.
    ``configured_services`` is updated only on successful discovery when provided (catch-up);
    poll phase passes None and only updates ``registered_services``.
    Returns True if any service newly registered at least one tool.
    """
    ids = [s for s in service_ids if s]
    if not ids:
        return False

    if len(ids) == 1:
        sid = ids[0]
        try:
            count = _discover_and_register_tools_for_service(sid, tool_registry, worker_id)
            if count > 0:
                registered_services.add(sid)
                if phase == "poll":
                    logger.info(
                        "mcp_watcher_poll_registered",
                        service_id=sid,
                        worker_id=worker_id,
                        tool_count=count,
                    )
            if configured_services is not None:
                configured_services.add(sid)
            return count > 0
        except Exception as e:
            log_event = (
                "mcp_watcher_catchup_skip" if configured_services is not None else "mcp_watcher_poll_discover_failed"
            )
            logger.debug(log_event, service_id=sid, error=str(e))
            return False

    max_workers = _mcp_watcher_parallel_max_workers(len(ids))
    any_new = False
    logger.info(
        "mcp_watcher_parallel_discover_start",
        worker_id=worker_id,
        phase=phase,
        services=len(ids),
        max_workers=max_workers,
    )

    def _work(sid: str) -> Tuple[str, int, bool]:
        """Returns (service_id, count, success_without_exception)."""
        try:
            count = _discover_and_register_tools_for_service(sid, tool_registry, worker_id)
            return sid, count, True
        except Exception as e:
            log_event = (
                "mcp_watcher_catchup_skip" if configured_services is not None else "mcp_watcher_poll_discover_failed"
            )
            logger.debug(log_event, service_id=sid, error=str(e))
            return sid, 0, False

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mcp-discover") as pool:
        futures = [pool.submit(_work, sid) for sid in ids]
        for fut in as_completed(futures):
            sid, count, ok = fut.result()
            if not ok:
                continue
            if count > 0:
                registered_services.add(sid)
                any_new = True
                if phase == "poll":
                    logger.info(
                        "mcp_watcher_poll_registered",
                        service_id=sid,
                        worker_id=worker_id,
                        tool_count=count,
                    )
            if configured_services is not None:
                configured_services.add(sid)

    logger.info(
        "mcp_watcher_parallel_discover_done",
        worker_id=worker_id,
        phase=phase,
        services=len(ids),
        any_new_tools=any_new,
    )
    return any_new


def _mcp_tool_watcher(worker_id: str, tool_registry: Any, manager_id: str) -> None:
    """
    Continuous watcher: SUBSCRIBE to lifecycle channel (ADR-0069). Runs in every process (parent + children).
    Daemon thread — runs until worker process exits. Only parent updates readiness.

    Strategy (hybrid PUB/SUB + polling fallback):
    1. Subscribe to Redis PUB/SUB channel for instant service_ready / service_removed events.
    2. Immediately do a durable-set catch-up (services that published before we subscribed).
    3. If any configured services are still unregistered, poll the durable set every few seconds
       with exponential backoff until all services are discovered or a max wait is reached.
    4. After initial discovery completes, keep listening on PUB/SUB for live updates (restarts,
       removals, new services).

    ADR-0105 §R2: ``manager_id`` is the bus-routing prefix for the signal
    channel and readiness set. ``worker_id`` is retained as a telemetry tag
    (which worker initiated which discovery) and for legacy log labels.
    """
    import time as _time

    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client
        redis_client = get_sync_redis_client("mcp_instance_manager")
    except Exception as e:
        logger.warning(
            "mcp_watcher_redis_failed",
            worker_id=worker_id,
            manager_id=manager_id,
            error=str(e),
        )
        return

    from motet.core.distributed.tenant_keys import product_key

    channel = product_key(f"mcp:signals:{manager_id}")
    configured_services: Set[str] = set(_load_configured_service_ids())
    registered_services: Set[str] = set()
    ready_set_key = product_key(f"mcp:ready_services:{manager_id}")

    def _ready_service_ids() -> set[Any]:
        return set(cast(Any, redis_client.smembers(ready_set_key)) or set())

    # --- Phase 1: Subscribe to PUB/SUB channel first (so we don't miss events during catch-up) ---
    logger.info(
        "mcp_watcher_subscribing",
        worker_id=worker_id,
        manager_id=manager_id,
        channel=channel,
    )
    pubsub = redis_client.pubsub()
    pubsub.subscribe(channel)

    # --- Phase 2: Durable-set catch-up (services that published before we subscribed) ---
    try:
        ready_service_ids = _ready_service_ids()
        catchup_ids = [
            (sid.decode() if isinstance(sid, bytes) else sid) for sid in (ready_service_ids or [])
        ]
        if catchup_ids:
            _discover_services_parallel(
                catchup_ids,
                tool_registry,
                worker_id,
                registered_services,
                configured_services,
                phase="catchup",
            )
        if registered_services:
            _update_watcher_readiness(worker_id, tool_registry)
            logger.info("mcp_watcher_catchup_done", worker_id=worker_id,
                        services_registered=len(registered_services),
                        services_configured=len(configured_services))
    except Exception as e:
        logger.debug("mcp_watcher_catchup_failed", worker_id=worker_id, error=str(e))

    # --- Phase 3: Polling fallback for services not yet in durable set ---
    # The MCP subprocess may not have finished creating instances yet; poll until
    # all configured services are registered or we hit the max wait.
    # Default scales with configured services × per-service init cap so we don't bail at 180s
    # while the instance manager is still creating Docker/subprocess MCP servers.
    _per_svc_timeout = int(os.getenv("MOTET_MCP_PER_SERVICE_INIT_TIMEOUT_SECONDS", "240"))
    _default_poll = max(300, len(configured_services) * _per_svc_timeout + 120)
    _poll_env = os.getenv("MOTET_MCP_WATCHER_POLL_TIMEOUT_SECONDS", "").strip()
    max_poll_seconds = int(_poll_env) if _poll_env else _default_poll
    poll_interval = 5  # seconds between polls, increases with backoff
    max_poll_interval = 30
    poll_start = _time.time()
    pending_services = configured_services - registered_services

    if pending_services:
        logger.info("mcp_watcher_polling_start", worker_id=worker_id,
                     pending_services=sorted(pending_services),
                     max_poll_seconds=max_poll_seconds)

    while pending_services and (_time.time() - poll_start) < max_poll_seconds:
        # Drain any PUB/SUB messages that arrived while we were polling
        _drain_pubsub_messages(pubsub, tool_registry, worker_id, registered_services, configured_services)

        # Re-check durable set
        try:
            ready_service_ids = _ready_service_ids()
            ready_sids = {
                (sid.decode() if isinstance(sid, bytes) else sid)
                for sid in (ready_service_ids or [])
            }
        except Exception:
            ready_sids = set()

        newly_ready = ready_sids - registered_services
        if newly_ready:
            if _discover_services_parallel(
                sorted(newly_ready),
                tool_registry,
                worker_id,
                registered_services,
                None,
                phase="poll",
            ):
                _update_watcher_readiness(worker_id, tool_registry)

        pending_services = configured_services - registered_services
        if pending_services:
            logger.debug("mcp_watcher_polling_wait", worker_id=worker_id,
                         pending=sorted(pending_services),
                         elapsed=round(_time.time() - poll_start, 1),
                         next_poll=poll_interval)
            _time.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.5, max_poll_interval)

    if registered_services:
        _update_watcher_readiness(worker_id, tool_registry)
    if pending_services:
        logger.warning("mcp_watcher_polling_timeout", worker_id=worker_id,
                        registered=sorted(registered_services),
                        still_pending=sorted(pending_services),
                        elapsed=round(_time.time() - poll_start, 1))
    else:
        logger.info("mcp_watcher_all_services_registered", worker_id=worker_id,
                     services=sorted(registered_services),
                     elapsed=round(_time.time() - poll_start, 1))

    # --- Phase 4: Steady-state PUB/SUB listener ---
    logger.info("mcp_watcher_entering_steady_state", worker_id=worker_id)
    for message in pubsub.listen():
        _handle_pubsub_message(message, tool_registry, worker_id, registered_services, configured_services)


def _drain_pubsub_messages(
    pubsub: Any,
    tool_registry: Any,
    worker_id: str,
    registered_services: Set[str],
    configured_services: Set[str],
) -> None:
    """Drain all pending PUB/SUB messages without blocking."""
    while True:
        message = pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if message is None:
            break
        _handle_pubsub_message(message, tool_registry, worker_id, registered_services, configured_services)


def _handle_pubsub_message(
    message: Dict[str, Any],
    tool_registry: Any,
    worker_id: str,
    registered_services: Set[str],
    configured_services: Set[str],
) -> None:
    """Process a single PUB/SUB message (service_ready / service_restarted / service_removed)."""
    if message.get("type") != "message":
        return
    data = message.get("data")
    if data is None:
        return
    payload = data.decode() if isinstance(data, bytes) else data
    signal_type, _, service_id = payload.partition(":")
    if not service_id:
        return
    if signal_type in ("service_ready", "service_restarted"):
        count = _discover_and_register_tools_for_service(service_id, tool_registry, worker_id)
        if count > 0:
            registered_services.add(service_id)
        configured_services.add(service_id)
        _update_watcher_readiness(worker_id, tool_registry)
    elif signal_type == "service_removed":
        removed = _unregister_tools_for_service(service_id, tool_registry)
        configured_services.discard(service_id)
        registered_services.discard(service_id)
        if removed:
            _update_watcher_readiness(worker_id, tool_registry)
        logger.info("mcp_watcher_service_removed", service_id=service_id, tools_removed=removed)


# Export for use by workers and parent coordination (ADR-0069: event-driven watcher)
__all__ = [
    "_load_configured_service_ids",
    "_discover_and_register_tools_for_service",
    "_discover_services_parallel",
    "_unregister_tools_for_service",
    "_mcp_tool_watcher",
    "ensure_mcp_watcher_started",
    "_resolve_manager_id",
]
