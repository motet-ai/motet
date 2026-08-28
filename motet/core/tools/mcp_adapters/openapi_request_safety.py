"""
Motet - OpenAPI Adapter Request Safety

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    SSRF and payload guardrails for the generic OpenAPI-to-MCP adapter.
    Validates request URLs (HTTPS-by-default, optional host allowlist) and
    enforces configurable timeouts and max response sizes so generated tools
    cannot be used as an unrestricted network bridge.

Dependencies:
    - urllib.parse: URL scheme/host extraction
    - httpx: optional event-hook helpers for the runtime client

Usage:
    from motet.core.tools.mcp_adapters.openapi_request_safety import (
        RequestSafetyPolicy,
        validate_request_url,
        read_response_bounded,
    )

    policy = RequestSafetyPolicy(allowed_hosts=("api.example.com",))
    validate_request_url("https://api.example.com/openapi.json", policy)

Notes:
    - Host allowlist matching is case-insensitive exact hostname match.
    - ``allow_http`` is for non-production/dev only; HTTPS is the default.
    - Oversized responses are rejected, not truncated, so callers fail loudly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet, Iterable, Optional
from urllib.parse import urlparse

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024


class RequestSafetyError(ValueError):
    """URL or payload policy violation."""


class PayloadTooLargeError(RequestSafetyError):
    """Response exceeded ``max_response_bytes``."""


@dataclass(frozen=True)
class RequestSafetyPolicy:
    """Operator-configured request safety for the OpenAPI adapter."""

    allowed_hosts: FrozenSet[str] = frozenset()
    allow_http: bool = False
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    @classmethod
    def from_cli(
        cls,
        *,
        allowed_hosts: Optional[str] = None,
        allow_http: bool = False,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> "RequestSafetyPolicy":
        return cls(
            allowed_hosts=parse_allowed_hosts(allowed_hosts),
            allow_http=bool(allow_http),
            timeout_seconds=float(timeout_seconds),
            max_response_bytes=int(max_response_bytes),
        )


def parse_allowed_hosts(raw: Optional[str]) -> FrozenSet[str]:
    """Parse a comma-separated host list into a lowercase frozenset."""
    if not raw:
        return frozenset()
    hosts = []
    for part in raw.split(","):
        host = part.strip().lower().rstrip(".")
        if host:
            hosts.append(host)
    return frozenset(hosts)


def validate_request_url(url: str, policy: RequestSafetyPolicy) -> None:
    """Reject disallowed schemes, empty hosts, and hosts outside the allowlist.

    Args:
        url: Absolute URL to validate (base_url or OpenAPI URL)
        policy: Active safety policy

    Raises:
        RequestSafetyError: when the URL is not permitted
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower().rstrip(".")

    if scheme not in {"http", "https"}:
        raise RequestSafetyError(
            f"URL scheme '{scheme or 'missing'}' is not allowed; use https://"
        )
    if scheme == "http" and not policy.allow_http:
        raise RequestSafetyError(
            "http:// is not allowed; pass --allow-http for non-production use"
        )
    if not host:
        raise RequestSafetyError("URL is missing a hostname")
    if parsed.username or parsed.password:
        raise RequestSafetyError("URLs with embedded credentials are not allowed")
    if policy.allowed_hosts and host not in policy.allowed_hosts:
        raise RequestSafetyError(
            f"Host '{host}' is not in the allowed-hosts list"
        )


def read_bytes_bounded(chunks: Iterable[bytes], max_bytes: int) -> bytes:
    """Assemble chunks, raising if the total exceeds ``max_bytes``."""
    parts: list[bytes] = []
    total = 0
    for chunk in chunks:
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise PayloadTooLargeError(
                f"Response exceeded max-response-bytes ({max_bytes})"
            )
        parts.append(chunk)
    return b"".join(parts)


def read_response_bounded(response: Any, max_bytes: int) -> bytes:
    """Read an httpx response body with a hard size cap.

    Checks ``Content-Length`` first so oversized bodies are rejected before
    they are pulled into memory, then streams the remainder with the same cap.
    """
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            declared = int(content_length)
        except (TypeError, ValueError):
            declared = 0
        if declared > max_bytes:
            raise PayloadTooLargeError(
                f"Content-Length {declared} exceeds max-response-bytes ({max_bytes})"
            )
    return read_bytes_bounded(response.iter_bytes(), max_bytes)


def build_httpx_event_hooks(policy: RequestSafetyPolicy) -> dict[str, list[Any]]:
    """Return httpx event hooks that re-validate every request, including redirects."""

    def on_request(request: Any) -> None:
        validate_request_url(str(request.url), policy)

    def on_response(response: Any) -> None:
        content_length = response.headers.get("content-length")
        if not content_length:
            return
        try:
            declared = int(content_length)
        except (TypeError, ValueError):
            return
        if declared > policy.max_response_bytes:
            raise PayloadTooLargeError(
                f"Content-Length {declared} exceeds max-response-bytes "
                f"({policy.max_response_bytes})"
            )

    return {"request": [on_request], "response": [on_response]}
