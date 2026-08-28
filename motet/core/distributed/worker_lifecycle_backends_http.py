"""
Motet - HTTP Worker Lifecycle Backend

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    HTTP backend for worker lifecycle (start/stop/restart). POSTs to a
    configured URL; the remote endpoint performs the actual action (e.g.
    Railway API, Docker on another host). See.

Dependencies:
    - httpx: HTTP client for POST requests

Usage:
    Set MOTET_LIFECYCLE_BACKEND=http and MOTET_LIFECYCLE_HTTP_BASE_URL.
    Backend is instantiated via get_lifecycle_backend() in worker_lifecycle_backends.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Default path if base URL has no path (ADR-0067 contract)
LIFECYCLE_PATH = "/lifecycle"


class HttpLifecycleBackend:
    """
    Lifecycle backend that delegates to a remote HTTP endpoint.
    POST body: { worker_id, action, timeout_seconds }. Response: { success, method?, error? }.
    """

    def __init__(self, base_url: str, timeout_seconds: int = 60):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._url = (
            self.base_url
            if self.base_url.endswith(LIFECYCLE_PATH)
            else f"{self.base_url}{LIFECYCLE_PATH}"
        )

    def start_worker(self, worker_id: str) -> Dict[str, Any]:
        """POST action=start to remote endpoint."""
        return self._request(worker_id, "start", timeout_seconds=30)

    def stop_worker(self, worker_id: str, timeout_seconds: int = 30) -> Dict[str, Any]:
        """POST action=stop to remote endpoint."""
        return self._request(worker_id, "stop", timeout_seconds=timeout_seconds)

    def restart_worker(self, worker_id: str) -> Dict[str, Any]:
        """POST action=restart to remote endpoint."""
        return self._request(worker_id, "restart", timeout_seconds=30)

    def _request(
        self,
        worker_id: str,
        action: str,
        timeout_seconds: int = 30,
    ) -> Dict[str, Any]:
        payload = {
            "worker_id": worker_id,
            "action": action,
            "timeout_seconds": timeout_seconds,
        }
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                resp = client.post(
                    self._url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            success = data.get("success", False)
            result = {
                "success": success,
                "method": "http",
                **{k: v for k, v in data.items() if k in ("error", "note", "container")},
            }
            if not success and "error" not in result:
                result["error"] = resp.text or f"HTTP {resp.status_code}"
            return result
        except httpx.HTTPStatusError as e:
            logger.warning(
                "lifecycle_http_backend_error",
                worker_id=worker_id,
                action=action,
                status_code=e.response.status_code,
            )
            return {
                "success": False,
                "method": "http",
                "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}",
            }
        except Exception as e:
            logger.warning(
                "lifecycle_http_backend_exception",
                worker_id=worker_id,
                action=action,
                error=str(e),
            )
            return {"success": False, "method": "http", "error": str(e)}
