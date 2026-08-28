"""
Motet - Meta-Tool Disclosure PoC Fixtures

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-30

Description:
    Standalone PoC fixtures for progressive capability disclosure: a fake Motet
    catalog plus resident meta-tools (tools_search + tool_call). Used by the
    opt-in live harness that asks whether models close the discovery loop when
    target schemas are not in tools[].

    Wire names at the provider boundary use double underscores
    (core__tools_search / core__tool_call), matching Motet MCP sanitization.
    Catalog tool names stay canonical (core.*, mcp.server.tool).

Dependencies:
    - json: serialize observations and parse tool arguments
    - motet.core.types: CanonicalToolSchema

Usage:
    from tests.fixtures.meta_tool_disclosure_poc import (
        META_TOOL_SCHEMAS,
        SYSTEM_PROMPT,
        SCENARIOS,
        dispatch_meta_tool,
    )

Notes:
    - No real registry / agentic loop — fake catalog only.
    - Search returns full JSON schemas inline (design-note piece 2).
    - tool_call validates required fields lightly and echoes schema on error.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from motet.core.types import CanonicalToolSchema

# Wire forms the model sees in tools[] (provider-safe: ^[a-zA-Z0-9_-]+$).
WIRE_TOOLS_SEARCH = "core__tools_search"
WIRE_TOOL_CALL = "core__tool_call"

# Accept either wire or dotted canonical when the model names the meta-tools.
_META_SEARCH_ALIASES = frozenset(
    {WIRE_TOOLS_SEARCH, "core.tools_search", "tools_search"}
)
_META_CALL_ALIASES = frozenset(
    {WIRE_TOOL_CALL, "core.tool_call", "tool_call"}
)


@dataclass(frozen=True)
class FakeCatalogTool:
    """One Motet-shaped tool in the PoC catalog (not resident in tools[])."""

    name: str
    description: str
    json_schema: Dict[str, Any]
    keywords: Tuple[str, ...] = ()


FAKE_CATALOG: Tuple[FakeCatalogTool, ...] = (
    FakeCatalogTool(
        name="core.schedule_command",
        description=(
            "Schedule a reminder or future Motet command. Use for 'remind me', "
            "'schedule', 'tomorrow at 9am', recurring reminders."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Reminder text or command summary.",
                },
                "when": {
                    "type": "string",
                    "description": "When to fire (ISO-8601 or natural language).",
                },
            },
            "required": ["message", "when"],
        },
        keywords=(
            "schedule",
            "reminder",
            "remind",
            "tomorrow",
            "alarm",
            "later",
        ),
    ),
    FakeCatalogTool(
        name="mcp.google_workspace.list_calendars",
        description=(
            "List Google calendars available to the user. Use before reading "
            "events when the calendar id is unknown."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Max calendars to return.",
                },
            },
            "required": [],
        },
        keywords=("calendar", "calendars", "google", "workspace", "list"),
    ),
    FakeCatalogTool(
        name="mcp.google_workspace.list_events",
        description=(
            "List upcoming events on a Google calendar. Use for 'what's on my "
            "calendar', agenda, meetings today/tomorrow."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "calendar_id": {
                    "type": "string",
                    "description": "Calendar id (use 'primary' if unknown).",
                },
                "time_min": {
                    "type": "string",
                    "description": "ISO-8601 lower bound for event start.",
                },
                "time_max": {
                    "type": "string",
                    "description": "ISO-8601 upper bound for event start.",
                },
            },
            "required": ["calendar_id"],
        },
        keywords=(
            "calendar",
            "events",
            "agenda",
            "meeting",
            "schedule",
            "tomorrow",
            "today",
        ),
    ),
    FakeCatalogTool(
        name="core.memory_store",
        description=(
            "Store a fact or note in Motet persistent memory for later recall."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Text to remember.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags.",
                },
            },
            "required": ["content"],
        },
        keywords=("memory", "remember", "store", "note", "recall"),
    ),
    FakeCatalogTool(
        name="core.web_search",
        description="Search the public web. Distractor — not needed for PoC need-* prompts.",
        json_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
            },
            "required": ["query"],
        },
        keywords=("web", "search", "internet", "google"),
    ),
    FakeCatalogTool(
        name="core.math_eval",
        description="Evaluate a math expression. Distractor for coding/idle prompts.",
        json_schema={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Arithmetic expression.",
                },
            },
            "required": ["expression"],
        },
        keywords=("math", "calculate", "eval", "arithmetic"),
    ),
)

_CATALOG_BY_NAME: Dict[str, FakeCatalogTool] = {t.name: t for t in FAKE_CATALOG}


SYSTEM_PROMPT = """You are a Motet capability backend assistant for this PoC.

