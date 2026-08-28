"""
Motet - Turn Tool Ownership Classifier

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Shared pre-execution ownership decision for a model tool-call turn
    (GitHub issue #157).

    When any call in the turn names an externally-owned tool, the classifier
    returns ``HANDBACK_ALL``: the whole turn is handed back unexecuted so the wire assistant message declares every call
    and a caller-supplied resume transcript stays provider-valid. For mixed
    turns, Motet-owned calls are executed by ``resume_turn`` when the client's
    observations come back (issue #159 execute-at-resume); ``split_calls_by_
    ownership`` is the shared partition helper for that.

    Both Turn Runtime persist (write-time assert) and the OpenAI-compat
    HOSTED_TOOLS path consume this classifier. The agentic loop emits a
    handback intent via ``loop_intents.calls_require_handback`` so it never
    imports this package; persist still asserts ``HANDBACK_ALL`` before
    writing Redis. The two paths differ only in *how* handback is delivered.

Dependencies:
    - enum / typing: TurnOwnership, call-name sequences

Usage:
    from motet.core.checkpoints.ownership import (
        TurnOwnership,
        classify_turn_ownership,
        split_calls_by_ownership,
    )

    decision = classify_turn_ownership(
        ["read_file", "client_edit"],
        external_names={"client_edit"},
    )
    if decision is TurnOwnership.HANDBACK_ALL:
        ...

Notes:
    - Distinct from ``classify_loop_outcome`` (post-loop finalize/stream gate).
    - Empty ``call_tool_names`` or empty ``external_names`` → EXECUTE (no
      ownership conflict); callers still handle the empty-calls stop case.
    - Does not invent HOSTED_TOOLS checkpoints — only the policy predicate.
"""

from __future__ import annotations

from enum import Enum
from typing import AbstractSet, Dict, Iterable, List, Sequence, Tuple


class TurnOwnership(str, Enum):
    """Ownership decision for one model tool-call turn."""

    EXECUTE = "execute"
    HANDBACK_ALL = "handback_all"


def classify_turn_ownership(
    call_tool_names: Sequence[str],
    *,
    external_names: AbstractSet[str],
) -> TurnOwnership:
    """
    Decide whether Motet may execute this turn's tool calls.

    Returns ``HANDBACK_ALL`` when any call names a tool in ``external_names``;
    otherwise ``EXECUTE``. Mixed Motet + external calls in one turn always
    classify as ``HANDBACK_ALL`` (ADR-0125 deviation 5); the Motet subset is
    executed at resume (#159 execute-at-resume).
    """
    if not call_tool_names or not external_names:
        return TurnOwnership.EXECUTE
    for raw in call_tool_names:
        name = str(raw or "").strip()
        if name and name in external_names:
            return TurnOwnership.HANDBACK_ALL
    return TurnOwnership.EXECUTE


def call_tool_names(calls: Iterable[dict]) -> list[str]:
    """Extract tool names from loop or facade call dicts (``tool_name`` key)."""
    return [str(call.get("tool_name") or "").strip() for call in calls]


def split_calls_by_ownership(
    calls: Sequence[Dict],
    *,
    external_names: AbstractSet[str],
) -> Tuple[List[Dict], List[Dict]]:
    """Partition calls into ``(motet_owned, externally_owned)`` by tool name."""
    motet_owned: List[Dict] = []
    externally_owned: List[Dict] = []
    for call in calls:
        name = str(call.get("tool_name") or "").strip()
        if name and name in external_names:
            externally_owned.append(call)
        else:
            motet_owned.append(call)
    return motet_owned, externally_owned


__all__ = [
    "TurnOwnership",
    "classify_turn_ownership",
    "call_tool_names",
    "split_calls_by_ownership",
]
