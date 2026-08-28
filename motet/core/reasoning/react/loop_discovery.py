"""
Motet - Agentic Loop Discovery Helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Discovery and shortlist helpers for the agentic loop (issue #147).
    Context-query enrichment, discovery filters, keyword pin tables
    for intent-critical core tools, sticky shortlist schema merge, and
    exec/catalog parameter normalization used before tool execution.
    Memory intent pins `core.memory_store` + `core.memory_recall` (issue #217).
    Forget-intent phrases pin `core.memory_forget` separately; "don't forget"
    stays on the store/recall list.

Dependencies:
    - structlog: Structured logging for distributed tracing
    - tool_shortlist: merge_sticky_tool_names for frozen meta-bag membership
    - ToolSchemaExporter / WorkflowRegistry: resolve schemas by name
    - AgenticLoopData: skill_refs for exec/catalog parameter normalization
    - Message: conversation history typing for context-query enrichment

Usage:
    from motet.core.reasoning.react.loop_discovery import (
        merge_sticky_tool_schemas,
        _keyword_pinned_tool_names,
        _apply_discovery_filters,
    )

    schemas = merge_sticky_tool_schemas(
        sticky_names=["core.help"],
        motet=motet,
        max_tools=10,
        query="remind me tomorrow",
    )

Notes:
    - This module is the home of these symbols: import and patch them here.
      agentic_loop imports only the few it calls, so patching via agentic_loop
      is not a supported path.
    - Naming: helpers crossing a module boundary are public; keyword-pin tables
      and single-module helpers keep the leading underscore. A leading underscore
      on a cross-module name makes Pyright report the definition as unaccessed
      and the importer as reportPrivateUsage.
    - ``tool_schema_name`` is imported from ``motet.core.types``, next to the
      ``CanonicalToolSchema`` it reads. ``types.py`` is the home because it is a
      stdlib-only leaf; putting the helper in orchestration would point
      ``reasoning`` at ``orchestration``.
    - Keyword pin phrases stay specific enough to avoid pinning on everyday
      language; pins are the only non-permanent names admitted into the frozen
      meta bag.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import structlog

from ...types import Message, tool_schema_name
from .agentic_loop_data import AgenticLoopData

logger = structlog.get_logger(__name__)


def build_context_query(
    query: str,
    conversation_history: Optional[List[Message]],
    *,
    short_word_threshold: int = 4,
    context_chars: int = 300,
) -> str:
    """
    Enrich a short/affirmative query (e.g. "yes", "ok", "go ahead") with context from
    recent conversation history so that the embedding search has meaningful signal.

    When the user's reply is a brief confirmation, the prior user intent and assistant plan
    carry far more semantic content than the current message. This prevents the embedding
    store from returning random high-popularity tools instead of the one the model just
    announced it would use.

    Strategy:
    - If query has `short_word_threshold` or more words, return it unchanged.
    - Otherwise walk conversation history in reverse to collect:
        1. The most recent prior user message (original intent).
        2. The most recent assistant message content (planned action / tool announcement).
    - Blend: "{prior_user_intent} | {assistant_plan} | {query}"

    Only non-empty parts are included; the current query is always kept.
    """
    if not conversation_history or len(query.split()) >= short_word_threshold:
        return query

    prior_user: str = ""
    assistant_plan: str = ""
    current_user_seen = False

    for msg in reversed(conversation_history):
        role = getattr(msg, "role", None) if not isinstance(msg, dict) else msg.get("role")
        content = getattr(msg, "content", "") if not isinstance(msg, dict) else msg.get("content", "")
        content = str(content or "").strip()

        if role == "user":
            if not current_user_seen:
                # Skip the current user message (same text as query, already included).
                current_user_seen = True
                continue
            prior_user = content[:context_chars]
            break
        elif role == "assistant" and not assistant_plan:
            assistant_plan = content[:context_chars]

    parts = [p for p in [prior_user, assistant_plan, query] if p]
    return " | ".join(parts) if len(parts) > 1 else query


def normalize_exec_and_catalog_parameters(
    unique_tool_calls: List[Dict[str, Any]],
    data: AgenticLoopData,
) -> None:
    """
    Normalize common skill-execution parameter mistakes before tool execution.

    - If a tool call passes skill_id where bundle_id is required, map it via skill_refs
      when available, else treat values with a dot as `bundle.skill` and use the prefix.
    - Infer bundle_id from ``$MOTET_PLUGIN_ROOT/<slug>/`` paths in argv when needed, and
      prefer that slug when it disagrees with a bogus ``bundle_id`` (e.g. skill folder name).
    - Rewrite bundle script path entries (`/work/skills/...` and deployed absolute
      paths) to bundle-relative ``skills/...``, including when the model omits the
      ``skills/`` directory segment under the bundle root.
    """
    refs = list(data.skill_refs or [])

    skill_to_bundle: Dict[str, str] = {}
    discovered_bundles: set[str] = set()
    for ref in refs:
        skill_id = str(getattr(ref, "skill_id", "") or "").strip()
        bundle_id = str(getattr(ref, "bundle_id", "") or "").strip()
        if not skill_id:
            continue
        if not bundle_id and "." in skill_id:
            bundle_id = skill_id.split(".", 1)[0].strip()
        if bundle_id:
            skill_to_bundle[skill_id] = bundle_id
            discovered_bundles.add(bundle_id)

    default_bundle_id: Optional[str] = None
    if len(discovered_bundles) == 1:
        default_bundle_id = next(iter(discovered_bundles))
    plugin_root = (os.getenv("MOTET_PLUGIN_ROOT", "/tmp/imf_bundles") or "/tmp/imf_bundles").rstrip("/")

    def _mapped_bundle_id(raw: Any) -> Optional[str]:
        if not isinstance(raw, str):
            return None
        candidate = raw.strip()
        if not candidate:
            return None
        if candidate in skill_to_bundle:
            return skill_to_bundle[candidate]
        # Conventional skill id `bundle_id.skill_name` when the model uses skill_id as bundle_id.
        if "." in candidate:
            prefix = candidate.split(".", 1)[0].strip()
            if prefix:
                return prefix
        return candidate

    exec_tool_names = {
        "core.worker_exec",
        "core.host_exec",
    }

    for tool_call in unique_tool_calls:
        tool_name = str(tool_call.get("tool_name") or "")
        params = tool_call.get("parameters")
        if not isinstance(params, dict):
            continue

        if tool_name == "motet_admin.get_bundle_catalog":
            bundle_id = _mapped_bundle_id(params.get("bundle_id"))
            if bundle_id:
                params["bundle_id"] = bundle_id
            continue

        if tool_name not in exec_tool_names:
            continue

        argv = params.get("argv")
        inferred_bundle: Optional[str] = None
        if isinstance(argv, list):
            pref = f"{plugin_root}/"
            slug_tokens: List[str] = []
            for a in argv:
                if isinstance(a, str) and a.startswith(pref):
                    rest = a[len(pref) :].lstrip("/")
                    if rest:
                        slug_tokens.append(rest.split("/", 1)[0])
            uniq_slugs = {s for s in slug_tokens if s}
            if len(uniq_slugs) == 1:
                inferred_bundle = next(iter(uniq_slugs))

        raw_bundle = params.get("bundle_id")
        mapped = _mapped_bundle_id(raw_bundle)

        # Resolution order: argv-derived slug overrides a wrong mapped value,
        # mapped values not in discovered bundles fall back to the session default,
        # otherwise first available of mapped / default / inferred.
        if inferred_bundle and mapped and inferred_bundle != mapped:
            bundle_id = inferred_bundle
        elif mapped and discovered_bundles and mapped not in discovered_bundles and default_bundle_id:
            bundle_id = default_bundle_id
        else:
            bundle_id = mapped or default_bundle_id or inferred_bundle

        if not isinstance(argv, list):
            if bundle_id:
                params["bundle_id"] = bundle_id
            continue

        if bundle_id:
            bundle_prefix = f"{plugin_root}/{bundle_id}/"
            for idx, arg in enumerate(argv):
                if not isinstance(arg, str):
                    continue
                token = arg.strip()
                rel_script: Optional[str] = None
                if token.startswith("/work/skills/"):
                    rel_script = token[len("/work/") :]
                elif token.startswith("skills/"):
                    rel_script = token
                elif token.startswith(bundle_prefix):
                    rel_script = token[len(bundle_prefix) :]
                if rel_script:
                    if not (rel_script == "skills" or rel_script.startswith("skills/")):
                        rel_script = f"skills/{rel_script}"
                    argv[idx] = rel_script
            params["argv"] = argv

        if bundle_id:
            params["bundle_id"] = bundle_id


def _apply_discovery_filters(
    schemas: List[Any],
    tool_filter_metadata: Dict[str, Any],
    motet: Any,
) -> List[Any]:
    """
    ADR-0093: Apply exclude/prefix/category filters and add required tools/workflows.
    """
    from ...tools.schema_exporter import ToolSchemaExporter
    from ...workflow import WorkflowRegistry

    tool_registry = getattr(motet, "tools", None)
    all_tools = (tool_registry.list_items() or {}) if tool_registry else {}

    result: List[Any] = list(schemas)
    name_set = {tool_schema_name(s) for s in result}

    exclude_tools = list(tool_filter_metadata.get("exclude_tools") or [])
    exclude_workflows = tool_filter_metadata.get("exclude_workflows")
    no_workflows = bool(tool_filter_metadata.get("no_workflows"))
    prefix_list = list(tool_filter_metadata.get("prefix") or [])
    category_list = list(tool_filter_metadata.get("category") or [])
    required_tools = list(tool_filter_metadata.get("required_tools") or [])
    required_workflows = list(tool_filter_metadata.get("required_workflows") or [])

    exclude_set: set = set(exclude_tools)
    if no_workflows:
        exclude_set.update(n for n in name_set if n.startswith("workflow_"))
    for wf_id in exclude_workflows or []:
        exclude_set.add(f"workflow_{wf_id}")

    result = [s for s in result if tool_schema_name(s) not in exclude_set]
    name_set = {tool_schema_name(s) for s in result}

    if prefix_list:
        result = [s for s in result if any(tool_schema_name(s).startswith(p) for p in prefix_list)]
        name_set = {tool_schema_name(s) for s in result}

    if category_list:
        cats = set(category_list)
        filtered: List[Any] = []
        for s in result:
            n = tool_schema_name(s)
            if n.startswith("workflow_"):
                filtered.append(s)
            elif n in all_tools:
                t = all_tools.get(n)
                if t and getattr(t, "category", "general") in cats:
                    filtered.append(s)
        result = filtered
        name_set = {tool_schema_name(s) for s in result}

    for n in required_tools:
        if n not in name_set and tool_registry:
            schema_exporter = ToolSchemaExporter(
                registry=tool_registry,
                function_discovery_store=getattr(motet, "function_discovery_store", None),
            )
            extra = schema_exporter.export_canonical(preselected_tools=[n], max_tools=1)
            if extra:
                result.append(extra[0])
                name_set.add(n)

    for wf_id in required_workflows:
        wf_name = f"workflow_{wf_id}"
        if wf_name not in name_set:
            all_wf = WorkflowRegistry.export_canonical_schemas() or []
            for s in all_wf:
                if getattr(s, "name", "") == wf_name:
                    result.append(s)
                    name_set.add(wf_name)
                    break

    return result


def ensure_tool_filter_required_tools(
    metadata: Optional[Dict[str, Any]],
    tool_names: List[str],
) -> Dict[str, Any]:
    out = dict(metadata or {})
    required = list(out.get("required_tools") or [])
    for name in tool_names:
        if name not in required:
            required.append(name)
    out["required_tools"] = required
    return out


# Keyword force-include tables for intent-critical core tools (issue #131 / scheduling).
# Keep phrases specific enough to avoid pinning on unrelated everyday language.
_OAUTH_PIN_KEYWORDS = (
    "login",
    "logout",
    "sign in",
    "sign out",
    "connect",
    "disconnect",
    "authenticate",
    "authorize",
    "oauth",
)
_OAUTH_PIN_TOOLS = ("core.oauth_login", "core.oauth_logout", "core.oauth_list")

_EXEC_PIN_KEYWORDS = (
    "run ",
    "execute ",
    "python ",
    "python3 ",
    "script",
    "shell command",
    "bash ",
    "sh ",
    "terminal",
)
_EXEC_PIN_TOOLS = ("core.worker_exec", "core.host_exec")

_TEMPORAL_PIN_KEYWORDS = (
    "current time",
    "what time",
    "wall clock",
    "timezone",
    "time zone",
    "utc",
    "iso 8601",
    "iso8601",
    "datetime",
    "date and time",
    "what day",
    "what date",
    "today's date",
    "todays date",
    "schedule",
    "scheduled",
    "scheduling",
    "cron",
    "timer",
    "delay",
    "delayed",
    "remind",
    "reminder",
    "in a minute",
    "in an hour",
    "seconds from",
    "minutes from",
    "hours from",
    "later today",
    "tomorrow",
)
_TEMPORAL_PIN_TOOLS = (
    "core.current_time",
    "core.schedule_command",
    "core.scheduled_commands_list",
    "core.manage_schedule",
)


_MEMORY_PIN_KEYWORDS = (
    "remember",
    "recall",
    "memorize",
    "note that",
    "keep track of",
    "don't forget",
    "do not forget",
    "make a note",
)
_MEMORY_PIN_TOOLS = ("core.memory_store", "core.memory_recall")

# Separate from the default store/recall pin list. Negated "don't forget"
# contains "forget that" as a substring, so forget pins are skipped then.
_MEMORY_FORGET_PIN_KEYWORDS = (
    "forget that",
    "forget this",
    "please forget",
    "forget I said",
    "forget I told",
)
_MEMORY_FORGET_PIN_TOOLS = ("core.memory_forget",)
_MEMORY_FORGET_NEGATED = ("don't forget", "do not forget")


def _keyword_pinned_tool_names(query: str) -> List[str]:
    """
    Tool names force-included for the query's keyword intent.
    Used by the sticky-shortlist merge: pins are the only non-permanent names
    allowed to enter the frozen meta bag.

    Memory tools are pinned here rather than left to residency: the frozen
    meta bag does not include them, and catalog reachability is via
    tools_search → tool_call (or an explicit pin on intent).
    """
    query_lower = (query or "").lower()
    pinned: List[str] = []
    if any(kw in query_lower for kw in _OAUTH_PIN_KEYWORDS):
        pinned.extend(_OAUTH_PIN_TOOLS)
    if any(kw in query_lower for kw in _EXEC_PIN_KEYWORDS):
        pinned.extend(_EXEC_PIN_TOOLS)
    if any(kw in query_lower for kw in _TEMPORAL_PIN_KEYWORDS):
        pinned.extend(_TEMPORAL_PIN_TOOLS)
    if any(kw in query_lower for kw in _MEMORY_PIN_KEYWORDS):
        pinned.extend(_MEMORY_PIN_TOOLS)
    forget_intent = any(kw in query_lower for kw in _MEMORY_FORGET_PIN_KEYWORDS)
    negated_forget = any(kw in query_lower for kw in _MEMORY_FORGET_NEGATED)
    if forget_intent and not negated_forget:
        pinned.extend(_MEMORY_FORGET_PIN_TOOLS)
    return pinned


def _resolve_tool_schemas_by_name(names: List[str], motet: Any) -> Dict[str, Any]:
    """
    Resolve canonical schemas for tool/workflow names outside this turn's fresh
    discovery results (i.e. sticky shortlist carry-overs). Names that no longer
    resolve (deregistered tools, removed workflows) are silently dropped, which
    is the natural eviction path for stale shortlist entries.
    """
    from ...tools.schema_exporter import ToolSchemaExporter
    from ...workflow import WorkflowRegistry

    resolved: Dict[str, Any] = {}
    if not names:
        return resolved

    workflow_names = {n for n in names if n.startswith("workflow_")}
    if workflow_names:
        for s in WorkflowRegistry.export_canonical_schemas() or []:
            s_name = tool_schema_name(s)
            if s_name in workflow_names:
                resolved[s_name] = s

    tool_names = [n for n in names if n not in workflow_names]
    if tool_names and motet.tools:
        exporter = ToolSchemaExporter(
            registry=motet.tools,
            function_discovery_store=getattr(motet, "function_discovery_store", None),
        )
        for name in tool_names:
            try:
                for s in exporter.export_canonical(preselected_tools=[name], max_tools=1) or []:
                    if tool_schema_name(s) == name:
                        resolved[name] = s
            except Exception as e:
                logger.debug("agentic_loop_sticky_schema_resolve_failed", tool=name, error=str(e))
    return resolved


def merge_sticky_tool_schemas(
    sticky_names: List[str],
    motet: Any,
    max_tools: int,
    tool_filter_metadata: Optional[Dict[str, Any]] = None,
    query: str = "",
) -> List[Any]:
    """
    Build the frozen meta-disclosure shortlist for this turn.

    Membership is sticky + always-sticky meta tools + keyword pins (see
    merge_sticky_tool_names). Schemas are resolved from the registry so
    deregistered sticky entries drop out. ADR-0093 filters / required_tools
    are applied afterward.
    """
    from .tool_shortlist import merge_sticky_tool_names

    final_names = merge_sticky_tool_names(
        sticky_names,
        max_tools,
        pinned_names=_keyword_pinned_tool_names(query),
    )
    resolved = _resolve_tool_schemas_by_name(final_names, motet)

    merged: List[Any] = []
    for name in final_names:
        schema = resolved.get(name)
        if schema is not None:
            merged.append(schema)

    if tool_filter_metadata:
        merged = _apply_discovery_filters(merged, tool_filter_metadata, motet)

    logger.debug(
        "agentic_loop_sticky_shortlist_merged",
        sticky_count=len(sticky_names),
        merged_count=len(merged),
        carried_over=len(resolved),
        dropped_unresolvable=len(final_names) - len(resolved),
    )
    return merged
