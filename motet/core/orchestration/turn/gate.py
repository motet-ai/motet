"""
Motet - Turn Gate

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Always-on local turn gate and the forced-mode resolve that sits in front
    of it. Both return ``TurnModeDecision``: ``auto`` (the agent loop) or
    ``no_tools`` (one model call, no tools). Not a hook and not a classifier:
    it calls the shared trivial-message allowlist and pending-action
    override, then honors ``turn_gate_skip_simple``.

    ``resolve_turn_mode`` is the single entry for callers: forced mode wins,
    otherwise the gate. Forced mode is read from ``context["mode"]`` only.
    Accepted values are ``auto``, ``no_tools``, and ``agentic``. Unknown
    values run as ``auto``. ``conversation_analysis`` stays the opt-in
    command for tone / profile / complexity; it uses the same allowlist
    helpers.

Dependencies:
    - motet.core.conversations.trivial_message: ``is_trivial_message``
    - motet.core.conversations.pending_action: ``pending_action_blocks_direct``
    - motet.core.config.Config: ``turn_gate_skip_simple`` opt-out
    - structlog: config-load failure is logged, then the gate stays on

Usage:
    from motet.core.orchestration.turn.gate import (
        normalize_turn_mode, resolve_turn_mode, turn_gate, TurnModeDecision,
    )
    from motet.core.conversations.trivial_message import last_user_message

    decision = resolve_turn_mode(
        context=effective_context,
        message=last_user_message(history),
        pending_action=pending.routing_hint,
    )
    if decision.mode == "no_tools":
        # one model call, no tools; decision.no_tools_reason is "trivial"
        # when the gate took the turn, unset when the caller forced it
        ...

Notes:
    - Forced caller modes (``no_tools``, ``agentic``) are resolved here, not
      inside ``turn_gate``. A forced mode still wins over the gate.
    - Small and local models must not see a tool list on "hello".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional

import structlog

from motet.core.conversations.pending_action import pending_action_blocks_direct
from motet.core.conversations.trivial_message import is_trivial_message

logger = structlog.get_logger(__name__)

TurnMode = Literal["auto", "agentic", "no_tools"]
TurnGateReason = Literal["trivial"]
_VALID_MODES: frozenset[str] = frozenset(("auto", "agentic", "no_tools"))


@dataclass(frozen=True)
class TurnModeDecision:
    """Caller mode after forced-mode remap and the turn gate."""

    mode: TurnMode = "auto"
    no_tools_reason: Optional[TurnGateReason] = None


def _skip_simple_enabled(skip_simple: Optional[bool]) -> bool:
    """Honor the opt-out; default on if config cannot be loaded."""
    if skip_simple is not None:
        return bool(skip_simple)
    try:
        from motet.core.config import Config

        return bool(Config().turn_gate_skip_simple)
    except Exception as exc:
        logger.warning(
            "turn_gate_config_load_failed",
            operation="turn_gate",
            error=str(exc),
            error_type=type(exc).__name__,
            note="falling back to default-on turn_gate_skip_simple",
        )
        return True


def turn_gate(
    *,
    message: Any = None,
    pending_action: Optional[Dict[str, Any]] = None,
    skip_simple: Optional[bool] = None,
) -> TurnModeDecision:
    """Decide ``no_tools`` vs ``auto``. Always on, not a hook.

    Returns ``mode="no_tools"`` with ``no_tools_reason="trivial"`` for an
    allowlisted greeting or ack with no pending proposal, unless
    ``turn_gate_skip_simple`` is off.
    """
    if not _skip_simple_enabled(skip_simple):
        return TurnModeDecision()
    if message is None:
        return TurnModeDecision()
    if not is_trivial_message(message) or pending_action_blocks_direct(
        pending_action
    ):
        return TurnModeDecision()
    return TurnModeDecision(mode="no_tools", no_tools_reason="trivial")


def normalize_turn_mode(raw: Any) -> TurnMode:
    """Accept ``auto`` / ``no_tools`` / ``agentic``. Unknown values are ``auto``."""
    mode = str(raw or "auto").strip().lower() or "auto"
    if mode not in _VALID_MODES:
        return "auto"
    return mode  # type: ignore[return-value]


def _forced_mode_from_context(context: Optional[Dict[str, Any]]) -> TurnMode:
    return normalize_turn_mode((context or {}).get("mode"))


def resolve_turn_mode(
    *,
    context: Optional[Dict[str, Any]] = None,
    message: Any = None,
    pending_action: Optional[Dict[str, Any]] = None,
    skip_simple: Optional[bool] = None,
) -> TurnModeDecision:
    """Resolve the forced mode, then the turn gate. One function, no hop.

    Public caller modes are ``auto`` and ``no_tools``. ``agentic`` skips the
    trivial gate and is otherwise the same loop. Unknown ``mode`` values
    run as ``auto``.
    """
    mode = _forced_mode_from_context(context)
    if mode != "auto":
        return TurnModeDecision(mode=mode)
    return turn_gate(
        message=message,
        pending_action=pending_action,
        skip_simple=skip_simple,
    )