Motet hosts a live, runtime-dynamic catalog of server-side capabilities
(first-party tools, hosted MCP integrations, workflows, memory, and more).
This message is an awareness index only — it does not list tool names or
JSON schemas. What is available depends on the tenant and connected servers;
do not assume a fixed set.

To use a Motet capability:
1. Call core__tools_search with a short intent query to get ranked tools and
   their full JSON schemas in the tool result.
2. Call core__tool_call with the canonical tool_name and parameters matching
   that schema.

Rules:
- Only call tools that appear in this turn's tool list (core__tools_search and
  core__tool_call).
- Never invent Motet or catalog tool names; discover them via search first.
- Prefer Motet tools when the user needs server-side work the client cannot do.
- If the user question needs no Motet capability, answer directly without tools.
"""


META_TOOL_SCHEMAS: List[CanonicalToolSchema] = [
    CanonicalToolSchema(
        name=WIRE_TOOLS_SEARCH,
        description=(
            "Search the Motet tool catalog by intent. Returns ranked matches "
            "with full JSON schemas so you can call them via core__tool_call. "
            "Canonical name: core.tools_search."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language intent or keywords.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max matches (default 5).",
                },
            },
            "required": ["query"],
        },
    ),
    CanonicalToolSchema(
        name=WIRE_TOOL_CALL,
        description=(
            "Execute any authorized Motet catalog tool by canonical name after "
            "discovering it with core__tools_search. Canonical name: core.tool_call."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": (
                        "Canonical catalog tool name, e.g. core.schedule_command "
                        "or mcp.google_workspace.list_events."
                    ),
                },
                "parameters": {
                    "type": "object",
                    "description": "Arguments matching the tool's JSON schema.",
                },
            },
            "required": ["tool_name", "parameters"],
        },
    ),
]


@dataclass
class Scenario:
    """One PoC prompt and scoring rules."""

    scenario_id: str
    user_prompt: str
    # If non-empty, a successful tool_call must target one of these catalog names.
    required_tool_names: Set[str] = field(default_factory=set)
    # If True, any successful tool_call is a failure (idle).
    forbid_tool_call: bool = False


SCENARIOS: Tuple[Scenario, ...] = (
    Scenario(
        scenario_id="need_schedule",
        user_prompt=(
            "Please schedule a reminder for tomorrow at 9am to call Mom. "
            "Use Motet scheduling — do not just describe how you would do it."
        ),
        required_tool_names={"core.schedule_command"},
    ),
    Scenario(
        scenario_id="need_calendar",
        user_prompt=(
            "What's on my Google calendar tomorrow? Look it up with Motet's "
            "calendar integration — do not invent events."
        ),
        required_tool_names={
            "mcp.google_workspace.list_events",
            "mcp.google_workspace.list_calendars",
        },
    ),
    Scenario(
        scenario_id="idle",
        user_prompt=(
            "Explain what a binary search tree is in two short sentences. "
            "No tools needed."
        ),
        forbid_tool_call=True,
    ),
)


def _tokenize_query(query: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9_]+", (query or "").lower()) if t]


def search_catalog(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Rank fake catalog tools by keyword overlap; always include schemas."""
    tokens = set(_tokenize_query(query))
    scored: List[Tuple[int, FakeCatalogTool]] = []
    for tool in FAKE_CATALOG:
        hay = set(tool.keywords) | set(_tokenize_query(tool.name)) | set(
            _tokenize_query(tool.description)
        )
        score = len(tokens & hay)
        if score == 0 and tokens:
            # Soft substring fallback on name/description.
            q = (query or "").lower()
            if q and (q in tool.name.lower() or q in tool.description.lower()):
                score = 1
        if score > 0 or not tokens:
            scored.append((score, tool))
    scored.sort(key=lambda pair: (-pair[0], pair[1].name))
    if not tokens:
        # Empty query: return a small head of the catalog.
        ordered = list(FAKE_CATALOG)[: max(1, limit)]
    else:
        ordered = [t for s, t in scored if s > 0][: max(1, limit)]
        if not ordered:
            # No keyword hits — return top catalog slice so the model can still recover.
            ordered = list(FAKE_CATALOG)[: max(1, limit)]
    return [
        {
            "name": t.name,
            "description": t.description,
            "json_schema": t.json_schema,
            "canonical_name": t.name,
        }
        for t in ordered
    ]


