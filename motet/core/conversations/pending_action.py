"""
Motet - Pending-Action Confirmation State

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Pending-action confirmation state. When an assistant turn asks the user
    to confirm an action ("Should I send it?"), finalize_turn attaches a
    structured marker under ``metadata["pending_action"]`` on the turn's
    root assistant Message in the canonical transcript. The next turn
    reads the marker to decide whether a short reply ("ok") is a
    confirmation rather than a greeting, and what context to inject into
    the loop.

    Lifecycle is positional: a proposal is pending iff its marker sits on the
    latest root assistant message of the conversation (within a freshness
    window derived from the carrying transcript row's timestamp). Newer turns
    bury it; unconsumed deferrals re-attach it with an incremented, capped
    ``carried_forward`` count.

    Phase 1 markers are heuristic (``source: "heuristic"``, tail-question
    detection) and routing-only: a confirm never triggers prefilled execution — that path requires Phase 2 agent-declared markers with
    complete staged parameters.

Dependencies:
    - motet.core.memory (via motet context): recall_conversation for
      transcript rows carrying the marker
    - motet.core.conversations.transcript_codec: serialization format the
      raw-item reads here must match ("_type"/"message" discriminators)

Usage:
    from motet.core.conversations.pending_action import (
        build_heuristic_marker, classify_confirmation_reply,
        load_pending_action,
    )

    # Writer (finalize_turn / store_turn_transcript):
    marker = build_heuristic_marker("Here's the draft. Should I send it?",
                                    ["mcp.google_workspace.send_gmail_message"])

    # Reader (agent_turn, before conversation_analysis) — one-shot helper
    # that loads the marker, classifies the reply, computes the deferral
    # carry-forward, and builds the analysis routing hint:
    pending = evaluate_pending_action(motet, conversation_id, user_text)
    if pending.marker:
        ...  # inject context, pin tools on confirm, pass pending.carry on

Notes:
    - Freshness window and carry-forward cap are configurable via
      MOTET_PENDING_ACTION_FRESHNESS_SECONDS (default 1800) and
      MOTET_PENDING_ACTION_MAX_CARRY_FORWARD (default 2).
    - All reads are best-effort: any failure returns "nothing pending" so
      routing degrades to normal (no-marker) behavior instead of failing
      the turn.
    - The marker is the single source of truth for pendingness: detection
      happens once at write time (build_heuristic_marker), so readers never
      re-apply text heuristics to old messages.
    - The confirm/decline partition is a closed, enumerated table: ambiguous conversation-closers ("ok thanks", "no thanks")
      map to "other" so the model — not the router — disambiguates them.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, NamedTuple, Optional
from uuid import uuid4

import structlog

logger = structlog.get_logger(__name__)

PENDING_ACTION_METADATA_KEY = "pending_action"

_DEFAULT_FRESHNESS_SECONDS = 1800
_DEFAULT_MAX_CARRY_FORWARD = 2

# Question text stored on heuristic markers is capped so a long final
# paragraph cannot bloat the transcript row.
_QUESTION_TEXT_MAX_CHARS = 300

_PUNCT_RE = re.compile(r"[!.,;:]+")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_ack_text(text: str) -> str:
    """
    Normalize a short user reply for closed-table matching.

    Lowercased, [!.,;:] replaced with spaces, whitespace collapsed. Shared
    by confirm/decline classification here and the greeting allowlist in
    ``trivial_message``, so the two cannot disagree on what "the same
    message" means.
    """
    return _WHITESPACE_RE.sub(" ", _PUNCT_RE.sub(" ", text.strip().lower())).strip()


# Closed reply vocabulary. The affirmative/negative ack groups are the
# source of truth for confirmation: confirm/decline extends them with
# explicit imperatives that are unambiguous but not greetings ("do it"
# is a command, never a pleasantry), and ``trivial_message`` composes
# its allowlist from the same groups. Everything outside these tables —
# including conversation-closers that read as verdicts only in context
# ("ok thanks", "no thanks", "got it") — classifies as "other" and
# flows through the loop with the pending action in context.
AFFIRMATIVE_ACKS = frozenset({
    "yes", "yes please", "ok", "okay", "sure", "yep", "yeah", "yup",
    "k", "kk", "sounds good", "will do", "agreed", "fine",
    "alright", "all right", "ok great", "ok cool", "lgtm", "sgtm",
    "that works", "works for me",
})
NEGATIVE_ACKS = frozenset({"no", "nope", "nah"})
_CONFIRM_IMPERATIVES = frozenset({"do it", "go ahead", "go for it", "please do", "proceed"})
_DECLINE_PHRASES = frozenset({"cancel", "stop", "don't", "do not", "never mind", "nevermind"})

CONFIRM_REPLIES = AFFIRMATIVE_ACKS | _CONFIRM_IMPERATIVES
DECLINE_REPLIES = NEGATIVE_ACKS | _DECLINE_PHRASES


def pending_action_blocks_direct(
    pending_action: Optional[Dict[str, Any]],
) -> bool:
    """A fresh or stale marker means the ack may be a confirmation, not a greeting."""
    return pending_action_block_reason(pending_action) is not None


def pending_action_block_reason(
    pending_action: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Skip-analysis reason when a pending marker blocks the trivial path."""
    status = (pending_action or {}).get("status")
    if status not in ("fresh", "stale"):
        return None
    if status == "stale":
        return "stale_pending_action"
    reply = (pending_action or {}).get("reply")
    if reply == "confirm":
        return "confirm_pending_action"
    if reply == "decline":
        return "decline_pending_action"
    return "ack_to_pending_action"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "pending_action_config_invalid",
            operation="_env_int",
            env_var=name,
            raw_value=raw,
            fallback=default,
        )
        return default


