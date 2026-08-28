"""
Motet - OpenAI Compatible Errors

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    OpenAI-shaped error envelopes for the compatibility facade.

    Clients parse the ``{"error": {...}}`` body to decide whether to retry, so
    facade failures must use OpenAI's shape rather than FastAPI's ``detail``.
    Upstream provider text is sanitized before it reaches the client so provider
    credentials and internal identifiers are never echoed.

Dependencies:
    - fastapi: HTTP status codes and response construction

Usage:
    from motet.interfaces.api.openai_compat.errors import FacadeError

    raise FacadeError(400, "model is required", code="missing_model")

Notes:
    - FacadeError is converted to a JSONResponse by the router exception handler
    - Mid-stream failures reuse error_payload inside the SSE body
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

_SECRET_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|sa_[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,})"
)


def sanitize_error_text(message: str, *, limit: int = 500) -> str:
    """Strip credential-looking substrings and cap length for client display."""
    cleaned = _SECRET_PATTERN.sub("[redacted]", str(message or ""))
    if len(cleaned) > limit:
        return cleaned[:limit] + "…"
    return cleaned


def error_payload(
    message: str,
    *,
    error_type: str = "invalid_request_error",
    code: Optional[str] = None,
    param: Optional[str] = None,
) -> Dict[str, Any]:
    """Build an OpenAI-shaped error body."""
    return {
        "error": {
            "message": sanitize_error_text(message),
            "type": error_type,
            "param": param,
            "code": code,
        }
    }


class FacadeError(Exception):
    """An error that should surface to the client in OpenAI error shape."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        error_type: Optional[str] = None,
        code: Optional[str] = None,
        param: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_type = error_type or _default_error_type(status_code)
        self.code = code
        self.param = param

    def to_payload(self) -> Dict[str, Any]:
        """Render this error as an OpenAI error body."""
        return error_payload(
            self.message,
            error_type=self.error_type,
            code=self.code,
            param=self.param,
        )


def _default_error_type(status_code: int) -> str:
    if status_code in (401, 403):
        return "authentication_error" if status_code == 401 else "permission_error"
    if status_code == 404:
        return "not_found_error"
    if status_code == 429:
        return "rate_limit_error"
    if status_code >= 500:
        return "api_error"
    return "invalid_request_error"


__all__ = ["FacadeError", "error_payload", "sanitize_error_text"]
