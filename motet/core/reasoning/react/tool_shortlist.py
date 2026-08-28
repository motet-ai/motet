"""
Motet - Conversation Tool Shortlist Persistence

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Sticky per-conversation tool shortlist for the agentic loop. Provider prompt caches match an
    exact prefix of tools -> system -> messages, and tool schemas are the first
    segment. Membership is therefore a small frozen bag:

    always-sticky meta tools + keyword pins + required_tools carry-over

    Catalog reachability is ``core.tools_search`` → ``core.tool_call`` (schemas
    in the observation tail), not per-turn embedding residency. Keyword pins
    (temporal / oauth / exec / memory) are the only deliberate prefix
    invalidations besides first-time always-sticky admission.

Dependencies:
    - motet.core.distributed.redis_manager: Centralized Redis operations
      (store/retrieve_structured_data_sync per AGENTS.md requirements)
    - structlog: Structured logging

Usage:
    from motet.core.reasoning.react.tool_shortlist import (
        load_tool_shortlist, store_tool_shortlist, merge_sticky_tool_names,
    )

    sticky = load_tool_shortlist(tenant_id=t, motet_id=m, conversation_id=c)
    final = merge_sticky_tool_names(
        sticky, max_tools=8, pinned_names=["core.memory_store"],
    )
    store_tool_shortlist(tenant_id=t, motet_id=m, conversation_id=c, tool_names=final)

Notes:
    - Persistence failures degrade gracefully (empty sticky); they never fail the turn.
    - NON_STICKY_TOOL_NAMES holds names that must never enter the working set.
      It is empty. The filter stays because a list that cleans to empty must
      not be stored, or it would wipe an existing working set.
    - Entries expire after SHORTLIST_TTL_SECONDS of conversation inactivity.
    - max_tools truncates *after* pins are admitted, so it must exceed
      always-sticky (4) plus the largest keyword pin group (4) or intent pins
      are sliced off the tail.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

import structlog

logger = structlog.get_logger(__name__)

_SERVICE = "tool_shortlist"
SHORTLIST_TTL_SECONDS = 6 * 3600

# Names that must never persist into a conversation's working set. Empty since
# ADR-0138 removed the last synthetic per-iteration tool; kept as the seam for
# the next one rather than reintroduced under pressure.
NON_STICKY_TOOL_NAMES: frozenset = frozenset()

# Permanent shortlist members — the model's resident escape hatch:
# - core.help — internal operations router
# - core.tools_search / core.tool_call — progressive disclosure + generic dispatch
# - core.spawn_agents — parallel fan-out (ADR-0138). Left to search it ranked
#   0.045 on "deep research gather sources parallel web search" and a live
#   turn spent 20 serial Playwright iterations instead.
ALWAYS_STICKY_TOOL_NAMES = (
    "core.help",
    "core.tools_search",
    "core.tool_call",
    "core.spawn_agents",
)


def _shortlist_key(tenant_id: Optional[str], motet_id: Optional[str], conversation_id: str) -> str:
    return f"tool_shortlist:{tenant_id or 'global'}:{motet_id or 'default'}:{conversation_id}"


def merge_sticky_tool_names(
    sticky_names: List[str],
    max_tools: int,
    *,
    pinned_names: Sequence[str] = (),
    always_include: Sequence[str] = ALWAYS_STICKY_TOOL_NAMES,
) -> List[str]:
    """
    Build the frozen shortlist for this turn.

    Provider prefix caches are all-or-nothing on the tools segment. Membership
    is therefore frozen except for deliberate admissions:

      1. Sticky names keep their stored order (conversation working set).
      2. ``always_include`` and query ``pinned_names`` are always admitted.
      3. No discovery-drift admissions (catalog is via tools_search → tool_call).
      4. On overflow, evict non-immune sticky entries tail-first.

    Truncation to ``max_tools`` happens *after* pins are admitted.
    """
    seen: set = set()
    sticky: List[str] = []
    for name in sticky_names:
        if name and name not in seen:
            sticky.append(name)
            seen.add(name)

    mandatory: List[str] = []
    for name in (*always_include, *pinned_names):
        if name and name not in seen:
            mandatory.append(name)
            seen.add(name)

    final = sticky + mandatory

    overflow = len(final) - max_tools
    if overflow > 0:
        immune = {n for n in (*always_include, *pinned_names, *mandatory) if n}
        evict: List[str] = [
            n for n in reversed(sticky) if n not in immune
        ][:overflow]
        evict_set = set(evict)
        final = [n for n in final if n not in evict_set]

    return final[:max_tools]


def load_tool_shortlist(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    conversation_id: Optional[str],
) -> List[str]:
    """
    Load the persisted shortlist for a conversation. Returns [] when there is
    no conversation, no stored shortlist, or Redis is unavailable.
    """
    if not conversation_id:
        return []
    try:
        from ...distributed.redis_manager import retrieve_structured_data_sync

        data = retrieve_structured_data_sync(
            _SERVICE,
            _shortlist_key(tenant_id, motet_id, conversation_id),
            format_type="json_string",
        )
        names = (data or {}).get("tools")
        if not isinstance(names, list):
            return []
        return [str(n) for n in names if n]
    except Exception as e:
        logger.warning(
            "tool_shortlist_load_failed",
            conversation_id=conversation_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        return []


def store_tool_shortlist(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    conversation_id: Optional[str],
    tool_names: List[str],
) -> None:
    """
    Persist the shortlist for a conversation. Best-effort: never raises.
    Filters synthetic meta-tools that the loop re-injects every iteration.
    """
    if not conversation_id:
        return
    cleaned = [
        str(n)
        for n in tool_names
        if n and str(n) not in NON_STICKY_TOOL_NAMES
    ]
    # An empty list would overwrite a good working set with nothing; leave the
    # stored shortlist (and its TTL) alone instead.
    if not cleaned:
        return
    try:
        from ...distributed.redis_manager import (
            get_sync_redis_client,
            store_structured_data_sync,
        )

        key = _shortlist_key(tenant_id, motet_id, conversation_id)
        store_structured_data_sync(
            _SERVICE, key, {"tools": cleaned}, format_type="json_string"
        )
        get_sync_redis_client(_SERVICE).expire(key, SHORTLIST_TTL_SECONDS)
    except Exception as e:
        logger.warning(
            "tool_shortlist_store_failed",
            conversation_id=conversation_id,
            tool_count=len(cleaned),
            error=str(e),
            error_type=type(e).__name__,
        )
