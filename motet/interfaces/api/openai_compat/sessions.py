"""
Motet - OpenAI Compatible Session Mapping

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Mapping between OpenAI session primitives and Motet conversations.

    OpenAI clients express continuity three ways: a Conversations API id, a
    ``previous_response_id`` chain, or nothing at all. Motet expresses it with a
    single ``conversation_id`` that scopes memory, transcripts, and
    conversation-scoped artifact RAG. This module resolves the former to the
    latter so multi-turn memory works for standards-speaking clients, and records
    the reverse mapping so a returned response id can continue the conversation.

    Stateless Chat Completions clients such as Cursor send none of the three:
    they resend the full transcript every turn with no session reference. Two
    mechanisms recover continuity for them, in this order.

    **Session banner (preferred).** Agent-mode replies end with a visible footer
    naming the conversation — ``_Motet session `openai-abc123` - tracked...``_.
    The client echoes it back verbatim in the next request's history, so the
    conversation is identified by explicit reference rather than inference. This
    is what separates two chat windows whose transcripts are byte-identical, and
    it survives history edits that would change a hash. Banners are stripped
    from inbound history before the model sees them, so they cost nothing in
    context and cannot be imitated by the model.

    **Transcript fingerprint (fallback).** After each completed turn the facade
    records a hash of the transcript-so-far (request messages plus the assistant
    reply) mapped to the conversation id. The next request's prefix — everything
    up to and including the last assistant message — hashes to the same
    fingerprint and rejoins the conversation. Fingerprints are salted with
    tenant and principal, and claimed atomically on first write, so neither a
    foreign credential nor a coincidentally identical transcript can capture an
    existing conversation.

    Correlation records are scoped to the owning principal and tenant: a response
    id minted for one credential must not resume another credential's
    conversation. Caller-supplied conversation ids are additionally guarded by
    the core conversation-ownership check (issue #139 / §11f):
    ``ensure_conversation_access`` fails a cross-principal id before dispatch so
    streaming responses reject cleanly rather than mid-stream. A banner is a
    caller-supplied id for this purpose and gets the same guard.

Dependencies:
    - motet.core.distributed.redis_manager: centralized Redis access with TTL
    - motet.core.conversations.ownership: core ownership guard (issue #139)

Usage:
    from motet.interfaces.api.openai_compat import sessions

    resolved = await sessions.resolve_conversation(
        req, principal, cfg, messages=messages, infer_from_transcript=True
    )
    await sessions.ensure_conversation_access(resolved.conversation_id, principal)

    # Banner is appended to the reply and stripped from inbound history.
    clean = sessions.strip_session_banners(messages)
    reply += sessions.build_session_banner(resolved.conversation_id)

    await sessions.remember_response(response_id, resolved.conversation_id, principal, cfg)
    await sessions.remember_transcript(messages, result, resolved.conversation_id, principal, cfg)

Notes:
    - conversation and previous_response_id are mutually exclusive per OpenAI
    - An unknown or expired previous_response_id is a 404, not a silent fresh
      conversation: memory quietly stopping would be indistinguishable from success
    - Missing session information yields a fresh conversation id, never a shared one
    - Transcript inference is best-effort: an edited history hashes to a new
      fingerprint and starts a fresh conversation rather than a wrong one, and a
      fingerprint already claimed by another conversation is never repointed
    - remember_transcript must be given the transcript as the client sent it
      (banners intact), because that is what the next request will hash
    - Banner rendering is controlled by openai_compat_session_banner
      (off | first | every) and applies to agent mode only
    - Records expire after openai_compat_session_ttl_seconds
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog

from ....core.conversations.ownership import (
    ConversationAccessDenied,
    require_not_owned_by_other_sync,
)
from ....core.distributed.redis_manager import (
    get_redis_client,
    retrieve_structured_data,
    store_structured_data,
)
from ....core.distributed.tenant_keys import (
    maybe_tenant_key,
    retrieve_structured_data_tenant,
)
from .errors import FacadeError
from .wire import ChatCompletionRequest

logger = structlog.get_logger(__name__)

_REDIS_CLIENT_ID = "openai_compat"
_RESPONSE_KEY_PREFIX = "openai_compat:response:"
_TRANSCRIPT_KEY_PREFIX = "openai_compat:transcript:"

_BANNER_MODES = ("off", "first", "every")

# Rendered as a markdown rule plus one italic line so it reads as a footer in a
# chat UI rather than part of the answer. The conversation id is fenced in
# backticks: that is what the parser keys on, and it also stops a client's
# markdown renderer from mangling the id.
_BANNER_LEAD = "\n\n---\n"
_BANNER_LABEL = "Motet session"
_BANNER_RE = re.compile(
    r"(?:\r?\n)*(?:---[ \t]*(?:\r?\n))?"
    rf"_{_BANNER_LABEL} `(?P<conversation_id>[^`\r\n]+)`[^\r\n]*_[ \t]*\Z"
)


def new_conversation_id() -> str:
    """Mint a Motet conversation id for a fresh facade session."""
    return f"openai-{uuid.uuid4().hex}"


@dataclass(frozen=True)
class ResolvedConversation:
    """A conversation id plus how it was determined.

    The source matters to callers: a freshly minted id means this is the
    opening turn of a conversation, which is what ``banner_mode == "first"``
    keys on.
    """

    conversation_id: str
    source: str

    @property
    def is_new(self) -> bool:
        return self.source == "new"


def _principal_tenant(principal: Any) -> str:
    return str(getattr(principal, "tenant_id", "") or "").strip()


def _response_logical(response_id: str) -> str:
    return f"{_RESPONSE_KEY_PREFIX}{response_id}"


def _response_key(response_id: str, tenant_id: Optional[str] = None) -> str:
    return maybe_tenant_key(tenant_id, _response_logical(response_id))


def _conversation_from_field(value: Any) -> Optional[str]:
    """Extract a conversation id from a string or {'id': ...} object."""
    if not value:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        candidate = value.get("id")
        return str(candidate).strip() if candidate else None
    return None


async def remember_response(
    response_id: str,
    conversation_id: str,
    principal: Any,
    cfg: Any,
) -> None:
    """Record response_id -> conversation_id so the client can chain turns."""
    if not response_id or not conversation_id:
        return
    key = _response_key(response_id, _principal_tenant(principal))
    try:
        await store_structured_data(
            _REDIS_CLIENT_ID,
            key,
            {
                "conversation_id": conversation_id,
                "principal_id": getattr(principal, "id", "") or "",
                "tenant_id": getattr(principal, "tenant_id", "") or "",
            },
            format_type="hash",
        )
        ttl = int(getattr(cfg, "openai_compat_session_ttl_seconds", 604800) or 604800)
        client = get_redis_client(_REDIS_CLIENT_ID)
        await client.expire(key, ttl)
    except Exception as exc:
        # Losing the mapping degrades continuity for the next turn but must not
        # fail a request that already produced a valid answer.
        logger.warning(
            "openai_compat_session_record_failed",
            response_id=response_id,
            error=str(exc),
            exc_info=True,
        )


async def lookup_response_conversation(response_id: str, principal: Any) -> Optional[str]:
    """Resolve a previous response id to its conversation, enforcing ownership."""
    if not response_id:
        return None
    try:
        tid = _principal_tenant(principal)
        if tid:
            record = await retrieve_structured_data_tenant(
                _REDIS_CLIENT_ID, tid, _response_logical(response_id), format_type="hash"
            )
        else:
            record = await retrieve_structured_data(
                _REDIS_CLIENT_ID, _response_logical(response_id), format_type="hash"
            )
    except Exception as exc:
        logger.warning(
            "openai_compat_session_lookup_failed",
            response_id=response_id,
            error=str(exc),
            exc_info=True,
        )
        return None

    if not record:
        return None

    owner = str(record.get("principal_id") or "")
    tenant = str(record.get("tenant_id") or "")
    if owner and owner != (getattr(principal, "id", "") or ""):
        logger.warning(
            "openai_compat_session_owner_mismatch",
            response_id=response_id,
            requester=getattr(principal, "id", None),
        )
        raise FacadeError(
            404,
            f"previous response '{response_id}' not found",
            error_type="not_found_error",
            code="response_not_found",
            param="previous_response_id",
        )
    if tenant and tenant != (getattr(principal, "tenant_id", "") or ""):
        raise FacadeError(
            404,
            f"previous response '{response_id}' not found",
            error_type="not_found_error",
            code="response_not_found",
            param="previous_response_id",
        )
    return str(record.get("conversation_id") or "") or None


def _message_field(message: Any, field: str) -> Any:
    if isinstance(message, dict):
        return message.get(field)
    return getattr(message, field, None)


# ---------------------------------------------------------------------------
# Session banner continuity (ADR-0125 §5d)
# ---------------------------------------------------------------------------


def banner_mode(cfg: Any) -> str:
    """Resolve the configured banner mode, defaulting an unknown value to ``every``.

    An unrecognized setting falls back to the robust option rather than to
    ``off``: a typo should not silently disable continuity.
    """
    raw = str(getattr(cfg, "openai_compat_session_banner", "every") or "").strip().lower()
    return raw if raw in _BANNER_MODES else "every"


def banner_guard_enabled(cfg: Any) -> bool:
    return bool(getattr(cfg, "openai_compat_session_banner_guard", True))


def build_session_banner(conversation_id: str, *, now: Optional[datetime] = None) -> str:
    """Render the footer appended to an assistant reply.

    The timestamp is cosmetic — it tells the user when Motet handled the turn.
    Continuity rides on the conversation id, which is why the id is what the
    parser extracts.
    """
    if not conversation_id:
        return ""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    return f"{_BANNER_LEAD}_{_BANNER_LABEL} `{conversation_id}` - tracked {stamp}_"


def strip_session_banner(content: Any) -> str:
    """Remove a trailing banner from one message's text."""
    text = str(content or "")
    if not text or _BANNER_LABEL not in text:
        return text
    return _BANNER_RE.sub("", text)


