"""
Motet - Tenant-Prefixed Redis Keys

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-27

Description:
    Leading ``{tenant_id}:`` prefix for application Redis keys and pub/sub
    channels so ElastiCache RBAC globs ``~{tenant_id}:*`` / ``&{tenant_id}:*``
    match. Writers store the collapsed logical shape for every tenant-scoped
    application family (issue #218): no inner tenant and no ``imf:``
    (``{tenant}:mem:…``, ``{tenant}:conv:…``, ``{tenant}:vault:…``,
    ``{tenant}:tenant:meta``, …). Readers do not dual-read leftover
    ``{tenant}:imf:…`` or unprefixed ``imf:mem:…`` keys.
    Shared control-plane keys use the ``motet:`` product prefix
    (MCP, function discovery, ``motet:tenant:index``). User workflows
    are tenant-scoped (``{tenant}:user_wf:…``, issue #234).
    Tenant EventBus channels are ``{tenant}:events:channel``
    (issue #233); ``motet:events:channel`` is platform-only. Task streams
    and join keys are ``{tenant}:task:…`` (issue #228 slice B). MCP I/O
    streams are ``[{tenant}:]mcp:[{manager_id}:]mcp-…`` (issue #235);
    ``motet:mcp:`` lifecycle signals and ``{manager_id}:mcp-control`` stay
    shared. Tenant id ``mcp`` is reserved.

    Tenant KEKs (``vault:`` + ``encryption:tenant:{tid}``) store under
    ``{tid}:vault:…``. Platform secrets (API keys, langfuse, system_config)
    stay shared as ``motet:vault:…``. Hash ``tenant_id`` of ``None`` /
    ``"None"`` is not a tenant.

Dependencies:
    - motet.core.distributed.redis_manager: structured-data store/retrieve

Usage:
    from motet.core.distributed.tenant_keys import tenant_key, product_key, vault_index_key

    key = tenant_key("acme", "conv:default:user-1")
    # "acme:conv:default:user-1"
    event_bus_channel("acme")
    # "acme:events:channel"
    event_bus_channel(None)
    # "motet:events:channel"
    task_response_stream("acme", "t-1")
    # "acme:task:t-1:response"
    tenant_key("acme", "vault:credential:encryption:tenant:acme")
    # "acme:vault:credential:encryption:tenant:acme"
    vault_index_key("acme")
    # "acme:vault:index"
    vault_index_key(None)
    # "motet:vault:index"

    command_id_from_cmd_key("motet-global:cmd:meta:abc")  # "abc"
    for key in iter_cmd_keys_sync(redis, kind="meta"):
        ...

Notes:
    - Do not prefix Celery, MCP lifecycle signals, worker readiness, or other
      shared infrastructure keys (Phase 3 / shared control plane).
    - Tenant EventBus pub/sub is ``{tenant}:events:channel`` (issue #233).
      Workers ``PSUBSCRIBE *:events:channel``. SSE subscribes to the caller
      tenant only. Platform events stay on ``motet:events:channel``.
    - Task streams / cancel / live index are ``{tenant}:task:…`` (issue #228
      slice B). Celery and ``_kombu`` stay unprefixed. Unprefixed
      ``task:{id}:response`` keys are not read (ephemeral; in-flight turns drop
      across deploy).
    - MCP I/O streams (issue #235): physical
      ``{tid}:mcp:{manager_id}:mcp-{service}-{visibility}-{scope}-{type}``.
      ``mcp:`` is the family (second segment). GLOBAL / discovery stay
      ``mcp:[{manager_id}:]mcp-…``. Tenant id ``mcp`` is reserved so
      ``~mcp:*`` cannot claim GLOBAL I/O. Leftover unprefixed
      ``{manager}:mcp-…`` tenant streams are drop-on-restart, not rewritten.
      Do not tenant-prefix ``motet:mcp:`` or ``{manager_id}:mcp-control``.
    - UnifiedRedisManager does not auto-prefix; callers that know tenant_id
      use these helpers.
    - FT.SEARCH isolation remains TAG filters; search bypasses
      key-pattern ACL.
    - ``RESERVED_TENANT_IDS`` blocks catalog slugs that collide with shared
      prefixes (``motet``, ``imf``, ``celery``, ``worker``, ``lock``, …)
      and the MCP I/O family (``mcp``). ``motet-global`` is allowed.
      Enforced in ``validate_catalog_id``.
    - ``tenant_acl_username`` / ``tenant_acl_access_string`` are the local
      ACL SETUSER and ElastiCache RBAC templates. Do not use that user as
      the Celery login (Phase 3).
    - Debug/admin command scanners must use cmd_key_scan_patterns /
      iter_cmd_keys_sync. SCAN ``cmd:meta:*`` misses ``{tenant}:cmd:meta:*``.
    - Vault: infer tenant from ``encryption:tenant:{tid}`` in the credential
      id (ignore hash ``tenant_id=None``). Platform vault rows stay shared
      as unprefixed ``motet:vault:…``.
    - ``TENANT_SCOPED_PREFIXES`` / ``SHARED_KEY_PREFIXES`` list live families
      only (``motet:…``, collapsed ``{family}:…``, Celery / ``worker:`` /
      ``lock:``). Classifier rows use those live families only (issue #232).
    - Envelope AAD binds the collapsed logical name (``stable_aad_logical_key``).
      Decrypt retries prior physical key names
      (``payload_aad_key_candidates``), including ``imf:`` names.
    - Token and vault lookups with no tenant use a ``motet:`` locator
      written at create time (``GET`` then the tenant key). Callers that
      have a tenant use ``tenant_key``. Vault retrieve also tries leftover
      physical names from ``vault_read_key_candidates`` (``None:vault:…``,
      unprefixed ``vault:…``, ``imf:vault:…``) so a locator miss still
      opens a row that EXISTS.
    - Vault list reads ``{tid}:vault:index`` / ``motet:vault:index``
      (SET of credential ids). Store and delete update that SET.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Union

# Shared / infrastructure keys that must NOT receive a tenant prefix.
# Live Motet families write ``motet:``. ``LEGACY_PRODUCT_PREFIX`` is reserved
# as a tenant-id slug and for envelope AAD historical names only.
PRODUCT_PREFIX = "motet:"
LEGACY_PRODUCT_PREFIX = "imf:"

SHARED_KEY_PREFIXES: Tuple[str, ...] = (
    "celery",
    "_kombu",
    "unacked",
    "healthcheck:",
    "motet:mcp:",
    "motet:manager_status:",
    "motet:events:",
    "motet:function_discovery",
    "motet:fd:",
    "motet:surface:",
    "motet:tenant:index",
    "motet:ratelimit:",
    "motet:trace:",
    "motet:vault:",
    "motet:auth:",
    "edge_device:wg_peer_ip_counter",
    "worker:",
    "manager:status:",
    "manager:registered",
    "lock:",
)

# Tenant-scoped application families. Writers store ``{tenant_id}:{logical}``.
# Platform vault rows under ``motet:vault:`` stay unprefixed (no usable tenant).
TENANT_SCOPED_PREFIXES: Tuple[str, ...] = (
    "mem:",
    "memvec:",
    "openai_compat:",
    "model_profiles:",
    "tenant:meta:",
    "tenant:motet:",
    "auth:refresh_token:",
    "auth:service_account:",
    "vault:",
    "cmd:data:",
    "cmd:result:",
    "cmd:outcome:",
    "cmd:meta:",
    "conversation:",
    "turn_checkpoint:",
    "workflow_checkpoint:",
    "workflow_control:",
    "art:",
    "idx:art:",
    "meta:art:",
    "budget:",
    "cost:",
    "events:",
    "task:",
    "tasks:live:",
    "workspace:container:",
    "artifact_chunk:",
    "edge_device:",
    "user_wf:",
    "mcp:",
)

# Historical AAD names whose leftover logical is ``imf:{collapsed}``.
_STRIP_LEGACY_COLLAPSED_PREFIXES: Tuple[str, ...] = (
    "openai_compat:",
    "auth:refresh_token:",
    "auth:service_account:",
    "vault:",
)

# Global catalogs (``motet:tenant:index``, WG IP counter) stay in
# SHARED_KEY_PREFIXES. User workflows are tenant-scoped (issue #234).
# ``oauth:tokens:`` is a vault credential id, not a Redis key family —
# tenant rows live under ``vault:``; platform under ``motet:vault:``.

def _catalog_slug_segment(prefix: str) -> Optional[str]:
    """First path segment of a Redis prefix when it is a valid catalog slug."""
    first = (prefix or "").split(":", 1)[0].strip().lower()
    if not first or not first[0].isalnum():
        return None
    if not all(ch.isalnum() or ch in "-_" for ch in first):
        return None
    return first


# Tenant ids that would collide with shared control-plane keys (``motet:…``,
# ``imf:…``, Celery, ``worker:``, ``lock:``, …) and with ElastiCache
# globs ``~{tenant_id}:*``. ``motet-global`` is a different first segment.
# ``mcp`` is the MCP I/O family (``{tid}:mcp:…`` / GLOBAL ``mcp:…``).
# Not derived from SHARED_KEY_PREFIXES — those use ``motet:mcp:``.
_EXTRA_RESERVED_TENANT_IDS: frozenset[str] = frozenset({"mcp"})

RESERVED_TENANT_IDS: frozenset[str] = frozenset(
    slug
    for slug in (
        _catalog_slug_segment(PRODUCT_PREFIX),
        _catalog_slug_segment(LEGACY_PRODUCT_PREFIX),
        *(_catalog_slug_segment(prefix) for prefix in SHARED_KEY_PREFIXES),
    )
    if slug
) | _EXTRA_RESERVED_TENANT_IDS

# Hash / leftover values that must never become ``None:imf:vault:…``.
_UNUSABLE_TENANT_IDS = frozenset({"", "none", "null", "nil", "undefined", "n/a"})
_VAULT_KEK_MARKER = "encryption:tenant:"
# Shared platform vault credential ids. Stay unprefixed even if metadata
# hash ``tenant_id`` is a real catalog slug (QA had ``default_tenant``).
PLATFORM_VAULT_CREDENTIAL_IDS: frozenset[str] = frozenset(
    {
        "openai_api_key",
        "anthropic_api_key",
        "github_personal_token",
        "langfuse",
        "system_config",
    }
)

# Logical names used by vault_index_key / event_bus_channel. Physical keys are
# ``{tid}:vault:index`` / ``motet:vault:index`` and
# ``{tid}:events:channel`` / ``motet:events:channel``.
VAULT_INDEX_LOGICAL = "vault:index"
EVENT_BUS_LOGICAL_CHANNEL = "events:channel"
EVENT_BUS_PSUBSCRIBE_PATTERN = "*:events:channel"


def is_reserved_tenant_id(tenant_id: Optional[str]) -> bool:
    """True when *tenant_id* collides with a shared Valkey prefix slug."""
    tid = (tenant_id or "").strip().lower()
    return bool(tid) and tid in RESERVED_TENANT_IDS


def is_usable_tenant_id(tenant_id: Optional[str]) -> bool:
    """True when *tenant_id* is a real catalog id, not a sentinel or mock."""
    tid = (tenant_id or "").strip()
    if not tid:
        return False
    if tid.lower() in _UNUSABLE_TENANT_IDS:
        return False
    if is_reserved_tenant_id(tid):
        return False
    if tid.startswith("<") or "MagicMock" in tid:
        return False
    return True


def _product_suffix(logical_key: str) -> str:
    """Strip a leading ``motet:`` or leftover ``imf:`` product prefix."""
    raw = (logical_key or "").strip()
    if raw.startswith(PRODUCT_PREFIX):
        return raw[len(PRODUCT_PREFIX) :]
    if raw.startswith(LEGACY_PRODUCT_PREFIX):
        return raw[len(LEGACY_PRODUCT_PREFIX) :]
    return raw


def product_key(logical_key: str) -> str:
    """Return the shared write key ``motet:{family}:…``."""
    suffix = _product_suffix(logical_key)
    if not suffix:
        raise ValueError("logical_key is required for a product-prefixed Redis key")
    return f"{PRODUCT_PREFIX}{suffix}"


def vault_index_key(tenant_id: Optional[str] = None) -> str:
    """SET of credential ids: ``{tid}:vault:index`` or ``motet:vault:index``."""
    tid = (tenant_id or "").strip()
    if is_usable_tenant_id(tid):
        return tenant_key(tid, VAULT_INDEX_LOGICAL)
    return product_key(VAULT_INDEX_LOGICAL)


def event_bus_channel(tenant_id: Optional[str] = None) -> str:
    """
    Pub/sub channel for EventBus.

    Usable tenant → ``{tenant_id}:events:channel``. Otherwise the platform
    channel ``motet:events:channel`` (circuit breaker and other no-tenant
    events). Issue #233.
    """
    tid = (tenant_id or "").strip()
    if is_usable_tenant_id(tid):
        return tenant_key(tid, EVENT_BUS_LOGICAL_CHANNEL)
    return product_key(EVENT_BUS_LOGICAL_CHANNEL)


def _maybe_tenant_logical(tenant_id: Optional[str], logical_key: str) -> str:
    """Prefix ``logical_key`` when tenant is usable; otherwise leave unprefixed."""
    tid = (tenant_id or "").strip()
    if is_usable_tenant_id(tid):
        return tenant_key(tid, logical_key)
    return logical_key


def task_response_stream(
    tenant_id: Optional[str],
    task_id: str,
    *,
    loop_id: Optional[str] = None,
) -> str:
    """
    Unified task response stream (ADR-0050 / issue #228 slice B).

    Usable tenant → ``{tenant_id}:task:{task_id}:response``. Otherwise the
    legacy unprefixed ``task:{task_id}:response``. Optional ``loop_id``
    appends ``:{loop_id}`` for nested agent / spawn_agents streams.
    """
    tid = (task_id or "").strip()
    if not tid:
        raise ValueError("task_id is required for a task response stream")
    logical = f"task:{tid}:response"
    extra = (loop_id or "").strip()
    if extra:
        logical = f"{logical}:{extra}"
    return _maybe_tenant_logical(tenant_id, logical)


def task_response_stream_for(context: Any, *, loop_id: Optional[str] = None) -> str:
    """
    Unified task stream from a MotetContext-like object.

    Uses ``stream_key`` when it is a non-empty string. Otherwise builds
    ``task_response_stream(tenant_id, task_id)``. Non-string mocks are ignored
    so unit tests that MagicMock MotetContext still get a real key.
    """
    existing = getattr(context, "stream_key", None)
    if isinstance(existing, str) and existing.strip():
        return existing
    tenant = getattr(context, "tenant_id", None)
    if not isinstance(tenant, str):
        tenant = None
    task_id = getattr(context, "task_id", None)
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id is required for a task response stream")
    return task_response_stream(tenant, task_id, loop_id=loop_id)


def task_control_key(tenant_id: Optional[str], scope_id: str) -> str:
    """Sticky cancel key ``{tenant}:task:control:{scope_id}``."""
    sid = (scope_id or "").strip()
    if not sid:
        raise ValueError("scope_id is required for a task control key")
    return _maybe_tenant_logical(tenant_id, f"task:control:{sid}")


def task_waiters_key(tenant_id: Optional[str], scope_id: str) -> str:
    """Waiter registry ``{tenant}:task:waiters:{scope_id}``."""
    sid = (scope_id or "").strip()
    if not sid:
        raise ValueError("scope_id is required for a task waiters key")
    return _maybe_tenant_logical(tenant_id, f"task:waiters:{sid}")


def task_live_key(tenant_id: Optional[str], task_id: str) -> str:
    """Live task meta ``{tenant}:task:live:{task_id}``."""
    tid = (task_id or "").strip()
    if not tid:
        raise ValueError("task_id is required for a task live key")
    return _maybe_tenant_logical(tenant_id, f"task:live:{tid}")


def is_unified_task_response_stream(stream_key: str) -> bool:
    """True for ``task:{id}:response`` or ``{tenant}:task:{id}:response``."""
    key = (stream_key or "").strip()
    if not key.endswith(":response"):
        return False
    return key.startswith("task:") or ":task:" in key


def tasks_live_index_key(
    tenant_id: Optional[str],
    principal_id: Optional[str] = None,
) -> str:
    """
    Live-task index for a principal.

    Usable tenant → ``{tenant_id}:tasks:live:{principal}`` (no inner tenant).
    Otherwise ``tasks:live:{tenant_or_global}:{principal}``.
    """
    principal = (principal_id or "").strip() or "unknown"
    tid = (tenant_id or "").strip()
    if is_usable_tenant_id(tid):
        return tenant_key(tid, f"tasks:live:{principal}")
    tenant_seg = tid or "global"
    return f"tasks:live:{tenant_seg}:{principal}"


def _vault_family_rest(logical_key: str) -> Optional[str]:
    """Return the suffix after ``motet:vault:``, ``imf:vault:``, or ``vault:``."""
    raw = logical_key or ""
    if raw.startswith("motet:vault:"):
        return raw[len("motet:vault:") :]
    if raw.startswith("imf:vault:"):
        return raw[len("imf:vault:") :]
    if raw.startswith("vault:"):
        return raw[len("vault:") :]
    return None


def is_vault_family_logical_key(logical_key: str) -> bool:
    """True for vault rows under ``imf:vault:`` or collapsed ``vault:``."""
    return _vault_family_rest(logical_key) is not None


def is_vault_kek_logical_key(logical_key: str) -> bool:
    """True for vault rows whose credential id is ``encryption:tenant:…``."""
    rest = _vault_family_rest(logical_key)
    return rest is not None and _VAULT_KEK_MARKER in (logical_key or "")


def is_platform_vault_logical_key(logical_key: str) -> bool:
    """True for shared platform vault rows (API keys, langfuse, system_config)."""
    rest = _vault_family_rest(logical_key)
    if rest is None or is_vault_kek_logical_key(logical_key):
        return False
    _kind, sep, credential_id = rest.partition(":")
    if not sep or not credential_id:
        return False
    return credential_id.split(":", 1)[0] in PLATFORM_VAULT_CREDENTIAL_IDS


def vault_kek_tenant_id(logical_key: str) -> Optional[str]:
    """
    Tenant id embedded in a vault KEK credential id.

    ``imf:vault:metadata:encryption:tenant:demo`` → ``demo``.
    ``vault:metadata:encryption:tenant:demo`` → ``demo``.
    Rejects ``None`` / empty / MagicMock leftovers. Does not read hash fields.
    """
    raw = logical_key or ""
    if not is_vault_kek_logical_key(raw):
        return None
    rest = raw.split(_VAULT_KEK_MARKER, 1)[1]
    tid = rest.split(":")[0].strip()
    return tid if is_usable_tenant_id(tid) else None


def is_collapsed_conv_family(logical_key: str) -> bool:
    """True for ``conv:`` registry/owner/children, not ``conversation:`` transcripts."""
    raw = logical_key or ""
    return raw.startswith("conv:") and not raw.startswith("conversation:")


def _historical_logical_key(logical_key: str, tenant_id: Optional[str] = None) -> str:
    """
    Drop ``imf:`` and the inner tenant from tenant-scoped application keys.

    ``imf:mem:{motet}:{tenant}:{rest}`` → ``mem:{motet}:{rest}``
    ``imf:conv:{motet}:{tenant}:{principal}`` → ``conv:{motet}:{principal}``
    ``imf:conv:owner:{motet}:{tenant}:{cid}`` → ``conv:owner:{motet}:{cid}``
    ``imf:conv:children:{tenant}:{cid}`` → ``conv:children:{cid}``
    ``imf:memvec:{id}`` → ``memvec:{id}``
    ``imf:model_profiles:{tenant}:{name}`` → ``model_profiles:{name}``
    ``imf:tenant:meta:{tenant}`` → ``tenant:meta``
    ``imf:tenant:motet:{tenant}:{motet}`` → ``tenant:motet:{motet}``
    ``imf:tenant:motet:index:{tenant}`` → ``tenant:motet:index``
    ``imf:openai_compat:…`` → ``openai_compat:…``
    ``imf:auth:refresh_token:…`` → ``auth:refresh_token:…``
    ``imf:auth:service_account:…`` → ``auth:service_account:…``
    ``imf:vault:…`` → ``vault:…``

    Shared catalogs (``motet:tenant:index``, MCP, events) are unchanged.
    Already-collapsed keys and ``motet:`` product keys are returned as-is.
    """
    del tenant_id
    raw = (logical_key or "").strip()
    if not raw:
        return raw
    if raw.startswith(PRODUCT_PREFIX):
        return raw
    if raw.startswith("mem:") or raw.startswith("memvec:") or is_collapsed_conv_family(raw):
        return raw
    if raw.startswith("openai_compat:") or raw.startswith("model_profiles:"):
        return raw
    if raw == "tenant:meta" or raw.startswith("tenant:meta:"):
        return "tenant:meta"
    if raw == "tenant:motet:index" or raw.startswith("tenant:motet:index:"):
        return "tenant:motet:index"
    if raw.startswith("tenant:motet:"):
        return raw
    if raw.startswith(("auth:refresh_token:", "auth:service_account:", "vault:")):
        return raw
    if raw.startswith("imf:memvec:"):
        return f"memvec:{raw[len('imf:memvec:'):]}"
    if raw.startswith("imf:conv:owner:"):
        parts = raw.split(":")
        if len(parts) >= 6:
            return f"conv:owner:{parts[3]}:{':'.join(parts[5:])}"
        return raw
    if raw.startswith("imf:conv:children:"):
        parts = raw.split(":")
        if len(parts) >= 5:
            return f"conv:children:{':'.join(parts[4:])}"
        return raw
    if raw.startswith("imf:conv:"):
        parts = raw.split(":")
        if len(parts) >= 5:
            return f"conv:{parts[2]}:{':'.join(parts[4:])}"
        return raw
    if raw.startswith("imf:mem:"):
        parts = raw.split(":")
        if len(parts) >= 4:
            rest = ":".join(parts[4:])
            return f"mem:{parts[2]}:{rest}" if rest else f"mem:{parts[2]}"
        return raw
    if raw.startswith("imf:model_profiles:"):
        parts = raw.split(":")
        if len(parts) >= 4:
            return f"model_profiles:{':'.join(parts[3:])}"
        return f"model_profiles:{raw[len('imf:model_profiles:'):]}"
    if raw.startswith("imf:tenant:motet:index:"):
        return "tenant:motet:index"
    if raw.startswith("imf:tenant:motet:"):
        parts = raw.split(":")
        if len(parts) >= 5:
            return f"tenant:motet:{':'.join(parts[4:])}"
        return raw
    if raw.startswith("imf:tenant:meta:"):
        return "tenant:meta"
    if raw.startswith("imf:openai_compat:"):
        return f"openai_compat:{raw[len('imf:openai_compat:'):]}"
    if raw.startswith("imf:auth:refresh_token:"):
        return f"auth:refresh_token:{raw[len('imf:auth:refresh_token:'):]}"
    if raw.startswith("imf:auth:service_account:"):
        return f"auth:service_account:{raw[len('imf:auth:service_account:'):]}"
    if raw.startswith("imf:vault:"):
        return f"vault:{raw[len('imf:vault:'):]}"
    return raw


def expand_legacy_logical_keys(logical_key: str, tenant_id: str) -> Tuple[str, ...]:
    """Phase-2 / pre-Phase-2 logical keys for rewrite dests and envelope AAD."""
    tid = (tenant_id or "").strip()
    collapsed = _historical_logical_key(logical_key, tid)
    keys: List[str] = []
    if collapsed.startswith("memvec:"):
        keys.append(f"imf:memvec:{collapsed[len('memvec:'):]}")
    elif collapsed.startswith("conv:owner:"):
        rest = collapsed[len("conv:owner:") :]
        motet, sep, cid = rest.partition(":")
        if tid and sep:
            keys.append(f"imf:conv:owner:{motet}:{tid}:{cid}")
    elif collapsed.startswith("conv:children:"):
        cid = collapsed[len("conv:children:") :]
        if tid and cid:
            keys.append(f"imf:conv:children:{tid}:{cid}")
    elif is_collapsed_conv_family(collapsed):
        rest = collapsed[len("conv:") :]
        motet, sep, principal = rest.partition(":")
        if tid and sep:
            keys.append(f"imf:conv:{motet}:{tid}:{principal}")
    elif collapsed.startswith("mem:"):
        rest = collapsed[len("mem:") :]
        motet, sep, remainder = rest.partition(":")
        if tid:
            keys.append(f"imf:mem:{motet}:{tid}:{remainder}" if sep else f"imf:mem:{motet}:{tid}")
    elif collapsed == "tenant:meta":
        if tid:
            keys.append(f"imf:tenant:meta:{tid}")
    elif collapsed == "tenant:motet:index":
        if tid:
            keys.append(f"imf:tenant:motet:index:{tid}")
    elif collapsed.startswith("tenant:motet:"):
        motet = collapsed[len("tenant:motet:") :]
        if tid and motet:
            keys.append(f"imf:tenant:motet:{tid}:{motet}")
    elif collapsed.startswith("model_profiles:"):
        name = collapsed[len("model_profiles:") :]
        if tid and name:
            keys.append(f"imf:model_profiles:{tid}:{name}")
    elif collapsed.startswith(_STRIP_LEGACY_COLLAPSED_PREFIXES):
        keys.append(f"imf:{collapsed}")
    seen: set[str] = set()
    out: List[str] = []
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return tuple(out)


def tenant_key(tenant_id: str, logical_key: str) -> str:
    """Return ``{tenant_id}:{logical}``. Idempotent if *logical_key* is already prefixed."""
    tid = (tenant_id or "").strip()
    key = (logical_key or "").strip()
    if not tid:
        raise ValueError("tenant_id is required for tenant-prefixed Redis keys")
    if not key:
        raise ValueError("logical_key is required for tenant-prefixed Redis keys")
    prefix = f"{tid}:"
    if key.startswith(prefix):
        key = key[len(prefix) :]
    return f"{prefix}{key}"


def is_tenant_prefixed(tenant_id: str, key: str) -> bool:
    """True when *key* already starts with ``{tenant_id}:``."""
    tid = (tenant_id or "").strip()
    return bool(tid) and (key or "").startswith(f"{tid}:")


def strip_tenant_prefix(tenant_id: str, key: str) -> str:
    """Remove a leading ``{tenant_id}:`` when present."""
    tid = (tenant_id or "").strip()
    prefix = f"{tid}:"
    if tid and (key or "").startswith(prefix):
        return key[len(prefix) :]
    return key


def maybe_tenant_key(tenant_id: Optional[str], logical_key: str) -> str:
    """Prefix when *tenant_id* is a usable catalog id; otherwise return the logical key."""
    tid = (tenant_id or "").strip()
    if not is_usable_tenant_id(tid):
        return logical_key
    return tenant_key(tid, logical_key)


def vault_read_key_candidates(
    logical_key: str, tenant_id: Optional[str] = None
) -> Tuple[str, ...]:
    """
    Physical vault keys that may hold a row: current write shape, then leftovers.

    Current: ``{tid}:vault:…`` when *tenant_id* is usable, else ``motet:vault:…``.
    Leftovers: ``None:vault:…``, unprefixed ``vault:…``, ``imf:vault:…``,
    ``{tid}:imf:vault:…``. Exists and retrieve must use this same list.
    """
    logical = (logical_key or "").strip()
    if not logical:
        return ()
    logical = _product_suffix(logical)
    out: List[str] = []
    seen: set[str] = set()

    def add(key: str) -> None:
        if key and key not in seen:
            seen.add(key)
            out.append(key)

    tid = (tenant_id or "").strip()
    if is_usable_tenant_id(tid):
        add(tenant_key(tid, logical))
        add(f"{tid}:{LEGACY_PRODUCT_PREFIX}{logical}")
    add(product_key(logical))
    add(f"None:{logical}")
    add(logical)
    add(f"{LEGACY_PRODUCT_PREFIX}{logical}")
    return tuple(out)


def family_scan_patterns(logical_prefix: str) -> Tuple[str, ...]:
    """SCAN globs for the collapsed family only (no leftover ``imf:`` shapes)."""
    marker = logical_prefix if logical_prefix.endswith(":") else f"{logical_prefix}:"
    return (f"{marker}*", f"*:{marker}*")


def write_key(redis_client: Any, tenant_id: str, logical_key: str) -> str:
    """Return the tenant-prefixed key to write."""
    del redis_client
    return tenant_key(tenant_id, logical_key)


def _as_keys(keys: Union[str, Sequence[str]]) -> Tuple[str, ...]:
    """Normalize a single Redis key or a sequence of keys."""
    if isinstance(keys, str):
        return (keys,) if keys else ()
    return tuple(key for key in keys if key)


def hgetall_first(redis_client: Any, keys: Union[str, Sequence[str]]) -> Dict[str, Any]:
    """Return the first non-empty HGETALL among *keys*."""
    for key in _as_keys(keys):
        try:
            data = redis_client.hgetall(key)
        except Exception:
            continue
        if data:
            return data
    return {}


def smembers_union(redis_client: Any, keys: Union[str, Sequence[str]]) -> List[str]:
    """Decoded SMEMBERS for one key, or the union across several."""
    seen: set[str] = set()
    out: List[str] = []
    for key in _as_keys(keys):
        try:
            members = redis_client.smembers(key) or set()
        except Exception:
            continue
        for member in members:
            decoded = decode_redis_id(member)
            if decoded in seen:
                continue
            seen.add(decoded)
            out.append(decoded)
    return out


def delete_candidate_keys(redis_client: Any, keys: Union[str, Sequence[str]]) -> int:
    """Delete each key that exists. Returns the number of keys removed."""
    removed = 0
    for key in _as_keys(keys):
        try:
            if redis_client.delete(key):
                removed += 1
        except Exception:
            continue
    return removed


def stable_aad_logical_key(redis_key: str, tenant_id: str) -> str:
    """Collapsed logical name bound into new envelopes (not the physical Redis key)."""
    logical = strip_tenant_prefix(tenant_id, redis_key)
    return _historical_logical_key(logical, tenant_id)


def payload_aad_key_candidates(redis_key: str, tenant_id: str) -> Tuple[str, ...]:
    """
    Redis key names to bind into encrypted-payload AES-GCM AAD.

    New writes use ``stable_aad_logical_key`` (collapsed, no leading tenant).
    Decrypt also tries the physical key, Phase 2 ``{tenant}:imf:…`` keys, and
    pre-Phase-2 unprefixed ``imf:…`` keys so historical ciphertext still opens.
    """
    raw = (redis_key or "").strip()
    if not raw:
        return ()
    tid = (tenant_id or "").strip()
    keys: List[str] = []
    stable = stable_aad_logical_key(raw, tid)
    if stable:
        keys.append(stable)
    if raw not in keys:
        keys.append(raw)
    logical = strip_tenant_prefix(tid, raw) if tid else raw
    if logical and logical not in keys:
        keys.append(logical)
    if tid:
        new_physical = tenant_key(tid, stable or raw)
        if new_physical not in keys:
            keys.append(new_physical)
        for legacy in expand_legacy_logical_keys(stable or logical, tid):
            phase2 = f"{tid}:{legacy}"
            if phase2 not in keys:
                keys.append(phase2)
            if legacy not in keys:
                keys.append(legacy)
    return tuple(keys)


def is_shared_key(key: str) -> bool:
    """True for shared/infrastructure keys that stay unprefixed."""
    raw = key or ""
    if raw.startswith("celery-") or raw.startswith("_kombu"):
        return True
    return any(raw.startswith(prefix) for prefix in SHARED_KEY_PREFIXES)


def is_tenant_scoped_key(logical_key: str) -> bool:
    """True when the logical (unprefixed) key belongs to a tenant-scoped family."""
    key = logical_key or ""
    if is_collapsed_conv_family(key):
        return True
    if key == "tenant:meta":
        return True
    return any(key.startswith(prefix) for prefix in TENANT_SCOPED_PREFIXES)


def infer_tenant_id_from_key(logical_key: str) -> Optional[str]:
    """
    Best-effort tenant_id from a *logical* (unprefixed) key.

    Returns None when the tenant is only in the value (cmd:*, art:*, memvec, …)
    or the embedded id is a sentinel (``None``, MagicMock).
    """
    inferred = _infer_tenant_id_from_key_raw(logical_key)
    return inferred if is_usable_tenant_id(inferred) else None


def _infer_tenant_id_from_key_raw(logical_key: str) -> Optional[str]:
    parts = (logical_key or "").split(":")
    if len(parts) < 3:
        return None
    # idx:art:tenant:{tenant}...
    if logical_key.startswith("idx:art:tenant:") and len(parts) >= 4:
        return parts[3] or None
    # budget:usage:daily:{tenant}:... / budget:config:{tenant}
    if logical_key.startswith("budget:") and len(parts) >= 3:
        if parts[1] == "config":
            return parts[2] or None
        if len(parts) >= 4:
            return parts[3] or None
    # cost:{kind}:{tenant}:...
    if logical_key.startswith("cost:") and len(parts) >= 3:
        return parts[2] or None
    # turn_checkpoint:{tenant}:{motet}:{id}
    if logical_key.startswith("turn_checkpoint:") and len(parts) >= 3:
        if parts[1] in {"index", "by_conversation"}:
            return parts[2] or None
        return parts[1] or None
    if logical_key.startswith("workflow_checkpoint:") and len(parts) >= 3:
        if parts[1] in {"index", "paused"}:
            return parts[2] or None
        return parts[1] or None
    if logical_key.startswith("workflow_control:") and len(parts) >= 3:
        return parts[1] or None
    if logical_key.startswith("workspace:container:"):
        if parts[2] == "index" and len(parts) >= 5 and parts[3] == "tenant":
            return parts[4] or None
        if len(parts) >= 4:
            return parts[2] or None
    if logical_key.startswith("artifact_chunk:") and len(parts) >= 3:
        return parts[1] or None
    if logical_key.startswith("oauth:tokens:") and len(parts) >= 4:
        return parts[2] or None
    if is_vault_family_logical_key(logical_key):
        kek = vault_kek_tenant_id(logical_key)
        if kek:
            return kek
        cache_prefixes = ("vault:cache:", "motet:vault:cache:")
        if logical_key.startswith(cache_prefixes) and ":t:" in logical_key:
            after = logical_key.rsplit(":t:", 1)[-1]
            return after.split(":m:")[0].split(":")[0].strip() or None
        return None
    return None


def tenant_acl_username(tenant_id: str, *, app_prefix: str = "motet") -> str:
    """Stable ACL / ElastiCache user id for a tenant-scoped application client."""
    tid = (tenant_id or "").strip()
    if not tid:
        raise ValueError("tenant_id is required for a tenant ACL username")
    prefix = (app_prefix or "motet").strip() or "motet"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in tid)[:40]
    return f"{prefix}-t-{safe}"[:40]


def tenant_acl_access_string(tenant_id: str) -> str:
    """ElastiCache/Valkey ACL access string for a tenant-scoped application user."""
    tid = (tenant_id or "").strip()
    if not tid:
        raise ValueError("tenant_id is required for a tenant ACL access string")
    return f"on ~{tid}:* &{tid}:* +@read +@write -@dangerous"


def already_tenant_prefixed_key(key: str) -> bool:
    """
    True when *key* looks like ``{tenant}:{tenant-scoped-family}``.

    A logical key that itself starts with a tenant-scoped family
    (``cost:conversation:…``, ``idx:art:…``, ``meta:art:…``) is not treated as
    already prefixed — the first segment is the family, not a tenant id.
    """
    raw = key or ""
    if ":" not in raw:
        return False
    if is_tenant_scoped_key(raw) or is_shared_key(raw):
        return False
    tenant, rest = raw.split(":", 1)
    if not tenant or is_shared_key(rest):
        return False
    return is_tenant_scoped_key(rest)


async def retrieve_structured_data_tenant(
    client_id: str,
    tenant_id: str,
    logical_key: str,
    format_type: str = "hash",
) -> Optional[Dict[str, Any]]:
    """Read structured data from the tenant-prefixed key."""
    from .redis_manager import retrieve_structured_data

    return await retrieve_structured_data(
        client_id, tenant_key(tenant_id, logical_key), format_type=format_type
    )


def retrieve_structured_data_tenant_sync(
    client_id: str,
    tenant_id: str,
    logical_key: str,
    format_type: str = "hash",
) -> Optional[Dict[str, Any]]:
    """Read structured data from the tenant-prefixed key (sync)."""
    from .redis_manager import retrieve_structured_data_sync

    return retrieve_structured_data_sync(
        client_id, tenant_key(tenant_id, logical_key), format_type=format_type
    )


async def store_structured_data_tenant(
    client_id: str,
    tenant_id: str,
    logical_key: str,
    data: Dict[str, Any],
    format_type: str = "hash",
) -> str:
    """Write structured data to the tenant-prefixed key. Returns the key used."""
    from .redis_manager import store_structured_data

    key = tenant_key(tenant_id, logical_key)
    await store_structured_data(client_id, key, data, format_type=format_type)
    return key


def store_structured_data_tenant_sync(
    client_id: str,
    tenant_id: str,
    logical_key: str,
    data: Dict[str, Any],
    format_type: str = "hash",
) -> str:
    """Sync write to the tenant-prefixed key. Returns the key used."""
    from .redis_manager import store_structured_data_sync

    key = tenant_key(tenant_id, logical_key)
    store_structured_data_sync(client_id, key, data, format_type=format_type)
    return key


def first_existing_key(redis_client: Any, keys: Union[str, Sequence[str]]) -> Optional[str]:
    """Return the first key that EXISTS on *redis_client*, or None."""
    for key in _as_keys(keys):
        try:
            if redis_client.exists(key):
                return key
        except Exception:
            continue
    return None


def decode_redis_id(raw: Any) -> str:
    """Decode a Redis zset/set member that may be bytes or str."""
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def cmd_key_scan_patterns(kind: str = "meta") -> Tuple[str, ...]:
    """
    SCAN globs for command blobs: legacy ``cmd:{kind}:*`` and prefixed
    ``*:cmd:{kind}:*`` (ADR-0095 Phase 2).
    """
    marker = f"cmd:{kind}:"
    return (f"{marker}*", f"*:{marker}*")


def command_id_from_cmd_key(key: Any, kind: str = "meta") -> str:
    """
    Extract command_id from ``cmd:{kind}:{id}`` or ``{tenant}:cmd:{kind}:{id}``.

    ``str.replace("cmd:meta:", "")`` is wrong on prefixed keys: it leaves the
    tenant prefix in the id and the Tasks view cannot load the command.
    """
    raw = decode_redis_id(key)
    marker = f"cmd:{kind}:"
    idx = raw.find(marker)
    if idx < 0:
        return ""
    return raw[idx + len(marker) :]


def iter_cmd_keys_sync(
    redis_client: Any, kind: str = "meta", count: int = 300
) -> Iterator[str]:
    """Yield unique command keys from both legacy and tenant-prefixed SCAN globs."""
    seen: set[str] = set()
    for pattern in cmd_key_scan_patterns(kind):
        cursor = 0
        while True:
            cursor, keys = redis_client.scan(cursor, match=pattern, count=count)
            for key in keys:
                decoded = decode_redis_id(key)
                if decoded in seen:
                    continue
                seen.add(decoded)
                yield decoded
            if cursor == 0:
                break


def zrevrange_ids_with_fallback(
    redis_client: Any,
    keys: Union[str, Iterable[str]],
    start: int = 0,
    end: int = -1,
) -> List[str]:
    """Read a zset from the first non-empty key."""
    for key in _as_keys(tuple(keys) if not isinstance(keys, str) else keys):
        try:
            members = redis_client.zrevrange(key, start, end) or []
        except Exception:
            continue
        if members:
            return [decode_redis_id(m) for m in members]
    return []
