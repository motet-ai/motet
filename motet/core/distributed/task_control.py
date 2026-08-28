"""
Motet - Task-Level Cooperative Cancel Control

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Sticky Redis cancel control plane. Writes
    ``{tenant}:task:control:{scope_id}`` as source of truth (task id, root
    command id, or workflow_run_id; unprefixed when tenant is not usable —
    issue #228 slice B). Commands carry a small ``cancel_scopes`` list and
    honor with one variadic ``EXISTS``. Parked ``WorkerCommunicator`` waiters
    use per-waiter ``LPUSH``/``BLPOP`` wakes (cancel + command result) with
    Redis hash tags so both keys share a cluster slot — no pub/sub and no
    Celery ``ready()`` poll. Parents read SUCCESS/FAILURE from Motet
    ``cmd:outcome:{command_id}`` after the wake (issue #229).     Gather/map
    fan-in uses ``wait_for_command_outcomes`` over those same wakes
    (issue #242) — leftover unary waits run concurrently via
    ``WorkerExecutor`` (one hash-tagged BLPOP per waiter). Not EventBus
    completion events. Redis probe errors are tri-state (not treated as
    "not cancelled"). Also maintains an ephemeral live task index for
    ``GET /api/v1/tasks/live``.

Dependencies:
    - motet.core.checkpoints.redis_store: JSON blob store/load helpers
    - motet.core.distributed.redis_manager: sync Redis client for wake / EXISTS
    - structlog: Structured logging

Usage:
    from motet.core.distributed.task_control import (
        request_task_cancel,
        is_task_cancelled,
        wait_for_command_outcome,
        wait_for_command_outcomes,
        signal_command_result,
        register_live_task,
        unregister_live_task,
        get_live_task,
        list_live_tasks,
    )

    request_task_cancel(task_id, reason="operator", principal_id="u1")
    waited = wait_for_command_outcome(task_id, celery_task_id, timeout_seconds=60)
    # waited.outcome is "cancelled" | "completed" | "timeout"

Notes:
    - Empty / missing ``task_id`` → cancel checks are no-ops (active / False).
    - Sticky until TTL; do not clear on each honor (children must keep seeing it).
    - Wake is for latency; sticky EXISTS / result-done is authoritative after wake.
    - Honor points (pre-send / dispatch) treat Redis errors as cancelled (fail closed).
    - The wait loop uses tri-state probes: unknown backs off, it does not mean "not cancelled".
    - Cancel fans out via waiter registry (BLPOP is 1:1; shared list would drop waiters).
    - Gather leftover waits are parallel unary BLPOPs, not one cross-slot BLPOP.
    - Authz for HTTP cancel lives at the API boundary (owning principal).
    - Task keys are tenant-prefixed via ``tenant_keys`` helpers. Bind tenant
      for the command lifetime (``bind_task_key_tenant``) or pass ``tenant_id``.
"""

from __future__ import annotations

import os
import time
from contextvars import ContextVar, Token
from typing import Any, Dict, List, Literal, NamedTuple, Optional, Sequence

import structlog

from motet.core.checkpoints.redis_store import load_json_blob, store_json_blob
from motet.core.distributed.tenant_keys import (
    product_key,
    task_control_key,
    task_live_key,
    task_response_stream,
    task_waiters_key,
    tasks_live_index_key,
)

logger = structlog.get_logger(__name__)

_SERVICE = "task_control"
_task_key_tenant: ContextVar[Optional[str]] = ContextVar(
    "motet_task_key_tenant", default=None
)

TASK_CONTROL_TTL_SECONDS = int(
    os.getenv("MOTET_TASK_CONTROL_TTL_SECONDS", str(6 * 3600))
)
TASK_LIVE_TTL_SECONDS = int(
    os.getenv("MOTET_TASK_LIVE_TTL_SECONDS", str(6 * 3600))
)
WAITER_TTL_SECONDS = int(os.getenv("MOTET_TASK_WAITER_TTL_SECONDS", "7200"))
RESULT_DONE_TTL_SECONDS = int(os.getenv("MOTET_TASK_RESULT_DONE_TTL_SECONDS", "300"))
# BLPOP chunk: wake is still instant; this only bounds how often we re-check
# stickies / result-done if a wake is missed.
WAIT_BLPOP_CHUNK_SECONDS = int(os.getenv("MOTET_TASK_WAIT_BLPOP_SECONDS", "15"))
WAIT_BLPOP_ERROR_BACKOFF_INITIAL_SECONDS = 0.05
WAIT_BLPOP_ERROR_BACKOFF_MAX_SECONDS = 5.0

TASK_CANCELLED_CODE = "task_cancelled"

CommandWaitOutcome = Literal["cancelled", "completed", "timeout"]
TaskCancelProbe = Literal["cancelled", "active", "unknown"]
ResultDoneProbe = Literal["done", "pending", "unknown"]