def pending_action_freshness_seconds() -> int:
    """Freshness window: markers older than this never auto-route as fresh."""
    return _env_int("MOTET_PENDING_ACTION_FRESHNESS_SECONDS", _DEFAULT_FRESHNESS_SECONDS)


def pending_action_max_carry_forward() -> int:
    """Carry-forward cap: past this, a deferred proposal must be re-asked."""
    return _env_int("MOTET_PENDING_ACTION_MAX_CARRY_FORWARD", _DEFAULT_MAX_CARRY_FORWARD)


def ends_with_question(text: Optional[str]) -> bool:
    """
    Return True when the final non-empty line of ``text`` contains a "?".

    Only the tail signals a prompt awaiting a reply; a rhetorical question
    mid-message ("What does this mean? It means...") does not. This is the
    Phase 1 heuristic writer's detection proxy (ADR-0121), applied at write
    time only — read-time routing trusts the marker exclusively.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    return "?" in text.strip().splitlines()[-1]


def build_heuristic_marker(
    assistant_response: Optional[str],
    tool_shortlist: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Build a Phase 1 heuristic pending-action marker, or None when the
    response does not end with a question.

    Heuristic markers are routing-only (never eligible for prefilled
    execution): the tail-question proxy fires on proposals and empty prompts
    ("Anything else?") alike, so downstream consumption must not trust them
    to execute without a planning model call.
    """
    if not ends_with_question(assistant_response):
        return None
    assert assistant_response is not None  # ends_with_question guarantees it
    question = assistant_response.strip().splitlines()[-1].strip()
    if len(question) > _QUESTION_TEXT_MAX_CHARS:
        question = question[: _QUESTION_TEXT_MAX_CHARS - 3] + "..."
    marker: Dict[str, Any] = {
        "marker_id": f"pa_{uuid4().hex[:12]}",
        "source": "heuristic",
        "question": question,
        "carried_forward": 0,
    }
    shortlist = sorted({str(t) for t in (tool_shortlist or []) if t})
    if shortlist:
        marker["tool_shortlist"] = shortlist
    return marker