def parse_session_banner(messages: List[Any]) -> Optional[str]:
    """Return the conversation id from the most recent assistant banner.

    Scans backwards so the newest banner wins: with ``banner_mode == "every"``
    every assistant turn carries one, and the latest reflects any rebinding
    that happened mid-conversation (for example a resumed turn, ADR-0127).
    """
    for message in reversed(messages or []):
        if str(_message_field(message, "role") or "") != "assistant":
            continue
        match = _BANNER_RE.search(str(_message_field(message, "content") or ""))
        if match:
            return match.group("conversation_id").strip() or None
    return None


def _strip_message_banner(message: Any) -> Any:
    """Return *message* with any trailing banner removed from its text.

    Copies rather than mutates: the caller keeps the original list for
    fingerprinting, which must hash the transcript exactly as the client sent
    it.
    """
    content = str(_message_field(message, "content") or "")
    parts = _message_field(message, "content_parts") or None
    cleaned = strip_session_banner(content)
    cleaned_parts = None
    if parts:
        cleaned_parts = [
            (
                part.model_copy(update={"text": strip_session_banner(part.text)})
                if getattr(part, "type", None) == "text"
                else part
            )
            for part in parts
        ]
    if cleaned == content and cleaned_parts is None:
        return message

    update: Dict[str, Any] = {"content": cleaned}
    if cleaned_parts is not None:
        update["content_parts"] = cleaned_parts
    if isinstance(message, dict):
        return {**message, **update}
    return message.model_copy(update=update)