class CommandWaitResult(NamedTuple):
    """Wait-loop result. ``cancelled_scope`` is set only when ``outcome`` is cancelled."""

    outcome: CommandWaitOutcome
    cancelled_scope: Optional[str] = None


def normalize_cancel_scopes(scopes: Optional[Sequence[str]]) -> List[str]:
    """Deduplicate scope ids, preserving order. Empty strings dropped."""
    out: List[str] = []
    seen: set[str] = set()
    for raw in scopes or []:
        sid = (raw or "").strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
    return out


def append_cancel_scope(scopes: Optional[Sequence[str]], scope_id: Optional[str]) -> List[str]:
    """Return scopes with ``scope_id`` appended if non-empty and not already present."""
    out = normalize_cancel_scopes(scopes)
    sid = (scope_id or "").strip()
    if sid and sid not in out:
        out.append(sid)
    return out


def apply_command_cancel_scopes(command: Any) -> None:
    """Fill ``cancel_scopes`` / ``own_cancel_scope`` after ``command_id`` exists.

    Roots push ``command_id`` (and keep ``task_id`` as the outermost scope).
    Nested commands inherit only; they do not push unless a later opt-in
    (e.g. ``workflow_run_id``) calls ``push_own_cancel_scope``.
    """
    ctx = getattr(command, "distributed_context", None)
    if ctx is None:
        return
    task_id = (getattr(ctx, "task_id", None) or "").strip()
    scopes = append_cancel_scope(getattr(ctx, "cancel_scopes", None), task_id)
    parent = (getattr(ctx, "parent_command_id", None) or "").strip()
    own = (getattr(ctx, "own_cancel_scope", None) or "").strip() or None
    command_id = (getattr(command, "command_id", None) or "").strip()
    if not parent and command_id:
        scopes = append_cancel_scope(scopes, command_id)
        own = own or command_id
    ctx.cancel_scopes = scopes
    ctx.own_cancel_scope = own


def push_own_cancel_scope(command: Any, scope_id: Optional[str]) -> None:
    """Opt-in: this command is a cancellable subtree (e.g. workflow run)."""
    ctx = getattr(command, "distributed_context", None)
    sid = (scope_id or "").strip()
    if ctx is None or not sid:
        return
    ctx.cancel_scopes = append_cancel_scope(getattr(ctx, "cancel_scopes", None), sid)
    ctx.own_cancel_scope = sid


def cancel_own_scope_for_command(
    command: Any,
    *,
    reason: Optional[str] = None,
    source: Optional[str] = None,
) -> bool:
    """Cancel the scope this command pushed, if any. Nested leaves are a no-op.

    When the command pushed its own ``command_id`` (roots), also write the
    product ``task_id`` sticky so the live index and operator API stay in sync.
    """
    ctx = getattr(command, "distributed_context", None)
    if ctx is None:
        return False
    scope = (getattr(ctx, "own_cancel_scope", None) or "").strip()
    if not scope:
        return False
    principal_id = getattr(ctx, "principal_id", None)
    tenant_id = getattr(ctx, "tenant_id", None)
    request_scope_cancel(
        scope,
        reason=reason,
        principal_id=principal_id,
        source=source,
        tenant_id=tenant_id,
    )
    command_id = (getattr(command, "command_id", None) or "").strip()
    task_id = (getattr(ctx, "task_id", None) or "").strip()
    if task_id and command_id and scope == command_id and scope != task_id:
        request_scope_cancel(
            task_id,
            reason=reason,
            principal_id=principal_id,
            source=source,
            tenant_id=tenant_id,
        )
    return True


def _normalize_task_id(task_id: Optional[str]) -> str:
    return (task_id or "").strip()


def _normalize_waiter_id(waiter_id: Optional[str]) -> str:
    return (waiter_id or "").strip()


def bind_task_key_tenant(tenant_id: Optional[str]) -> Token:
    """Bind tenant for task-control / stream key helpers (command lifetime)."""
    tid = (tenant_id or "").strip() or None
    return _task_key_tenant.set(tid)


def reset_task_key_tenant(token: Token) -> None:
    """Restore the previous ``bind_task_key_tenant`` value."""
    _task_key_tenant.reset(token)


def current_task_key_tenant() -> Optional[str]:
    """Tenant bound for this command, or None."""
    return _task_key_tenant.get()


def _resolve_tenant(explicit: Optional[str] = None) -> Optional[str]:
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    return _task_key_tenant.get()


def control_key(task_id: str, tenant_id: Optional[str] = None) -> str:
    return task_control_key(_resolve_tenant(tenant_id), task_id)


def waiter_registry_key(task_id: str, tenant_id: Optional[str] = None) -> str:
    return task_waiters_key(_resolve_tenant(tenant_id), task_id)


def cancel_wake_key(waiter_id: str) -> str:
    """Per-waiter cancel wake list (hash-tagged with result key for cluster)."""
    return f"{{{waiter_id}}}:wake:cancel"


