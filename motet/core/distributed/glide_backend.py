"""
Motet - Valkey GLIDE Backend

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Optional Valkey GLIDE client for UnifiedRedisManager. GLIDE is the official
    Rust-core Valkey client (reconnect, cluster routing, OTel). It is not a
    drop-in for redis-py: Celery broker/backend, redis-py Pub/Sub objects, and
    pipelines stay on redis-py. Application get/set/hash/zset/BLPOP/FT.* go
    through a redis-py-shaped adapter over GLIDE when MOTET_VALKEY_CLIENT=glide.

Dependencies:
    - valkey-glide: async GlideClient
    - valkey-glide-sync: sync GlideClient
    - urllib.parse: Redis URL parsing

Usage:
    from motet.core.distributed.glide_backend import (
        resolve_valkey_client_backend,
        create_sync_glide_adapter,
    )

    if resolve_valkey_client_backend() == "glide":
        client = create_sync_glide_adapter(url, fallback=redis_client)

Notes:
    - Opt in with MOTET_VALKEY_CLIENT=glide. Default remains redis-py.
    - get_pubsub_redis_client() always uses redis-py (callback/subscribe API).
    - pipeline() / pubsub() delegate to the redis-py fallback when provided.
    - SCAN cursors are sent to GLIDE as bytes. The adapter still returns an
      int cursor to redis-py callers; scan_iter re-encodes that int on the
      next page so GLIDE does not raise Unsupported argument type.
    - Sync adapters share one process-wide GLIDE client. Adapter close()
      drops only the redis-py fallback; pass close_shared=True to tear down
      the shared client (process shutdown).
    - Default request timeout is 30s and inflight limit is 128
      (``MOTET_VALKEY_GLIDE_TIMEOUT_MS``, ``MOTET_VALKEY_GLIDE_INFLIGHT``).
      The library default of 2 inflight slots saturates as soon as one SCAN
      or large HGETALL is in flight, so later GET/EXISTS fail in 5s.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import structlog

logger = structlog.get_logger(__name__)

_GLIDE_UNAVAILABLE_LOGGED = False


def resolve_valkey_client_backend() -> str:
    """
    Return ``glide`` or ``redis``.

    ``MOTET_VALKEY_CLIENT=glide`` selects GLIDE. Default is ``redis`` so
    existing images and unit tests keep redis-py until the packages are
    installed and the flag is set.
    """
    raw = (os.getenv("MOTET_VALKEY_CLIENT") or "redis").strip().lower()
    if raw in {"glide", "valkey-glide", "valkey_glide"}:
        if _glide_importable():
            return "glide"
        _log_glide_unavailable("MOTET_VALKEY_CLIENT=glide but packages are missing")
        return "redis"
    return "redis"


def _glide_importable() -> bool:
    try:
        import glide  # noqa: F401
        import glide_sync  # noqa: F401

        return True
    except Exception:
        return False


def _log_glide_unavailable(reason: str) -> None:
    global _GLIDE_UNAVAILABLE_LOGGED
    if _GLIDE_UNAVAILABLE_LOGGED:
        return
    _GLIDE_UNAVAILABLE_LOGGED = True
    logger.warning("valkey_glide_unavailable_using_redis_py", reason=reason)


def parse_valkey_url(url: str) -> Dict[str, Any]:
    """Parse ``redis://`` / ``rediss://`` into GLIDE connection fields."""
    parsed = urlparse(url or "")
    if parsed.scheme not in {"redis", "rediss"}:
        raise ValueError(f"Unsupported Valkey URL scheme: {parsed.scheme!r}")
    path = (parsed.path or "/0").lstrip("/")
    database_id = 0
    if path:
        try:
            database_id = int(path.split("/")[0] or 0)
        except ValueError:
            database_id = 0
    query = parse_qs(parsed.query or "")
    cert_reqs = (query.get("ssl_cert_reqs") or [""])[0].upper()
    insecure = cert_reqs in {"CERT_NONE", "NONE"}
    env_certs = (os.getenv("MOTET_REDIS_SSL_CERT_REQS") or "").upper()
    if env_certs in {"NONE", "CERT_NONE"}:
        insecure = True
    username = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None
    return {
        "host": parsed.hostname or "localhost",
        "port": int(parsed.port or 6379),
        "database_id": database_id,
        "use_tls": parsed.scheme == "rediss",
        "insecure_tls": insecure,
        "username": username,
        "password": password,
    }