def strip_session_banners(messages: List[Any]) -> List[Any]:
    """Strip banners from assistant messages before the model sees them.

    The banner is facade bookkeeping addressed to the user and to the next
    request, not conversation content. Leaving it in history would spend tokens
    on it every turn and invite the model to imitate it.
    """
    out: List[Any] = []
    for message in messages or []:
        if str(_message_field(message, "role") or "") == "assistant":
            out.append(_strip_message_banner(message))
        else:
            out.append(message)
    return out


def banner_guard_instruction() -> str:
    """System-prompt line asking the model to preserve banners it rewrites.

    Nothing here can bind the client. It matters because Cursor BYOK sends its
    own history-compaction request back through this endpoint: when the model
    doing the summarizing is ours, this line is the only chance to keep the
    session reference alive through that rewrite.
    """
    return (
        f"If you summarize, compress, or rewrite prior conversation, preserve any "
        f"'{_BANNER_LABEL}' footer line exactly as written, including its "
        f"identifier. It is the session reference for this conversation and "
        f"discarding it loses continuity. Never write one yourself."
    )


# ---------------------------------------------------------------------------
# Transcript-fingerprint continuity (ADR-0125 §5d)
# ---------------------------------------------------------------------------


def _transcript_logical(fingerprint: str) -> str:
    return f"{_TRANSCRIPT_KEY_PREFIX}{fingerprint}"


