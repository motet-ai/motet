"""
Motet - OpenAPI Adapter Request Safety Tests (ADR-0060 / #69)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-14

Description:
    Unit tests for HTTPS-by-default, host allowlisting, timeout/size policy
    construction, and bounded response reads used by the OpenAPI MCP adapter.

Dependencies:
    - pytest
    - motet.core.tools.mcp_adapters.openapi_request_safety

Usage:
    pytest tests/unit/core/tools/mcp_adapters/test_openapi_request_safety.py -q
"""

from __future__ import annotations

from typing import Dict, Iterator, Optional

import pytest

from motet.core.tools.mcp_adapters.openapi_request_safety import (
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    PayloadTooLargeError,
    RequestSafetyError,
    RequestSafetyPolicy,
    parse_allowed_hosts,
    read_bytes_bounded,
    read_response_bounded,
    validate_request_url,
)


def test_https_is_allowed_by_default() -> None:
    validate_request_url(
        "https://api.example.com/openapi.json", RequestSafetyPolicy()
    )


def test_http_rejected_without_explicit_allow() -> None:
    with pytest.raises(RequestSafetyError, match="http:// is not allowed"):
        validate_request_url("http://api.example.com/openapi.json", RequestSafetyPolicy())


def test_http_allowed_when_flag_set() -> None:
    policy = RequestSafetyPolicy(allow_http=True)
    validate_request_url("http://localhost:8080/openapi.json", policy)


def test_non_http_schemes_rejected() -> None:
    with pytest.raises(RequestSafetyError, match="file"):
        validate_request_url("file:///etc/passwd", RequestSafetyPolicy())


def test_allowlist_rejects_unknown_host() -> None:
    policy = RequestSafetyPolicy(allowed_hosts=frozenset({"api.example.com"}))
    with pytest.raises(RequestSafetyError, match="not in the allowed-hosts"):
        validate_request_url("https://evil.example.net/openapi.json", policy)


def test_allowlist_accepts_listed_host() -> None:
    policy = RequestSafetyPolicy(allowed_hosts=frozenset({"api.example.com"}))
    validate_request_url("https://api.example.com/v1", policy)


def test_allowlist_is_case_insensitive() -> None:
    policy = RequestSafetyPolicy.from_cli(allowed_hosts="API.Example.COM")
    validate_request_url("https://api.example.com/v1", policy)


def test_embedded_credentials_rejected() -> None:
    with pytest.raises(RequestSafetyError, match="embedded credentials"):
        validate_request_url(
            "https://user:token@api.example.com/openapi.json",
            RequestSafetyPolicy(),
        )


def test_parse_allowed_hosts_splits_and_strips() -> None:
    assert parse_allowed_hosts(" api.example.com, other.example.com. ") == frozenset(
        {"api.example.com", "other.example.com"}
    )


def test_from_cli_defaults() -> None:
    policy = RequestSafetyPolicy.from_cli()
    assert policy.timeout_seconds == DEFAULT_TIMEOUT_SECONDS
    assert policy.max_response_bytes == DEFAULT_MAX_RESPONSE_BYTES
    assert policy.allowed_hosts == frozenset()
    assert policy.allow_http is False


def test_read_bytes_bounded_rejects_oversize() -> None:
    with pytest.raises(PayloadTooLargeError):
        read_bytes_bounded([b"abc", b"def"], max_bytes=4)


def test_read_bytes_bounded_accepts_exact_limit() -> None:
    assert read_bytes_bounded([b"ab", b"cd"], max_bytes=4) == b"abcd"


class _FakeResponse:
    def __init__(self, body: bytes, headers: Optional[Dict[str, str]] = None) -> None:
        self.headers = headers or {}
        self._body = body

    def iter_bytes(self) -> Iterator[bytes]:
        yield self._body


def test_read_response_bounded_rejects_content_length() -> None:
    response = _FakeResponse(b"ignored", headers={"content-length": "9999"})
    with pytest.raises(PayloadTooLargeError, match="Content-Length"):
        read_response_bounded(response, max_bytes=10)


def test_read_response_bounded_streams_body() -> None:
    response = _FakeResponse(b'{"ok": true}')
    assert read_response_bounded(response, max_bytes=100) == b'{"ok": true}'