def result_wake_key(waiter_id: str) -> str:
    """Per-waiter result wake list (same hash tag as cancel wake)."""
    return f"{{{waiter_id}}}:wake:result"


def result_done_key(waiter_id: str) -> str:
    """Sticky result-done marker (survives BLPOP-before-wait race)."""
    return f"{{{waiter_id}}}:result:done"


def live_meta_key(task_id: str, tenant_id: Optional[str] = None) -> str:
    return task_live_key(_resolve_tenant(tenant_id), task_id)


def _live_locate_key(task_id: str) -> str:
    return product_key(f"task:live:{task_id}")


def _locator_tenant_for_live_task(task_id: str) -> Optional[str]:
    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client

        raw = get_sync_redis_client(_SERVICE).get(_live_locate_key(task_id))
    except Exception:
        return None
    if isinstance(raw, (bytes, bytearray)):
        text = raw.decode("utf-8")
    elif isinstance(raw, str):
        text = raw
    else:
        return None
    text = text.strip()
    if not text or text in ("None", "null"):
        return None
    return text


def live_index_key(*, tenant_id: Optional[str], principal_id: Optional[str]) -> str:
    return tasks_live_index_key(tenant_id, principal_id)


def peek_task_control(
    task_id: Optional[str],
    tenant_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Non-consuming read of sticky task control payload."""
    tid = _normalize_task_id(task_id)
    if not tid:
        return None
    return load_json_blob(
        _SERVICE,
        control_key(tid, tenant_id=tenant_id),
        error_label="task_control",
    )


def probe_scopes_cancelled(
    scopes: Optional[Sequence[str]],
    tenant_id: Optional[str] = None,
) -> TaskCancelProbe:
    """Tri-state check: any control key in ``scopes`` exists → cancelled.

    Key existence is the signal (``request_scope_cancel`` only writes cancel).
    One variadic ``EXISTS``. Redis errors → ``unknown``. Empty scopes → ``active``.
    """
    ids = normalize_cancel_scopes(scopes)
    if not ids:
        return "active"
    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client

        client = get_sync_redis_client(_SERVICE)
        keys = [control_key(sid, tenant_id=tenant_id) for sid in ids]
        if client.exists(*keys):
            return "cancelled"
        return "active"
    except Exception as e:
        logger.warning(
            "task_control_scopes_cancelled_failed",
            scopes=ids,
            error=str(e),
            error_type=type(e).__name__,
        )
        return "unknown"


def probe_task_cancelled(
    task_id: Optional[str],
    tenant_id: Optional[str] = None,
) -> TaskCancelProbe:
    """Tri-state sticky check for a single task/scope id."""
    tid = _normalize_task_id(task_id)
    if not tid:
        return "active"
    return probe_scopes_cancelled([tid], tenant_id=tenant_id)


def is_cancelled(
    scopes: Optional[Sequence[str]],
    tenant_id: Optional[str] = None,
) -> bool:
    """Honor-point helper: refuse work unless every scope is known-active.

    Empty scopes → False. Redis errors → True (fail closed).
    """
    return probe_scopes_cancelled(scopes, tenant_id=tenant_id) != "active"


def is_task_cancelled(
    task_id: Optional[str],
    tenant_id: Optional[str] = None,
) -> bool:
    """Honor-point helper for a single task/scope id (fail closed on Redis errors)."""
    return probe_task_cancelled(task_id, tenant_id=tenant_id) != "active"


def first_cancelled_scope(
    scopes: Optional[Sequence[str]],
    tenant_id: Optional[str] = None,
) -> Optional[str]:
    """Return the first scope whose control key exists, or None."""
    ids = normalize_cancel_scopes(scopes)
    if not ids:
        return None
    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client

        client = get_sync_redis_client(_SERVICE)
        for sid in ids:
            if client.exists(control_key(sid, tenant_id=tenant_id)):
                return sid
    except Exception as e:
        logger.debug(
            "task_control_first_cancelled_scope_failed",
            error=str(e),
        )
    return None


def _decode_member(raw: Any) -> str:
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8")
    return str(raw)


def register_command_waiter(
    task_id: Optional[str],
    waiter_id: str,
    *,
    scopes: Optional[Sequence[str]] = None,
    tenant_id: Optional[str] = None,
) -> None:
    """Register a parked parent under each cancel scope so any of them can wake it."""
    wid = _normalize_waiter_id(waiter_id)
    ids = append_cancel_scope(scopes, task_id)
    if not ids or not wid:
        return
    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client

        client = get_sync_redis_client(_SERVICE)
        for sid in ids:
            reg = waiter_registry_key(sid, tenant_id=tenant_id)
            client.sadd(reg, wid)
            client.expire(reg, WAITER_TTL_SECONDS)
        client.expire(cancel_wake_key(wid), WAITER_TTL_SECONDS)
        client.expire(result_wake_key(wid), WAITER_TTL_SECONDS)
    except Exception as e:
        logger.warning(
            "task_control_register_waiter_failed",
            task_id=task_id,
            scopes=ids,
            waiter_id=wid,
            error=str(e),
            error_type=type(e).__name__,
        )


def unregister_command_waiter(
    task_id: Optional[str],
    waiter_id: str,
    *,
    scopes: Optional[Sequence[str]] = None,
    tenant_id: Optional[str] = None,
) -> None:
    """Drop waiter registration and wake lists (best-effort)."""
    wid = _normalize_waiter_id(waiter_id)
    if not wid:
        return
    ids = append_cancel_scope(scopes, task_id)
    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client

        client = get_sync_redis_client(_SERVICE)
        for sid in ids:
            client.srem(waiter_registry_key(sid, tenant_id=tenant_id), wid)
        client.delete(
            cancel_wake_key(wid), result_wake_key(wid), result_done_key(wid)
        )
    except Exception as e:
        logger.debug(
            "task_control_unregister_waiter_failed",
            task_id=task_id,
            waiter_id=wid,
            error=str(e),
        )


def probe_command_result_done(waiter_id: Optional[str]) -> ResultDoneProbe:
    """Tri-state result-done check. Redis errors → ``unknown``, not pending."""
    wid = _normalize_waiter_id(waiter_id)
    if not wid:
        return "pending"
    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client

        if get_sync_redis_client(_SERVICE).exists(result_done_key(wid)):
            return "done"
        return "pending"
    except Exception as e:
        logger.debug(
            "task_control_result_done_check_failed",
            waiter_id=wid,
            error=str(e),
        )
        return "unknown"


def is_command_result_done(waiter_id: Optional[str]) -> bool:
    """True only when the result-done key is positively present."""
    return probe_command_result_done(waiter_id) == "done"


def signal_command_result(waiter_id: Optional[str]) -> None:
    """Mark result done + LPUSH result wake (call after Motet ``cmd:outcome`` is stored)."""
    wid = _normalize_waiter_id(waiter_id)
    if not wid:
        return
    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client

        client = get_sync_redis_client(_SERVICE)
        done = result_done_key(wid)
        client.set(done, "1", ex=RESULT_DONE_TTL_SECONDS)
        wake = result_wake_key(wid)
        client.lpush(wake, "result")
        client.expire(wake, min(300, WAITER_TTL_SECONDS))
    except Exception as e:
        logger.warning(
            "task_control_signal_result_failed",
            waiter_id=wid,
            error=str(e),
            error_type=type(e).__name__,
        )


def _wake_registered_cancel_waiters(
    task_id: str,
    tenant_id: Optional[str] = None,
) -> int:
    """LPUSH cancel onto each registered waiter's private list. Returns count."""
    from motet.core.distributed.redis_manager import get_sync_redis_client

    client = get_sync_redis_client(_SERVICE)
    try:
        members = client.smembers(waiter_registry_key(task_id, tenant_id=tenant_id)) or set()
    except Exception as e:
        logger.warning(
            "task_control_list_waiters_failed",
            task_id=task_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        return 0

    woken = 0
    for raw in members:
        wid = _decode_member(raw)
        if not wid:
            continue
        try:
            key = cancel_wake_key(wid)
            client.lpush(key, "cancel")
            client.expire(key, min(300, WAITER_TTL_SECONDS))
            woken += 1
        except Exception as e:
            logger.warning(
                "task_control_cancel_wake_lpush_failed",
                task_id=task_id,
                waiter_id=wid,
                error=str(e),
                error_type=type(e).__name__,
            )
    return woken


def _emit_task_cancelled_stream(
    task_id: str,
    *,
    reason: Optional[str],
    principal_id: Optional[str],
    source: Optional[str],
    tenant_id: Optional[str] = None,
) -> None:
    """Best-effort ``task_cancelled`` event on the unified task stream."""
    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client

        client = get_sync_redis_client(_SERVICE)
        stream_key = task_response_stream(_resolve_tenant(tenant_id), task_id)
        fields = {
            "event": "task_cancelled",
            "task_id": task_id,
            "reason": reason or "",
            "principal_id": principal_id or "",
            "source": source or "",
            "ts": str(time.time()),
        }
        client.xadd(stream_key, fields, maxlen=10000)
    except Exception as e:
        logger.debug(
            "task_control_stream_event_failed",
            task_id=task_id,
            error=str(e),
            error_type=type(e).__name__,
        )


def request_scope_cancel(
    scope_id: Optional[str],
    *,
    reason: Optional[str] = None,
    principal_id: Optional[str] = None,
    source: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Write sticky cancel for any scope id + LPUSH registered waiters. Idempotent.

    Same namespace as task cancel (``{tenant}:task:control:{scope_id}``).
    Live-index mark-cancelled and the unified task stream event run only when
    ``scope_id`` is a live orchestration task. The workflow-run index bridge
    always runs (empty index is a no-op) so admin cancel without live meta
    still stops durable runs.
    """
    sid = _normalize_task_id(scope_id)
    if not sid:
        raise ValueError("scope_id is required for request_scope_cancel")

    tenant = _resolve_tenant(tenant_id)
    meta = get_live_task(sid, tenant_id=tenant)
    if not tenant and meta:
        tenant = str(meta.get("tenant_id") or "").strip() or None
    is_live_task = bool(meta)
    existing = peek_task_control(sid, tenant_id=tenant)
    if existing and str(existing.get("action") or "") == "cancel":
        woken = _wake_registered_cancel_waiters(sid, tenant_id=tenant)
        _bridge_task_cancel_to_workflows(
            sid, reason=reason, principal_id=principal_id, source=source
        )
        logger.info(
            "scope_cancel_rewake",
            scope_id=sid,
            waiters_woken=woken,
            source=source,
        )
        return existing

    payload: Dict[str, Any] = {
        "action": "cancel",
        "requested_at": time.time(),
        "requested_by": principal_id,
        "reason": reason,
        "source": source or "api",
    }
    store_json_blob(
        _SERVICE,
        control_key(sid, tenant_id=tenant),
        payload,
        TASK_CONTROL_TTL_SECONDS,
        error_label="task_control",
    )
    woken = _wake_registered_cancel_waiters(sid, tenant_id=tenant)
    if is_live_task:
        _mark_live_task_cancelled(sid, reason=reason, tenant_id=tenant)
        _emit_task_cancelled_stream(
            sid,
            reason=reason,
            principal_id=principal_id,
            source=source,
            tenant_id=tenant,
        )
    _bridge_task_cancel_to_workflows(
        sid, reason=reason, principal_id=principal_id, source=source
    )
    logger.info(
        "scope_cancel_requested",
        scope_id=sid,
        reason=reason,
        principal_id=principal_id,
        source=source,
        waiters_woken=woken,
        product_task=is_live_task,
    )
    return payload


def request_task_cancel(
    task_id: Optional[str],
    *,
    reason: Optional[str] = None,
    principal_id: Optional[str] = None,
    source: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Product task cancel — same writer as ``request_scope_cancel``."""
    tid = _normalize_task_id(task_id)
    if not tid:
        raise ValueError("task_id is required for request_task_cancel")
    return request_scope_cancel(
        tid,
        reason=reason,
        principal_id=principal_id,
        source=source,
        tenant_id=tenant_id,
    )


def _bridge_task_cancel_to_workflows(
    task_id: str,
    *,
    reason: Optional[str],
    principal_id: Optional[str],
    source: Optional[str],
) -> None:
    """Best-effort: cancel workflow runs indexed under this task (ADR-0131)."""
    try:
        from motet.core.workflow.checkpoint import (
            list_workflow_runs_for_task,
            request_workflow_run_control,
        )

        runs = list_workflow_runs_for_task(task_id)
    except Exception as e:
        logger.warning(
            "task_cancel_workflow_bridge_list_failed",
            task_id=task_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        return

    bridge_reason = reason or "task_cancel"
    for ref in runs:
        run_id = (ref.get("workflow_run_id") or "").strip()
        if not run_id:
            continue
        try:
            request_workflow_run_control(
                tenant_id=ref.get("tenant_id") or None,
                motet_id=ref.get("motet_id") or "default",
                workflow_run_id=run_id,
                action="cancel",
                principal_id=principal_id,
                reason=bridge_reason,
            )
            logger.info(
                "task_cancel_workflow_bridged",
                task_id=task_id,
                workflow_run_id=run_id,
                source=source,
            )
        except Exception as e:
            logger.info(
                "task_cancel_workflow_bridge_skipped",
                task_id=task_id,
                workflow_run_id=run_id,
                error=str(e),
                error_type=type(e).__name__,
            )


def _cooperative_sleep(seconds: float) -> None:
    try:
        from motet.core.workers.concurrency_primitives import worker_sleep

        worker_sleep(seconds)
    except Exception:
        time.sleep(seconds)


def _blpop_timeout_seconds(
    remaining: float,
    *,
    next_ready_in: Optional[float],
    chunk: int,
) -> int:
    """Whole-second BLPOP timeout; never 0 (redis-py treats 0 as block forever)."""
    cap = min(float(max(1, chunk)), remaining)
    if next_ready_in is not None:
        cap = min(cap, max(next_ready_in, 1.0))
    return max(1, int(cap))


def _wait_is_cancelled(scopes: Sequence[str]) -> Optional[bool]:
    """True = cancelled, False = not cancelled, None = probe failed (unknown)."""
    probe = probe_scopes_cancelled(scopes)
    if probe == "cancelled":
        return True
    if probe == "unknown":
        return None
    return False


def wait_for_command_outcome(
    task_id: Optional[str],
    waiter_id: str,
    *,
    timeout_seconds: float,
    cancel_scopes: Optional[Sequence[str]] = None,
    workflow_run_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    motet_id: Optional[str] = None,
) -> CommandWaitResult:
    """Block on BLPOP cancel/result wakes until outcome or timeout.

    Registers the waiter under every ``cancel_scopes`` id (and ``task_id``)
    so any of those scopes can wake it. ``workflow_run_id`` is still accepted
    and folded into scopes for older call sites.

    Redis errors are tri-state: unknown backs off instead of reading as
    "not cancelled" / "not done". The parent then loads the envelope from
    Motet ``cmd:outcome:{command_id}`` (issue #229) — this function only
    reports completed / cancelled / timeout.

    When cancelled, ``cancelled_scope`` is the first sticky scope so callers
    do not re-probe Redis to choose ``task_cancelled`` vs ``workflow_cancelled``.
    """
    del motet_id  # honor path is scope EXISTS; kept for call-site compat
    tid = _normalize_task_id(task_id)
    wid = _normalize_waiter_id(waiter_id)
    if not wid:
        return CommandWaitResult("timeout")
    tenant_token = bind_task_key_tenant(tenant_id) if (tenant_id or "").strip() else None

    scopes = append_cancel_scope(cancel_scopes, tid)
    scopes = append_cancel_scope(scopes, workflow_run_id)
    chunk = max(1, WAIT_BLPOP_CHUNK_SECONDS)
    backoff = WAIT_BLPOP_ERROR_BACKOFF_INITIAL_SECONDS

    from motet.core.distributed.redis_manager import get_sync_redis_client

    client = get_sync_redis_client(_SERVICE)
    cancel_key = cancel_wake_key(wid)
    result_key = result_wake_key(wid)
    deadline = time.time() + max(0.0, float(timeout_seconds))

    register_command_waiter(tid or None, wid, scopes=scopes)

    def _cancel_state() -> Optional[bool]:
        return _wait_is_cancelled(scopes)

    def _cancelled() -> CommandWaitResult:
        return CommandWaitResult("cancelled", first_cancelled_scope(scopes))

    def _completed() -> CommandWaitResult:
        return CommandWaitResult("completed")

    def _timeout() -> CommandWaitResult:
        return CommandWaitResult("timeout")

    try:
        cancel_state = _cancel_state()
        if cancel_state is True:
            return _cancelled()
        result_probe = probe_command_result_done(wid)
        if result_probe == "done":
            return _completed()

        while time.time() < deadline:
            cancel_state = _cancel_state()
            if cancel_state is True:
                return _cancelled()
            result_probe = probe_command_result_done(wid)
            if result_probe == "done":
                return _completed()

            remaining = deadline - time.time()
            if remaining <= 0:
                break

            if cancel_state is None or result_probe == "unknown":
                logger.warning(
                    "task_control_wait_probe_unknown",
                    task_id=tid,
                    waiter_id=wid,
                    cancel_unknown=cancel_state is None,
                    result_unknown=result_probe == "unknown",
                    backoff_seconds=backoff,
                )
                _cooperative_sleep(backoff)
                backoff = min(
                    backoff * 2.0, WAIT_BLPOP_ERROR_BACKOFF_MAX_SECONDS
                )
                continue

            blpop_timeout = _blpop_timeout_seconds(
                remaining, next_ready_in=None, chunk=chunk
            )
            try:
                item = client.blpop([cancel_key, result_key], timeout=blpop_timeout)
                backoff = WAIT_BLPOP_ERROR_BACKOFF_INITIAL_SECONDS
            except Exception as e:
                logger.warning(
                    "task_control_blpop_failed",
                    task_id=tid,
                    waiter_id=wid,
                    error=str(e),
                    error_type=type(e).__name__,
                    backoff_seconds=backoff,
                )
                _cooperative_sleep(backoff)
                backoff = min(
                    backoff * 2.0, WAIT_BLPOP_ERROR_BACKOFF_MAX_SECONDS
                )
                continue

            if item is None:
                continue

            key_raw, _value = item
            key = _decode_member(key_raw)
            if key == cancel_key or key.endswith(":wake:cancel"):
                woken_cancel = _cancel_state()
                if woken_cancel is True:
                    return _cancelled()
                # Spurious cancel wake without sticky — keep waiting.
                continue
            if key == result_key or key.endswith(":wake:result"):
                return _completed()
            woken_cancel = _cancel_state()
            if woken_cancel is True:
                return _cancelled()
            if probe_command_result_done(wid) == "done":
                return _completed()

        cancel_state = _cancel_state()
        if cancel_state is True:
            return _cancelled()
        if probe_command_result_done(wid) == "done":
            return _completed()
        return _timeout()
    finally:
        unregister_command_waiter(tid or None, wid, scopes=scopes)
        if tenant_token is not None:
            reset_task_key_tenant(tenant_token)


def wait_for_command_outcomes(
    task_id: Optional[str],
    waiter_ids: Sequence[str],
    *,
    timeout_seconds: float,
    cancel_scopes: Optional[Sequence[str]] = None,
    workflow_run_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    motet_id: Optional[str] = None,
) -> Dict[str, CommandWaitResult]:
    """Wait for many child result wakes (gather/map fan-in, issue #242).

    Each waiter hash-tags its own wake keys, so a single ``BLPOP`` across
    children is not cluster-safe. Probe sticky result-done, then run unary
    ``wait_for_command_outcome`` for leftovers in parallel (one BLPOP pair
    per waiter). Shared ``cancel_scopes`` wake every parked leftover.
    Does not call Celery ``AsyncResult``.
    """
    deadline = time.time() + max(0.0, float(timeout_seconds))
    ordered: List[str] = []
    seen: set[str] = set()
    for raw in waiter_ids:
        wid = _normalize_waiter_id(raw)
        if not wid or wid in seen:
            continue
        seen.add(wid)
        ordered.append(wid)

    results: Dict[str, CommandWaitResult] = {}
    if not ordered:
        return results

    scopes = append_cancel_scope(cancel_scopes, _normalize_task_id(task_id))
    scopes = append_cancel_scope(scopes, workflow_run_id)
    if _wait_is_cancelled(scopes) is True:
        cancelled = CommandWaitResult("cancelled", first_cancelled_scope(scopes))
        return {wid: cancelled for wid in ordered}

    still_waiting: List[str] = []
    for wid in ordered:
        if probe_command_result_done(wid) == "done":
            results[wid] = CommandWaitResult("completed")
        else:
            still_waiting.append(wid)

    def _wait_one(wid: str) -> CommandWaitResult:
        remaining = deadline - time.time()
        if remaining <= 0:
            return CommandWaitResult("timeout")
        return wait_for_command_outcome(
            task_id,
            wid,
            timeout_seconds=remaining,
            cancel_scopes=cancel_scopes,
            workflow_run_id=workflow_run_id,
            tenant_id=tenant_id,
            motet_id=motet_id,
        )

    if len(still_waiting) == 1:
        results[still_waiting[0]] = _wait_one(still_waiting[0])
    elif still_waiting:
        from motet.core.workers.concurrency_primitives import WorkerExecutor

        with WorkerExecutor(max_workers=len(still_waiting)) as executor:
            futures = {
                wid: executor.submit(_wait_one, wid) for wid in still_waiting
            }
            for wid, future in futures.items():
                try:
                    waited = future.result()
                    results[wid] = (
                        waited
                        if isinstance(waited, CommandWaitResult)
                        else CommandWaitResult("timeout")
                    )
                except Exception as wait_error:
                    logger.error(
                        "wait_for_command_outcomes_leftover_failed",
                        task_id=task_id,
                        waiter_id=wid,
                        error=str(wait_error),
                        error_type=type(wait_error).__name__,
                        exc_info=True,
                    )
                    results[wid] = CommandWaitResult("timeout")

    for wid in ordered:
        if wid not in results:
            results[wid] = CommandWaitResult("timeout")
    return results


def build_task_cancelled_response(
    *,
    command_id: str = "",
    command_type: str = "",
    task_id: str = "",
    execution_time_ms: float = 0,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Standard command error envelope with ``details.code=task_cancelled`` (ADR-0029)."""
    return {
        "status": "error",
        "data": None,
        "error": {
            "type": "TaskCancelled",
            "message": reason or "Task cancelled",
            "details": {"code": TASK_CANCELLED_CODE, "task_id": task_id},
            "recoverable": False,
            "retry_recommended": False,
        },
        "metadata": {
            "command_id": command_id,
            "command_type": command_type,
            "task_id": task_id,
            "execution_time_ms": execution_time_ms,
        },
        "warnings": [],
    }


def register_live_task(
    task_id: Optional[str],
    *,
    tenant_id: Optional[str] = None,
    principal_id: Optional[str] = None,
    motet_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    command_type: Optional[str] = None,
    root_command_id: Optional[str] = None,
) -> None:
    """Register or refresh a live task index entry (best-effort)."""
    tid = _normalize_task_id(task_id)
    if not tid:
        return
    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client

        meta = {
            "task_id": tid,
            "tenant_id": tenant_id or "",
            "principal_id": principal_id or "",
            "motet_id": motet_id or "",
            "conversation_id": conversation_id or "",
            "command_type": command_type or "",
            "root_command_id": root_command_id or "",
            "status": "running",
            "started_at": time.time(),
            "updated_at": time.time(),
        }
        store_json_blob(
            _SERVICE,
            live_meta_key(tid, tenant_id=tenant_id),
            meta,
            TASK_LIVE_TTL_SECONDS,
            error_label="task_live",
        )
        client = get_sync_redis_client(_SERVICE)
        idx = live_index_key(tenant_id=tenant_id, principal_id=principal_id)
        client.sadd(idx, tid)
        client.expire(idx, TASK_LIVE_TTL_SECONDS)
        if tenant_id:
            client.set(_live_locate_key(tid), tenant_id, ex=TASK_LIVE_TTL_SECONDS)
    except Exception as e:
        logger.warning(
            "task_live_register_failed",
            task_id=tid,
            error=str(e),
            error_type=type(e).__name__,
        )


def _mark_live_task_cancelled(
    task_id: str,
    *,
    reason: Optional[str],
    tenant_id: Optional[str] = None,
) -> None:
    meta = get_live_task(task_id, tenant_id=tenant_id)
    if not meta:
        return
    meta["status"] = "cancelled"
    meta["updated_at"] = time.time()
    meta["cancel_reason"] = reason or ""
    try:
        store_json_blob(
            _SERVICE,
            live_meta_key(task_id, tenant_id=tenant_id or meta.get("tenant_id")),
            meta,
            TASK_LIVE_TTL_SECONDS,
            error_label="task_live",
        )
    except Exception as e:
        logger.debug(
            "task_live_mark_cancelled_failed",
            task_id=task_id,
            error=str(e),
        )


def unregister_live_task(
    task_id: Optional[str],
    *,
    tenant_id: Optional[str] = None,
    principal_id: Optional[str] = None,
) -> None:
    """Remove a task from the live index (best-effort)."""
    tid = _normalize_task_id(task_id)
    if not tid:
        return
    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client

        client = get_sync_redis_client(_SERVICE)
        meta = get_live_task(tid, tenant_id=tenant_id)
        tenant = tenant_id or (meta or {}).get("tenant_id")
        principal = principal_id or (meta or {}).get("principal_id")
        client.delete(live_meta_key(tid, tenant_id=tenant))
        client.delete(_live_locate_key(tid))
        if tenant is not None or principal is not None:
            client.srem(
                live_index_key(tenant_id=tenant, principal_id=principal),
                tid,
            )
    except Exception as e:
        logger.warning(
            "task_live_unregister_failed",
            task_id=tid,
            error=str(e),
            error_type=type(e).__name__,
        )


def get_live_task(
    task_id: Optional[str],
    tenant_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    tid = _normalize_task_id(task_id)
    if not tid:
        return None
    tenant = _resolve_tenant(tenant_id) or _locator_tenant_for_live_task(tid)
    return load_json_blob(
        _SERVICE, live_meta_key(tid, tenant_id=tenant), error_label="task_live"
    )


def list_live_tasks(
    *,
    tenant_id: Optional[str],
    principal_id: Optional[str],
    conversation_id: Optional[str] = None,
    include_cancelled: bool = False,
) -> List[Dict[str, Any]]:
    """List in-flight tasks for the caller's tenant/principal scope.

    Cancelled rows linger until TTL for observability; omit them by default.
    """
    from motet.core.distributed.redis_manager import get_sync_redis_client

    client = get_sync_redis_client(_SERVICE)
    idx = live_index_key(tenant_id=tenant_id, principal_id=principal_id)
    unknown_idx = live_index_key(tenant_id=tenant_id, principal_id=None)
    try:
        members = set(client.smembers(idx) or set())
        caller_principal = (principal_id or "").strip()
        if caller_principal:
            members |= set(client.smembers(unknown_idx) or set())
    except Exception as e:
        logger.warning(
            "task_live_list_failed",
            tenant_id=tenant_id,
            principal_id=principal_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        return []

    out: List[Dict[str, Any]] = []
    conv_filter = (conversation_id or "").strip()
    stale: List[str] = []
    for raw in members:
        tid = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        meta = get_live_task(tid, tenant_id=tenant_id)
        if not meta:
            stale.append(tid)
            continue
        if conv_filter and str(meta.get("conversation_id") or "") != conv_filter:
            continue
        if (
            not include_cancelled
            and str(meta.get("status") or "").strip().lower() == "cancelled"
        ):
            continue
        out.append(meta)
    if stale:
        try:
            client.srem(idx, *stale)
            if (principal_id or "").strip():
                client.srem(unknown_idx, *stale)
        except Exception:
            pass
    out.sort(key=lambda m: float(m.get("started_at") or 0), reverse=True)
    return out


def live_task_owned_by(
    meta: Dict[str, Any],
    *,
    principal_id: Optional[str],
    tenant_id: Optional[str] = None,
) -> bool:
    """True when the caller may view/cancel this live row.

    Matching principal wins. An empty owner (registration without
    ``principal_id``) is visible to any authenticated caller in the same
    tenant so GET/cancel does not 403 the actual owner. Empty caller is
    never authorized.
    """
    owner = str(meta.get("principal_id") or "").strip()
    caller = (principal_id or "").strip()
    if not caller:
        return False
    if owner:
        return owner == caller
    meta_tenant = str(meta.get("tenant_id") or "").strip()
    caller_tenant = (tenant_id or "").strip()
    return bool(meta_tenant and caller_tenant and meta_tenant == caller_tenant)