def _transcript_key(fingerprint: str, tenant_id: Optional[str] = None) -> str:
    return maybe_tenant_key(tenant_id, _transcript_logical(fingerprint))


def _infer_enabled(cfg: Any) -> bool:
    return bool(getattr(cfg, "openai_compat_infer_session", True))


def _message_fingerprint_entry(message: Any) -> List[Any]:
    """Reduce a message (canonical or dict) to its identity-bearing fields.

    Only role, text content, and tool-call ids participate. Tool-call
    arguments and names are excluded so that cosmetic re-serialization by the
    client (key ordering, whitespace) cannot break continuity; the ids alone
    already make a tool-call turn unique.
    """
    tool_call_ids: List[str] = []
    calls = _message_field(message, "tool_calls_canonical") or _message_field(message, "tool_calls") or []
    for call in calls:
        if isinstance(call, dict):
            call_id = call.get("call_id") or call.get("id")
        else:
            call_id = getattr(call, "call_id", None) or getattr(call, "id", None)
        if call_id:
            tool_call_ids.append(str(call_id))
    return [
        str(_message_field(message, "role") or ""),
        str(_message_field(message, "content") or ""),
        str(_message_field(message, "tool_call_id") or ""),
        tool_call_ids,
    ]


def transcript_fingerprint(messages: List[Any], principal: Any) -> str:
    """Hash a transcript prefix, salted by tenant and principal.

    The salt means two credentials replaying byte-identical transcripts get
    disjoint fingerprints, so inference can never join a conversation across
    an ownership boundary even before the stored-record check.
    """
    material = json.dumps(
        {
            "tenant": str(getattr(principal, "tenant_id", "") or ""),
            "principal": str(getattr(principal, "id", "") or ""),
            "messages": [_message_fingerprint_entry(m) for m in messages],
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _assistant_reply_entry(result: Dict[str, Any]) -> Dict[str, Any]:
    """Model the assistant message the client will echo back next turn.

    Mirrors what the wire translation returns to the client: the reply text
    and, for suspended turns (ADR-0127 handback), the handed-back tool-call
    ids. The echoed copy canonicalizes to the same fingerprint entry.
    """
    tool_call_ids: List[Dict[str, str]] = []
    for call in result.get("tool_calls_canonical") or []:
        if isinstance(call, dict):
            call_id = call.get("call_id") or call.get("id")
            if call_id:
                tool_call_ids.append({"id": str(call_id)})
    return {
        "role": "assistant",
        "content": str(result.get("content") or ""),
        "tool_calls": tool_call_ids,
    }


def _transcript_prefix_for_lookup(messages: List[Any]) -> Optional[List[Any]]:
    """Largest prefix of ``messages`` ending in an assistant message.

    Stored fingerprints always end with the assistant reply, so the last
    assistant message in the incoming request marks exactly where the prior
    turn's record stopped. Trailing user, tool, and system messages are the
    new material this turn adds. Any other trailing role means the transcript
    does not look like a continuation, so no lookup is attempted.
    """
    for index in range(len(messages) - 1, -1, -1):
        role = str(_message_field(messages[index], "role") or "")
        if role == "assistant":
            return list(messages[: index + 1])
        if role not in ("user", "tool", "system"):
            return None
    return None


async def remember_transcript(
    messages: List[Any],
    result: Dict[str, Any],
    conversation_id: str,
    principal: Any,
    cfg: Any,
) -> None:
    """Record fingerprint(request messages + assistant reply) -> conversation.

    Called after a successful memory-bearing turn so the next stateless
    request can rejoin this conversation. Failure only degrades continuity
    for the next turn and must not fail a request that already succeeded.
    """
    if not conversation_id or not messages or not _infer_enabled(cfg):
        return
    transcript = list(messages) + [_assistant_reply_entry(result)]
    fingerprint = transcript_fingerprint(transcript, principal)
    key = _transcript_key(fingerprint, _principal_tenant(principal))
    try:
        client = get_redis_client(_REDIS_CLIENT_ID)
        # Claim the fingerprint atomically. Two conversations can hash alike —
        # same principal, same opening question, same reply — and a plain write
        # would repoint the earlier one's mapping at the later conversation,
        # stranding its first turn in an abandoned conversation. First writer
        # wins; the loser keeps its own id and simply gets no inference hit.
        claimed = await client.hsetnx(key, "conversation_id", conversation_id)
        if not claimed:
            existing = await client.hget(key, "conversation_id")
            if isinstance(existing, bytes):
                existing = existing.decode("utf-8", "replace")
            if str(existing or "") != conversation_id:
                logger.info(
                    "openai_compat_transcript_fingerprint_taken",
                    conversation_id=conversation_id,
                    held_by=str(existing or ""),
                    fingerprint=fingerprint[:12],
                )
                return
        await store_structured_data(
            _REDIS_CLIENT_ID,
            key,
            {
                "conversation_id": conversation_id,
                "principal_id": getattr(principal, "id", "") or "",
                "tenant_id": getattr(principal, "tenant_id", "") or "",
            },
            format_type="hash",
        )
        ttl = int(getattr(cfg, "openai_compat_session_ttl_seconds", 604800) or 604800)
        await client.expire(key, ttl)
        logger.debug(
            "openai_compat_transcript_recorded",
            conversation_id=conversation_id,
            fingerprint=fingerprint[:12],
            message_count=len(transcript),
        )
    except Exception as exc:
        logger.warning(
            "openai_compat_transcript_record_failed",
            conversation_id=conversation_id,
            error=str(exc),
            exc_info=True,
        )


async def infer_conversation_from_transcript(
    messages: List[Any],
    principal: Any,
    cfg: Any,
) -> Optional[str]:
    """Best-effort: match this request's transcript prefix to a prior turn.

    Returns the recorded conversation id when the prefix fingerprint matches,
    None otherwise (first turn, edited history, expired record, or the
    feature disabled). Unlike ``previous_response_id``, a miss is not an
    error: the client never claimed continuity, so a fresh conversation is
    the correct fallback.
    """
    if not _infer_enabled(cfg):
        return None
    prefix = _transcript_prefix_for_lookup(messages or [])
    if not prefix:
        return None

    fingerprint = transcript_fingerprint(prefix, principal)
    try:
        tid = _principal_tenant(principal)
        if tid:
            record = await retrieve_structured_data_tenant(
                _REDIS_CLIENT_ID, tid, _transcript_logical(fingerprint), format_type="hash"
            )
        else:
            record = await retrieve_structured_data(
                _REDIS_CLIENT_ID, _transcript_logical(fingerprint), format_type="hash"
            )
    except Exception as exc:
        logger.warning(
            "openai_compat_transcript_lookup_failed",
            error=str(exc),
            exc_info=True,
        )
        return None

    if not record:
        return None

    # The salt already partitions fingerprints by credential; this check is
    # defense in depth against a salt regression or a poisoned record.
    owner = str(record.get("principal_id") or "")
    tenant = str(record.get("tenant_id") or "")
    if owner and owner != (getattr(principal, "id", "") or ""):
        logger.warning(
            "openai_compat_transcript_owner_mismatch",
            requester=getattr(principal, "id", None),
        )
        return None
    if tenant and tenant != (getattr(principal, "tenant_id", "") or ""):
        return None
    return str(record.get("conversation_id") or "") or None


async def resolve_conversation(
    req: ChatCompletionRequest,
    principal: Any,
    cfg: Any,
    *,
    header_conversation_id: Optional[str] = None,
    messages: Optional[List[Any]] = None,
    infer_from_transcript: bool = False,
) -> ResolvedConversation:
    """Resolve the Motet conversation for this request, and say how.

    Precedence: explicit Motet extension, then the OpenAI conversation id, then
    the previous_response_id chain, then a session banner echoed back in the
    transcript, then transcript-fingerprint inference (the last two only when
    the caller opts in for memory-bearing modes), then a freshly minted id. A
    new id is minted rather than reusing a shared default so unrelated clients
    never collide in one conversation's memory.

    The banner outranks the fingerprint because it is an explicit reference
    rather than an inference: it survives history edits that change the hash,
    and it distinguishes two conversations whose transcripts are identical.
    Both are equally subject to the caller's ownership check.
    """
    conversation_field = _conversation_from_field(req.conversation)
    if conversation_field and req.previous_response_id:
        raise FacadeError(
            400,
            "conversation and previous_response_id are mutually exclusive",
            code="invalid_session_reference",
            param="previous_response_id",
        )

    explicit = (req.motet_conversation_id or header_conversation_id or "").strip()
    if explicit:
        return ResolvedConversation(explicit, "explicit")
    if conversation_field:
        return ResolvedConversation(conversation_field, "conversation")
    if req.previous_response_id:
        resolved = await lookup_response_conversation(req.previous_response_id, principal)
        if resolved:
            return ResolvedConversation(resolved, "previous_response")
        # Minting a fresh conversation here would silently drop memory the
        # client believes it is continuing (ADR-0125 §5f: no accept-and-ignore).
        logger.info(
            "openai_compat_previous_response_unmapped",
            previous_response_id=req.previous_response_id,
        )
        raise FacadeError(
            404,
            f"previous response '{req.previous_response_id}' not found",
            error_type="not_found_error",
            code="response_not_found",
            param="previous_response_id",
        )
    if infer_from_transcript and messages:
        banner_id = parse_session_banner(messages)
        if banner_id:
            logger.info(
                "openai_compat_session_resolved_from_banner",
                conversation_id=banner_id,
                message_count=len(messages),
            )
            return ResolvedConversation(banner_id, "banner")
        inferred = await infer_conversation_from_transcript(messages, principal, cfg)
        if inferred:
            logger.info(
                "openai_compat_session_inferred_from_transcript",
                conversation_id=inferred,
                message_count=len(messages),
            )
            return ResolvedConversation(inferred, "transcript")
    return ResolvedConversation(new_conversation_id(), "new")


async def resolve_conversation_id(
    req: ChatCompletionRequest,
    principal: Any,
    cfg: Any,
    *,
    header_conversation_id: Optional[str] = None,
    messages: Optional[List[Any]] = None,
    infer_from_transcript: bool = False,
) -> str:
    """Resolve the Motet conversation id for this request.

    Thin wrapper over ``resolve_conversation`` for callers that do not need to
    know how the id was determined.
    """
    resolved = await resolve_conversation(
        req,
        principal,
        cfg,
        header_conversation_id=header_conversation_id,
        messages=messages,
        infer_from_transcript=infer_from_transcript,
    )
    return resolved.conversation_id


async def ensure_conversation_access(conversation_id: str, principal: Any) -> None:
    """Pre-flight ownership guard for memory-bearing modes (ADR-0125 §11f).

    The authoritative check lives in core: ``agent_turn`` binds ownership on
    first use and rejects cross-principal access. This non-binding guard exists
    only so a cross-principal id fails before dispatch — a streaming response
    cannot change status after headers are sent — matching the boundary guard
    on ``POST /api/v1/chat``. Deliberately does not claim ownership; the agent
    stack binds the effective id.
    """
    cid = (conversation_id or "").strip()
    principal_id = str(getattr(principal, "id", "") or "").strip()
    tenant_id = str(getattr(principal, "tenant_id", "") or "").strip()
    motet_id = str(getattr(principal, "motet_id", "") or "").strip() or "default"
    if not cid or not principal_id or not tenant_id:
        return

    try:
        await asyncio.to_thread(
            require_not_owned_by_other_sync,
            motet_id=motet_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            conversation_id=cid,
        )
    except ConversationAccessDenied:
        # Same non-disclosing shape as an unknown previous_response_id, so a
        # probing client cannot distinguish "exists, not yours" from "absent".
        raise FacadeError(
            404,
            f"conversation '{cid}' not found",
            error_type="not_found_error",
            code="invalid_session_reference",
            param="conversation",
        )


__all__ = [
    "ResolvedConversation",
    "banner_guard_enabled",
    "banner_guard_instruction",
    "banner_mode",
    "build_session_banner",
    "ensure_conversation_access",
    "infer_conversation_from_transcript",
    "lookup_response_conversation",
    "new_conversation_id",
    "parse_session_banner",
    "remember_response",
    "remember_transcript",
    "resolve_conversation",
    "resolve_conversation_id",
    "strip_session_banner",
    "strip_session_banners",
    "transcript_fingerprint",
]