def _normalize_catalog_name(name: str) -> str:
    n = (name or "").strip()
    if n in _CATALOG_BY_NAME:
        return n
    # Wire → canonical for mcp / core dotted names.
    if n.startswith("mcp__"):
        parts = n.split("__")
        if len(parts) >= 3:
            return f"{parts[0]}.{parts[1]}.{'_'.join(parts[2:])}"
    if n.startswith("core__"):
        return "core." + n[len("core__") :]
    return n


def validate_and_call(tool_name: str, parameters: Any) -> Dict[str, Any]:
    """Validate parameters against the fake catalog and return a fake success."""
    canonical = _normalize_catalog_name(tool_name)
    tool = _CATALOG_BY_NAME.get(canonical)
    if tool is None:
        return {
            "ok": False,
            "error": "unknown_tool",
            "message": f"Unknown tool {tool_name!r}. Search with core__tools_search first.",
            "available_tools": [t.name for t in FAKE_CATALOG],
        }
    if not isinstance(parameters, dict):
        return {
            "ok": False,
            "error": "bad_parameters",
            "message": "parameters must be a JSON object.",
            "expected_schema": tool.json_schema,
        }
    required = list(tool.json_schema.get("required") or [])
    missing = [k for k in required if k not in parameters or parameters[k] in (None, "")]
    if missing:
        return {
            "ok": False,
            "error": "validation_error",
            "message": f"Missing required fields: {missing}",
            "expected_schema": tool.json_schema,
        }
    return {
        "ok": True,
        "tool_name": canonical,
        "parameters": parameters,
        "result": {
            "status": "ok",
            "note": f"PoC fake execution of {canonical}",
        },
    }


def dispatch_meta_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    """
    Dispatch a meta-tool call; return observation JSON string.

    ``tool_name`` may be wire or dotted form.
    """
    name = (tool_name or "").strip()
    if name in _META_SEARCH_ALIASES:
        query = str(arguments.get("query") or "")
        try:
            limit = int(arguments.get("limit") or 5)
        except (TypeError, ValueError):
            limit = 5
        payload = {
            "ok": True,
            "matches": search_catalog(query, limit=limit),
            "hint": "Call core__tool_call with canonical tool_name and parameters.",
        }
        return json.dumps(payload)
    if name in _META_CALL_ALIASES:
        target = arguments.get("tool_name") or arguments.get("name") or ""
        params = arguments.get("parameters")
        if params is None and "arguments" in arguments:
            params = arguments.get("arguments")
        if params is None:
            # Allow flat args excluding meta keys.
            params = {
                k: v
                for k, v in arguments.items()
                if k not in {"tool_name", "name", "parameters", "arguments"}
            }
        payload = validate_and_call(str(target), params if params is not None else {})
        return json.dumps(payload)
    return json.dumps(
        {
            "ok": False,
            "error": "not_a_meta_tool",
            "message": (
                f"{tool_name!r} is not resident. Use core__tools_search then "
                "core__tool_call."
            ),
        }
    )