def resolve_glide_request_timeout_ms(default_ms: int = 30_000) -> int:
    """``MOTET_VALKEY_GLIDE_TIMEOUT_MS`` or *default_ms* (minimum 1000)."""
    raw = (os.getenv("MOTET_VALKEY_GLIDE_TIMEOUT_MS") or "").strip()
    if not raw:
        return default_ms
    try:
        return max(1000, int(raw))
    except ValueError:
        logger.warning(
            "invalid_motet_valkey_glide_timeout_ms",
            value=raw,
            default_ms=default_ms,
        )
        return default_ms


def resolve_glide_inflight_limit(default: int = 128) -> int:
    """``MOTET_VALKEY_GLIDE_INFLIGHT`` or *default* (minimum 2)."""
    raw = (os.getenv("MOTET_VALKEY_GLIDE_INFLIGHT") or "").strip()
    if not raw:
        return default
    try:
        return max(2, int(raw))
    except ValueError:
        logger.warning(
            "invalid_motet_valkey_glide_inflight",
            value=raw,
            default=default,
        )
        return default


def build_glide_client_configuration(
    url: str,
    *,
    request_timeout_ms: Optional[int] = None,
    inflight_requests_limit: Optional[int] = None,
) -> Any:
    """Build a standalone ``GlideClientConfiguration`` from a Redis URL."""
    from glide_sync import (
        AdvancedGlideClientConfiguration,
        GlideClientConfiguration,
        NodeAddress,
        ServerCredentials,
        TlsAdvancedConfiguration,
    )

    parsed = parse_valkey_url(url)
    credentials = None
    if parsed["username"] or parsed["password"]:
        credentials = ServerCredentials(
            username=parsed["username"] or "",
            password=parsed["password"] or "",
        )
    advanced = None
    if parsed["use_tls"] and parsed["insecure_tls"]:
        advanced = AdvancedGlideClientConfiguration(
            tls_config=TlsAdvancedConfiguration(use_insecure_tls=True),
        )
    timeout_ms = (
        request_timeout_ms
        if request_timeout_ms is not None
        else resolve_glide_request_timeout_ms()
    )
    inflight = (
        inflight_requests_limit
        if inflight_requests_limit is not None
        else resolve_glide_inflight_limit()
    )
    return GlideClientConfiguration(
        addresses=[NodeAddress(host=parsed["host"], port=parsed["port"])],
        use_tls=bool(parsed["use_tls"]),
        credentials=credentials,
        database_id=int(parsed["database_id"]),
        request_timeout=timeout_ms,
        inflight_requests_limit=inflight,
        client_name="motet",
        advanced_config=advanced,
        lazy_connect=True,
    )


def create_sync_glide_client(url: str) -> Any:
    """Create a process-wide sync GLIDE client."""
    from glide_sync import GlideClient

    return GlideClient.create(build_glide_client_configuration(url))


def glide_client_is_closed(client: Any) -> bool:
    """True when *client* is missing or GLIDE has already closed it."""
    if client is None:
        return True
    for attr in ("_is_closed", "is_closed", "closed"):
        value = getattr(client, attr, None)
        if isinstance(value, bool):
            return value
        if callable(value):
            try:
                return bool(value())
            except Exception:
                return True
    return False


def _encode_scan_cursor(cursor: Any) -> bytes:
    """Encode a redis-py SCAN cursor as the bytes GLIDE requires.

    redis-py callers pass and receive ``int`` cursors. GLIDE's ``scan``
    rejects ``int`` (``Unsupported argument type: <class 'int'>``), so every
    outbound cursor — including the non-zero page token from the previous
    reply — must be bytes.
    """
    if cursor in (0, "0", b"0", None):
        return b"0"
    if isinstance(cursor, (bytes, bytearray)):
        return bytes(cursor)
    return str(cursor).encode("utf-8")


def _glide_scan_options(*, match: Optional[str], count: Optional[int]) -> Dict[str, Any]:
    """Keyword args for GLIDE ``scan``; omit unset match/count."""
    options: Dict[str, Any] = {}
    if match is not None:
        options["match"] = match
    if count is not None:
        options["count"] = int(count)
    return options


