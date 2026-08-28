"""
Motet - Process control bridge HTTP client (container -> host)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-30

Description:
    Calls the host process control bridge (list / terminate) started by
    ``motet-cli device start --process-control-bridge``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from ..protocol import err


def _base_url() -> str:
    return (os.getenv("MOTET_PROCESS_CONTROL_BRIDGE_URL") or "").strip().rstrip("/")


def _token() -> str:
    return (os.getenv("MOTET_PROCESS_CONTROL_BRIDGE_TOKEN") or "").strip()


def _post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    base = _base_url()
    if not base:
        return err(
            "MOTET_PROCESS_CONTROL_BRIDGE_URL is not set; use "
            "`motet-cli device start --process-control-bridge` with host cwd allowlist."
        )
    tok = _token()
    if not tok:
        return err("MOTET_PROCESS_CONTROL_BRIDGE_URL set but token missing")

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = f"{base}{path}"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Content-Length", str(len(body)))
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace")[:800]
        return err(f"process control bridge HTTP {e.code}: {msg}")
    except urllib.error.URLError as e:
        return err(f"process control bridge connection failed: {e}")
    except Exception as exc:
        return err(str(exc))

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return err("process control bridge returned non-JSON")

    if not isinstance(data, dict):
        return err("invalid bridge response")
    if data.get("error"):
        return err(str(data["error"]))
    return data


def list_processes_via_bridge(limit: Optional[int]) -> Dict[str, Any]:
    p: Dict[str, Any] = {}
    if limit is not None:
        p["limit"] = int(limit)
    return _post("/list", p)


def terminate_via_bridge(pid: int, signal_name: Optional[str]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"pid": int(pid)}
    if signal_name:
        payload["signal"] = str(signal_name)
    return _post("/terminate", payload)


__all__ = ["list_processes_via_bridge", "terminate_via_bridge"]