def build_carry_forward_marker(marker: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Return a copy of ``marker`` with ``carried_forward`` incremented, or None
    when the cap is reached (the proposal must then be re-asked, so a
    confirmation can never bind to a proposal buried many turns deep).
    """
    try:
        carried = int(marker.get("carried_forward", 0) or 0)
    except (TypeError, ValueError):
        carried = 0
    if carried >= pending_action_max_carry_forward():
        return None
    return {**marker, "carried_forward": carried + 1}


def classify_confirmation_reply(text: Optional[str]) -> str:
    """
    Classify a user reply against a pending action: "confirm" | "decline" | "other".

    Matching mirrors the trivial-allowlist normalization (lowercase,
    [!.,;:] stripped, whitespace collapsed). Replies containing "?" are
    always "other" ("ok?" seeks confirmation, it does not give one), as is
    anything outside the closed tables — the model resolves those in the
    loop with the pending action injected into context.
    """
    if not isinstance(text, str) or not text.strip():
        return "other"
    if "?" in text:
        return "other"
    normalized = normalize_ack_text(text)
    if normalized in CONFIRM_REPLIES:
        return "confirm"
    if normalized in DECLINE_REPLIES:
        return "decline"
    return "other"


class PendingActionLookup(NamedTuple):
    """Result of reading the latest root assistant message for a marker.

    ``status`` is "fresh" or "stale" when ``marker`` is present, else None.
    The marker is the single source of truth for "something is pending": the
    writer applies the detection heuristic at write time, so a marker-less
    latest assistant message means nothing is pending.
    """

    marker: Optional[Dict[str, Any]]
    status: Optional[str]


def load_pending_action(motet: Any, conversation_id: Optional[str]) -> PendingActionLookup:
    """
    Read the pending-action marker from the latest root assistant message.

    Positional semantics (ADR-0121): only the newest root-turn transcript row
    with an assistant message decides. If that message carries no marker,
    nothing is pending — older markers are buried. Freshness compares the
    carrying row's timestamp against the configured window; stale markers are
    still returned (status "stale") because they disable the trivial skip
    even though they never auto-confirm.

    Best-effort: any failure returns an empty lookup so routing treats the
    turn as having nothing pending rather than failing it.
    """
    empty = PendingActionLookup(None, None)
    try:
        if not conversation_id:
            return empty
        memory = getattr(motet, "memory", None)
        if memory is None or not hasattr(memory, "recall_conversation"):
            return empty

        memories = memory.recall_conversation(
            conversation_id=conversation_id,
            types=["conversation_transcript"],
            limit=25,
        )
        if not memories:
            return empty

        def _sequence_key(m: Any) -> int:
            md = getattr(m, "metadata", {}) or {}
            try:
                return int(md.get("sequence") or 0)
            except (TypeError, ValueError):
                return 0

        for m in sorted(memories, key=_sequence_key, reverse=True):
            md = getattr(m, "metadata", {}) or {}
            if not isinstance(md, dict):
                continue
            if md.get("root_turn") is False:
                # Sub-agent row — the marker is a root-turn concept.
                continue
            items = md.get("items")
            if not isinstance(items, list) or not items:
                continue
            assistant: Optional[Dict[str, Any]] = None
            for raw in reversed(items):
                # Discriminator format from transcript_codec.serialize_transcript_item.
                if (
                    isinstance(raw, dict)
                    and raw.get("_type") == "message"
                    and raw.get("role") == "assistant"
                ):
                    assistant = raw
                    break
            if assistant is None:
                continue

            marker = (assistant.get("metadata") or {}).get(PENDING_ACTION_METADATA_KEY)
            if not isinstance(marker, dict):
                # Latest root assistant message carries no marker: nothing
                # pending (any older marker is positionally consumed).
                return empty

            ts = md.get("timestamp")
            fresh = isinstance(ts, (int, float)) and (
                time.time() - float(ts) <= pending_action_freshness_seconds()
            )
            return PendingActionLookup(dict(marker), "fresh" if fresh else "stale")
        return empty
    except Exception as exc:
        logger.warning(
            "pending_action_load_failed",
            operation="load_pending_action",
            conversation_id=conversation_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return empty


class PendingActionTurnState(NamedTuple):
    """Everything a turn needs from the pending-action read (ADR-0121).

    ``routing_hint`` is the compact dict passed to conversation_analysis
    (``{"status", "reply"}`` when a marker is present, ``{"status": "none"}``
    when not). ``carry`` is the cap-checked marker to re-attach via
    finalize_turn on an unconsumed deferral, or None.
    """

    marker: Optional[Dict[str, Any]]
    status: Optional[str]
    reply: Optional[str]
    carry: Optional[Dict[str, Any]]
    routing_hint: Dict[str, Any]


def evaluate_pending_action(
    motet: Any, conversation_id: Optional[str], user_text: str
) -> PendingActionTurnState:
    """
    One-shot pending-action evaluation for the start of a turn.

    Loads the marker from the latest root assistant message, classifies the
    user's reply against it, computes the deferral carry-forward
    (fresh + "other" replies only — confirm/decline consume the proposal and
    stale proposals must be re-asked, never carried), and builds the routing
    hint for conversation_analysis. Best-effort like the underlying read:
    failures yield an empty state and routing falls open.
    """
    lookup = load_pending_action(motet, conversation_id)
    if lookup.marker is None or lookup.status is None:
        return PendingActionTurnState(None, None, None, None, {"status": "none"})

    reply = classify_confirmation_reply(user_text)
    carry: Optional[Dict[str, Any]] = None
    if lookup.status == "fresh" and reply == "other":
        carry = build_carry_forward_marker(lookup.marker)
    logger.info(
        "pending_action_stale" if lookup.status == "stale" else "pending_action_consumed",
        conversation_id=conversation_id,
        marker_id=lookup.marker.get("marker_id"),
        source=lookup.marker.get("source"),
        reply=reply,
        carried_forward=lookup.marker.get("carried_forward"),
    )
    return PendingActionTurnState(
        lookup.marker,
        lookup.status,
        reply,
        carry,
        {"status": lookup.status, "reply": reply},
    )


def build_pending_action_system_message(
    marker: Dict[str, Any], status: str, reply: str
) -> str:
    """
    Render the context-injection system message for a pending action.

    The marker never enters model input via metadata (renderers use
    content/parts); this explicit system message is how the loop learns about
    the pending proposal — including, for stale markers, the instruction to
    re-confirm before acting (the freshness window governs auto-resume, not
    awareness).
    """
    lines: List[str] = [
        "A pending action from the previous assistant turn awaits the user's decision.",
    ]
    question = str(marker.get("question") or "").strip()
    if question:
        lines.append(f'Question asked: "{question}"')
    proposed = str(marker.get("proposed_action") or "").strip()
    if proposed:
        lines.append(f"Proposed action: {proposed}")
    shortlist = marker.get("tool_shortlist")
    if isinstance(shortlist, list) and shortlist:
        lines.append(
            "Tools used while preparing the proposal: " + ", ".join(str(t) for t in shortlist)
        )

    if status == "stale":
        lines.append(
            "This proposal is stale (asked too long ago). Re-confirm with the "
            "user before performing it; do not execute it without fresh confirmation."
        )
    elif reply == "confirm":
        lines.append(
            "The user's latest reply confirms the proposal. Proceed with the "
            "proposed action now; do not ask for confirmation again."
        )
    elif reply == "decline":
        lines.append(
            "The user's latest reply declines the proposal. Acknowledge briefly "
            "and do not perform the action."
        )
    else:
        lines.append(
            "Interpret the user's latest reply in the context of this pending "
            "proposal — it may confirm, decline, amend, or defer it."
        )
    return "\n".join(lines)


__all__ = [
    "AFFIRMATIVE_ACKS",
    "CONFIRM_REPLIES",
    "DECLINE_REPLIES",
    "NEGATIVE_ACKS",
    "PENDING_ACTION_METADATA_KEY",
    "PendingActionLookup",
    "PendingActionTurnState",
    "build_carry_forward_marker",
    "build_heuristic_marker",
    "build_pending_action_system_message",
    "classify_confirmation_reply",
    "ends_with_question",
    "evaluate_pending_action",
    "load_pending_action",
    "normalize_ack_text",
    "pending_action_block_reason",
    "pending_action_blocks_direct",
    "pending_action_freshness_seconds",
    "pending_action_max_carry_forward",
]