def _decode(value: Any, *, decode_responses: bool) -> Any:
    if not decode_responses:
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, list):
        return [_decode(v, decode_responses=True) for v in value]
    if isinstance(value, tuple):
        return tuple(_decode(v, decode_responses=True) for v in value)
    if isinstance(value, dict):
        return {
            _decode(k, decode_responses=True): _decode(v, decode_responses=True)
            for k, v in value.items()
        }
    return value


class SyncGlideRedisAdapter:
    """redis-py-shaped sync client over GLIDE, with optional redis-py fallback."""

    def __init__(
        self,
        glide_client: Any,
        *,
        decode_responses: bool = True,
        fallback: Any = None,
    ) -> None:
        self._glide = glide_client
        self._decode_responses = decode_responses
        self._fallback = fallback

    def __getattr__(self, name: str) -> Any:
        if self._fallback is not None and hasattr(self._fallback, name):
            return getattr(self._fallback, name)
        raise AttributeError(f"{type(self).__name__} has no attribute {name!r}")

    def _out(self, value: Any) -> Any:
        return _decode(value, decode_responses=self._decode_responses)

    def ping(self) -> bool:
        result = self._glide.ping()
        return result in (True, b"PONG", "PONG") or bool(result)

    def get(self, name: str) -> Any:
        return self._out(self._glide.get(name))

    def set(
        self,
        name: str,
        value: Any,
        ex: Any = None,
        px: Any = None,
        nx: bool = False,
        xx: bool = False,
        keepttl: bool = False,
        **kwargs: Any,
    ) -> Any:
        from glide_sync import ConditionalChange, ExpirySet, ExpiryType

        expiry = None
        if ex is not None:
            expiry = ExpirySet(ExpiryType.SEC, int(ex))
        elif px is not None:
            expiry = ExpirySet(ExpiryType.MILLSEC, int(px))
        elif keepttl:
            expiry = ExpirySet(ExpiryType.KEEP_TTL, None)
        cond = None
        if nx:
            cond = ConditionalChange.ONLY_IF_DOES_NOT_EXIST
        elif xx:
            cond = ConditionalChange.ONLY_IF_EXISTS
        result = self._glide.set(
            name,
            value if isinstance(value, (bytes, bytearray)) else str(value),
            conditional_set=cond,
            expiry=expiry,
        )
        if nx or xx:
            return True if result is not None else None
        return True

    def delete(self, *names: str) -> int:
        keys = [n for n in names if n is not None]
        if not keys:
            return 0
        return int(self._glide.delete(list(keys)))

    def exists(self, *names: str) -> int:
        keys = [n for n in names if n is not None]
        if not keys:
            return 0
        return int(self._glide.exists(list(keys)))

    def expire(self, name: str, time: int) -> bool:
        return bool(self._glide.expire(name, int(time)))

    def ttl(self, name: str) -> int:
        return int(self._glide.ttl(name))

    def hset(self, name: str, key: Any = None, value: Any = None, mapping: Optional[Mapping[Any, Any]] = None) -> int:
        fields: Dict[Any, Any] = {}
        if mapping:
            fields.update(mapping)
        if key is not None:
            fields[key] = value
        if not fields:
            return 0
        encoded = {
            str(k): (v if isinstance(v, (bytes, bytearray)) else str(v))
            for k, v in fields.items()
        }
        return int(self._glide.hset(name, encoded))

    def hget(self, name: str, key: str) -> Any:
        return self._out(self._glide.hget(name, key))

    def hgetall(self, name: str) -> Dict[Any, Any]:
        raw = self._glide.hgetall(name) or {}
        decoded = self._out(raw)
        return decoded if isinstance(decoded, dict) else {}

    def hdel(self, name: str, *keys: str) -> int:
        return int(self._glide.hdel(name, list(keys)))

    def hexists(self, name: str, key: str) -> bool:
        return bool(self._glide.hexists(name, key))

    def zadd(self, name: str, mapping: Mapping[Any, float], **kwargs: Any) -> int:
        members = {str(member): float(score) for member, score in mapping.items()}
        return int(self._glide.zadd(name, members))

    def zrevrange(self, name: str, start: int, end: int, withscores: bool = False) -> List[Any]:
        from glide_sync import RangeByIndex

        rows = self._glide.zrange(name, RangeByIndex(int(start), int(end)), reverse=True)
        return self._out(rows) or []

    def zrange(self, name: str, start: int, end: int, desc: bool = False, **kwargs: Any) -> List[Any]:
        from glide_sync import RangeByIndex

        rows = self._glide.zrange(name, RangeByIndex(int(start), int(end)), reverse=bool(desc))
        return self._out(rows) or []

    def blpop(self, keys: Sequence[str], timeout: float = 0) -> Optional[List[Any]]:
        result = self._glide.blpop(list(keys), float(timeout))
        if not result:
            return None
        return self._out(result)

    def publish(self, channel: str, message: Any) -> int:
        payload = message if isinstance(message, (bytes, bytearray)) else str(message)
        return int(self._glide.publish(channel, payload))

    def scan(
        self,
        cursor: Any = 0,
        match: Optional[str] = None,
        count: Optional[int] = None,
        _type: Optional[str] = None,
        **kwargs: Any,
    ) -> Tuple[Any, List[Any]]:
        result = self._glide.scan(
            _encode_scan_cursor(cursor),
            **_glide_scan_options(match=match, count=count),
        )
        next_cursor, keys = result[0], result[1]
        decoded_cursor = _decode(next_cursor, decode_responses=True)
        try:
            cursor_out: Any = int(decoded_cursor)
        except (TypeError, ValueError):
            cursor_out = decoded_cursor
        return cursor_out, self._out(keys) or []

    def scan_iter(self, match: Optional[str] = None, count: Optional[int] = None) -> Iterator[Any]:
        cursor: Any = 0
        while True:
            cursor, keys = self.scan(cursor=cursor, match=match, count=count)
            for key in keys or []:
                yield key
            if cursor in (0, "0", b"0"):
                break

    def execute_command(self, *args: Any) -> Any:
        command = [a if isinstance(a, (bytes, bytearray)) else str(a) for a in args]
        return self._out(self._glide.custom_command(command))

    def close(self, *, close_shared: bool = False) -> None:
        """Close the redis-py fallback. Do not close the shared GLIDE client.

        UnifiedRedisManager wraps one process-wide GLIDE client in many
        adapters. A health-check eviction must not kill that client or every
        later GET/SET fails with ``the client is closed``.
        """
        if close_shared:
            closer = getattr(self._glide, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
        if self._fallback is not None:
            try:
                self._fallback.close()
            except Exception:
                pass


class AsyncGlideRedisAdapter:
    """redis-py-shaped async client over GLIDE (lazy create)."""

    def __init__(
        self,
        config: Any,
        *,
        decode_responses: bool = True,
        fallback: Any = None,
    ) -> None:
        self._config = config
        self._glide: Any = None
        self._decode_responses = decode_responses
        self._fallback = fallback

    def __getattr__(self, name: str) -> Any:
        if self._fallback is not None and hasattr(self._fallback, name):
            return getattr(self._fallback, name)
        raise AttributeError(f"{type(self).__name__} has no attribute {name!r}")

    def _out(self, value: Any) -> Any:
        return _decode(value, decode_responses=self._decode_responses)

    async def _ensure(self) -> Any:
        if self._glide is None:
            from glide import GlideClient

            self._glide = await GlideClient.create(self._config)
        return self._glide

    async def ping(self) -> bool:
        client = await self._ensure()
        result = await client.ping()
        return result in (True, b"PONG", "PONG") or bool(result)

    async def get(self, name: str) -> Any:
        client = await self._ensure()
        return self._out(await client.get(name))

    async def set(
        self,
        name: str,
        value: Any,
        ex: Any = None,
        px: Any = None,
        nx: bool = False,
        xx: bool = False,
        keepttl: bool = False,
        **kwargs: Any,
    ) -> Any:
        from glide import ConditionalChange, ExpirySet, ExpiryType

        client = await self._ensure()
        expiry = None
        if ex is not None:
            expiry = ExpirySet(ExpiryType.SEC, int(ex))
        elif px is not None:
            expiry = ExpirySet(ExpiryType.MILLSEC, int(px))
        elif keepttl:
            expiry = ExpirySet(ExpiryType.KEEP_TTL, None)
        cond = None
        if nx:
            cond = ConditionalChange.ONLY_IF_DOES_NOT_EXIST
        elif xx:
            cond = ConditionalChange.ONLY_IF_EXISTS
        result = await client.set(
            name,
            value if isinstance(value, (bytes, bytearray)) else str(value),
            conditional_set=cond,
            expiry=expiry,
        )
        if nx or xx:
            return True if result is not None else None
        return True

    async def delete(self, *names: str) -> int:
        client = await self._ensure()
        keys = [n for n in names if n is not None]
        if not keys:
            return 0
        return int(await client.delete(list(keys)))

    async def exists(self, *names: str) -> int:
        client = await self._ensure()
        keys = [n for n in names if n is not None]
        if not keys:
            return 0
        return int(await client.exists(list(keys)))

    async def expire(self, name: str, time: int) -> bool:
        client = await self._ensure()
        return bool(await client.expire(name, int(time)))

    async def hset(
        self,
        name: str,
        key: Any = None,
        value: Any = None,
        mapping: Optional[Mapping[Any, Any]] = None,
    ) -> int:
        client = await self._ensure()
        fields: Dict[Any, Any] = {}
        if mapping:
            fields.update(mapping)
        if key is not None:
            fields[key] = value
        if not fields:
            return 0
        encoded = {
            str(k): (v if isinstance(v, (bytes, bytearray)) else str(v))
            for k, v in fields.items()
        }
        return int(await client.hset(name, encoded))

    async def hgetall(self, name: str) -> Dict[Any, Any]:
        client = await self._ensure()
        raw = await client.hgetall(name) or {}
        decoded = self._out(raw)
        return decoded if isinstance(decoded, dict) else {}

    async def blpop(self, keys: Sequence[str], timeout: float = 0) -> Optional[List[Any]]:
        client = await self._ensure()
        result = await client.blpop(list(keys), float(timeout))
        if not result:
            return None
        return self._out(result)

    async def publish(self, channel: str, message: Any) -> int:
        client = await self._ensure()
        payload = message if isinstance(message, (bytes, bytearray)) else str(message)
        return int(await client.publish(channel, payload))

    async def execute_command(self, *args: Any) -> Any:
        client = await self._ensure()
        command = [a if isinstance(a, (bytes, bytearray)) else str(a) for a in args]
        return self._out(await client.custom_command(command))

    async def scan(
        self,
        cursor: Any = 0,
        match: Optional[str] = None,
        count: Optional[int] = None,
        **kwargs: Any,
    ) -> Tuple[Any, List[Any]]:
        client = await self._ensure()
        result = await client.scan(
            _encode_scan_cursor(cursor),
            **_glide_scan_options(match=match, count=count),
        )
        next_cursor, keys = result[0], result[1]
        decoded_cursor = _decode(next_cursor, decode_responses=True)
        try:
            cursor_out: Any = int(decoded_cursor)
        except (TypeError, ValueError):
            cursor_out = decoded_cursor
        return cursor_out, self._out(keys) or []

    async def scan_iter(self, match: Optional[str] = None, count: Optional[int] = None) -> Any:
        cursor: Any = 0
        while True:
            cursor, keys = await self.scan(cursor=cursor, match=match, count=count)
            for key in keys or []:
                yield key
            if cursor in (0, "0", b"0"):
                break

    async def close(self) -> None:
        if self._glide is not None:
            closer = getattr(self._glide, "close", None)
            if callable(closer):
                result = closer()
                if hasattr(result, "__await__"):
                    await result
            self._glide = None
        if self._fallback is not None:
            closer = getattr(self._fallback, "aclose", None) or getattr(self._fallback, "close", None)
            if callable(closer):
                result = closer()
                if hasattr(result, "__await__"):
                    await result

    aclose = close


def create_sync_glide_adapter(
    url: str,
    *,
    decode_responses: bool = True,
    fallback: Any = None,
) -> SyncGlideRedisAdapter:
    """Create a sync redis-py-shaped adapter over a live GLIDE client."""
    return SyncGlideRedisAdapter(
        create_sync_glide_client(url),
        decode_responses=decode_responses,
        fallback=fallback,
    )


def create_async_glide_adapter(
    url: str,
    *,
    decode_responses: bool = True,
    fallback: Any = None,
) -> AsyncGlideRedisAdapter:
    """Create a lazy async adapter (GlideClient is created on first await)."""
    return AsyncGlideRedisAdapter(
        build_glide_client_configuration(url),
        decode_responses=decode_responses,
        fallback=fallback,
    )
