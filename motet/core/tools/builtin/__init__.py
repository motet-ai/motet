"""
Motet - Built-in Tools Registration

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-22

Description:
    Single source of truth for registering all built-in agent tools onto a
    ToolRegistry. ``register_all_builtin_tools`` iterates a data-driven table of
    (label, module, register_attr) entries, importing and invoking each tool's
    registrar lazily so a single broken/optional tool cannot prevent the rest
    from registering. Failures are logged (never silently swallowed) so missing
    tools are diagnosable rather than silently absent from the registry/index.

    The runtime singleton registry (motet.core.tools.registry, used by
    MotetStack.tool_registry) registers built-ins by delegating to this function,
    so there is exactly one list of built-in tools to maintain.

Dependencies:
    - importlib: Lazy per-tool module import for registration resilience
    - structlog: Structured, visible logging of per-tool registration failures
    - ToolRegistry: Registry the tools are registered onto

Usage:
    from motet.core.tools.builtin import register_all_builtin_tools
    register_all_builtin_tools(registry)

    # Fail-fast (e.g. in tests that assert every tool registers cleanly):
    register_all_builtin_tools(registry, strict=True)

Notes:
    - Adding a new built-in tool is a single-line edit to ``_BUILTIN_TOOL_SPECS``.
    - A drift guard test (tests/unit/core/tools/test_builtin_registration.py)
      asserts every registrar in the table ends up in the registry.
"""

from __future__ import annotations

import importlib
from typing import Any, List, Tuple

import structlog

logger = structlog.get_logger(__name__)


# Single source of truth for built-in tools, in registration order.
# Each entry is (label, module_name, register_attr) where module_name is relative
# to this package and register_attr is the registration callable on that module.
_BUILTIN_TOOL_SPECS: List[Tuple[str, str, str]] = [
    ("file_read", "file_read", "register"),
    ("file_write", "file_write", "register"),
    ("file_edit", "file_edit", "register"),
    ("file_search", "file_search", "register"),
    ("file_grep", "file_grep", "register"),
    ("clipboard_read", "clipboard_read", "register"),
    ("clipboard_write", "clipboard_write", "register"),
    ("host_exec", "host_exec", "register"),
    ("worker_exec", "worker_exec", "register"),
    ("edge_exec", "edge_exec", "register"),
    ("workspace_shell_exec", "workspace_shell_exec", "register"),
    ("activate_skill", "activate_skill", "register"),
    ("process_control", "process_control", "register"),
    ("http_get", "http_get", "register"),
    ("http_get_browser", "http_get_browser", "register_browser"),
    ("http_post", "http_post", "register"),
    ("math_eval", "math_eval", "register"),
    ("current_time", "current_time", "register"),
    ("memory_store", "memory_store", "register"),
    ("memory_recall", "memory_recall", "register"),
    ("memory_tag", "memory_tag", "register"),
    ("memory_forget", "memory_forget", "register"),
    ("note", "note", "register"),
    ("tool_describe", "tool_describe", "register"),
    ("tool_call", "tool_call", "register"),
    ("tools_list", "tools_list", "register"),
    ("tools_search", "tools_search", "register"),
    ("search_artifacts", "search_artifacts", "register"),
    ("artifact_read", "artifact_read", "register"),
    ("artifact_view", "artifact_view", "register"),
    ("image_generation", "image_generation", "register"),
    ("web_search", "web_search", "register"),
    ("spawn_agents", "spawn_agents", "register"),
    ("handoff", "handoff", "register"),
    ("oauth_login", "oauth_login", "register"),
    ("oauth_list", "oauth_list", "register"),
    ("oauth_logout", "oauth_logout", "register"),
    ("oauth_download_url_with_token", "oauth_download_url_with_token", "register"),
    ("schedule_command", "schedule_command", "register"),
    ("scheduled_commands_list", "scheduled_commands_list", "register"),
    ("manage_schedule", "manage_schedule", "register"),
    ("commands_list", "commands_list", "register"),
    ("workflows_list", "workflows_list", "register"),
    ("docs_read", "docs_read", "register"),
    ("workflow_builder", "workflow_builder", "register"),
    ("agents_list", "agents_list", "register"),
    ("command_describe", "command_describe", "register"),
    ("help", "help", "register"),
    ("admin_tools", "admin_tools", "register"),
]


def register_all_builtin_tools(registry: Any, *, strict: bool = False) -> List[str]:
    """Register all built-in tools onto ``registry``.

    Iterates ``_BUILTIN_TOOL_SPECS`` and imports/invokes each registrar lazily.
    By default this is per-tool resilient: if one tool's import or registration
    raises, the failure is logged and the remaining tools are still registered.

    Args:
        registry: ToolRegistry instance to register tools onto.
        strict: If True, re-raise the first registration failure instead of
            logging and continuing. Useful for tests that require a clean,
            complete registration.

    Returns:
        List of labels for tools that registered successfully.
    """
    registered: List[str] = []
    for label, module_name, register_attr in _BUILTIN_TOOL_SPECS:
        try:
            module = importlib.import_module(f".{module_name}", __package__)
            register_fn = getattr(module, register_attr)
            register_fn(registry)
            registered.append(label)
        except Exception as e:
            logger.warning(
                "builtin_tool_registration_failed",
                tool=label,
                module=module_name,
                register_attr=register_attr,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            if strict:
                raise
    logger.debug(
        "builtin_tools_registered",
        registered_count=len(registered),
        total=len(_BUILTIN_TOOL_SPECS),
    )
    return registered


__all__ = [
    "register_all_builtin_tools",
    "_BUILTIN_TOOL_SPECS",
]