def parse_tool_arguments(arguments_json: str, arguments: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    raw = arguments_json or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass
class RoundTrace:
    """One model round's tool activity."""

    tool_names: List[str] = field(default_factory=list)
    search_queries: List[str] = field(default_factory=list)
    tool_call_targets: List[str] = field(default_factory=list)
    successful_calls: List[str] = field(default_factory=list)
    failed_calls: List[str] = field(default_factory=list)


@dataclass
class ScenarioResult:
    case_id: str
    scenario_id: str
    verdict: str  # PASS | FAIL | SKIP
    reason: str = ""
    rounds: int = 0
    searched: bool = False
    tool_calls: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def score_scenario(
    scenario: Scenario,
    *,
    case_id: str,
    traces: Sequence[RoundTrace],
    rounds: int,
) -> ScenarioResult:
    """Apply PoC pass/fail rules to a completed mini-loop."""
    searched = any(t.search_queries or any(n in _META_SEARCH_ALIASES for n in t.tool_names) for t in traces)
    all_targets = [name for t in traces for name in t.tool_call_targets]
    successes = [name for t in traces for name in t.successful_calls]
    failures = [name for t in traces for name in t.failed_calls]
    warnings: List[str] = []

    if scenario.forbid_tool_call:
        if successes or all_targets:
            return ScenarioResult(
                case_id=case_id,
                scenario_id=scenario.scenario_id,
                verdict="FAIL",
                reason="idle_tool_call",
                rounds=rounds,
                searched=searched,
                tool_calls=all_targets,
            )
        if searched:
            warnings.append("idle_searched")
        return ScenarioResult(
            case_id=case_id,
            scenario_id=scenario.scenario_id,
            verdict="PASS",
            reason="idle_clean",
            rounds=rounds,
            searched=searched,
            tool_calls=all_targets,
            warnings=warnings,
        )

    # Need-* scenarios
    if not searched and not successes:
        return ScenarioResult(
            case_id=case_id,
            scenario_id=scenario.scenario_id,
            verdict="FAIL",
            reason="no_search",
            rounds=rounds,
            searched=False,
            tool_calls=all_targets,
        )

    matched = [n for n in successes if n in scenario.required_tool_names]
    if matched:
        if not searched:
            warnings.append("tool_call_without_search")
        return ScenarioResult(
            case_id=case_id,
            scenario_id=scenario.scenario_id,
            verdict="PASS",
            reason="ok",
            rounds=rounds,
            searched=searched,
            tool_calls=all_targets,
            warnings=warnings,
        )

    if all_targets and not successes:
        # Called tool_call but validation failed every time.
        bad_names = [
            n for n in all_targets if _normalize_catalog_name(n) not in _CATALOG_BY_NAME
        ]
        reason = "bad_name" if bad_names else "bad_args"
        return ScenarioResult(
            case_id=case_id,
            scenario_id=scenario.scenario_id,
            verdict="FAIL",
            reason=reason,
            rounds=rounds,
            searched=searched,
            tool_calls=all_targets,
            warnings=warnings,
        )

    if searched and not all_targets:
        return ScenarioResult(
            case_id=case_id,
            scenario_id=scenario.scenario_id,
            verdict="FAIL",
            reason="no_tool_call",
            rounds=rounds,
            searched=True,
            tool_calls=all_targets,
            warnings=warnings,
        )

    if successes and not matched:
        return ScenarioResult(
            case_id=case_id,
            scenario_id=scenario.scenario_id,
            verdict="FAIL",
            reason="wrong_tool",
            rounds=rounds,
            searched=searched,
            tool_calls=all_targets + [f"failures={failures}"],
            warnings=warnings,
        )

    return ScenarioResult(
        case_id=case_id,
        scenario_id=scenario.scenario_id,
        verdict="FAIL",
        reason="no_tool_call",
        rounds=rounds,
        searched=searched,
        tool_calls=all_targets,
        warnings=warnings,
    )


def format_summary_table(results: Sequence[ScenarioResult]) -> str:
    """Compact PASS/FAIL table for pytest -s."""
    if not results:
        return "(no PoC results)"
    headers = ("case", "scenario", "verdict", "reason", "rounds", "searched", "calls")
    rows = [
        (
            r.case_id,
            r.scenario_id,
            r.verdict,
            r.reason,
            str(r.rounds),
            "Y" if r.searched else "N",
            ",".join(r.tool_calls) if r.tool_calls else "-",
        )
        for r in results
    ]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(row: Sequence[str]) -> str:
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    lines = [fmt(headers), "-+-".join("-" * w for w in widths)]
    lines.extend(fmt(row) for row in rows)
    passed = sum(1 for r in results if r.verdict == "PASS")
    failed = sum(1 for r in results if r.verdict == "FAIL")
    skipped = sum(1 for r in results if r.verdict == "SKIP")
    lines.append(f"totals: PASS={passed} FAIL={failed} SKIP={skipped}")
    return "\n".join(lines)
