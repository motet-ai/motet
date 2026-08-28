"""
Motet - CLI API Request Helper

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-27

Description:
    Shared helper for CLI commands that call the REST API. Ensures consistent
    error handling (ClickException on 4xx/5xx), optional token refresh on 401,
    and timeout behavior.

Dependencies:
    - requests: HTTP client
    - click: ClickException for user-facing errors

Usage:
    from motet_sdk.cli._api import api_request
    r = api_request("GET", f"{base}/api/v1/schedules", headers=headers)
    data = r.json()
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import click
import requests

DEFAULT_TIMEOUT_SECONDS = 30


def _headers_for_request(
    headers: Optional[dict[str, str]],
    kwargs: dict[str, Any],
) -> dict[str, str]:
    """Return request headers adjusted for the body encoding."""
    request_headers = dict(headers or {})
    if kwargs.get("files") is not None:
        # Let requests add multipart/form-data with the generated boundary.
        request_headers.pop("Content-Type", None)
    elif kwargs.get("json") is not None:
        request_headers.setdefault("Content-Type", "application/json")
    return request_headers


def _base_url_from_request_url(url: str) -> str:
    """Extract scheme + netloc from URL (e.g. https://host:8000/api/v1/x -> https://host:8000)."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _try_refresh_and_store(url: str, headers: dict[str, str], timeout: int) -> bool:
    """
    On 401, try POST /api/v1/auth/refresh with current token; if 200, store new token and return True.
    Returns False if no Bearer token, or refresh failed.
    """
    auth = (headers or {}).get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return False
    base = _base_url_from_request_url(url)
    refresh_url = urljoin(base + "/", "api/v1/auth/refresh")
    try:
        r = requests.post(refresh_url, headers=headers, timeout=timeout)
        if r.status_code != 200:
            return False
        data = r.json()
        new_token = data.get("access_token") or data.get("token")
        if not new_token:
            return False
        from ._auth import store_credentials
        store_credentials(jwt_token=new_token)
        return True
    except Exception:
        return False


def api_request(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    retry_on_401_refresh: bool = True,
    **kwargs: Any,
) -> requests.Response:
    """
    Perform an API request; raise ClickException on 4xx/5xx.

    If retry_on_401_refresh is True (default) and the response is 401 with a Bearer token,
    attempts to refresh the token via POST /api/v1/auth/refresh, store the new token,
    and retry the request once. Keeps the session alive without re-running auth login.
    """
    request_headers = _headers_for_request(headers, kwargs)
    response = requests.request(
        method, url, headers=request_headers, timeout=timeout, **kwargs
    )
    if response.status_code == 401 and retry_on_401_refresh and (headers or {}).get("Authorization", "").startswith("Bearer "):
        if _try_refresh_and_store(url, headers or {}, timeout):
            from ._auth import get_api_headers
            new_headers = _headers_for_request(get_api_headers(), kwargs)
            response = requests.request(
                method, url, headers=new_headers, timeout=timeout, **kwargs
            )
            if response.status_code < 400:
                return response
    if response.status_code >= 400:
        detail = response.text
        if response.headers.get("content-type", "").startswith("application/json"):
            try:
                data = response.json()
                detail = data.get("detail", data.get("message", response.text))
                if isinstance(detail, list):
                    detail = "; ".join(str(d) for d in detail)
                elif isinstance(detail, dict):
                    detail = str(detail)
            except Exception:
                pass
        raise click.ClickException(f"API error {response.status_code}: {detail}")
    return response


def normalize_base_url(api_url: str) -> str:
    """Strip trailing slash from API base URL."""
    return api_url.rstrip("/")


def get_default_api_url() -> str:
    """Default API URL: env MOTET_API_URL/MOTET_API_URL, then ~/.motet/config.json, then http://localhost:8000."""
    from ._config import get_default_api_url as _get
    return _get()


def api_url_option():
    """Click option for --api-url with default from setup/env."""
    return click.option(
        "--api-url",
        default=get_default_api_url,
        help="API base URL (default: from 'motet-cli setup' or MOTET_API_URL or http://localhost:8000)",
    )
