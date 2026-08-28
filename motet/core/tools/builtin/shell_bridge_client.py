"""
Motet - Shell bridge HTTP client (container -> host)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-30

Description:
    POST argv/cwd to the host shell bridge (motet-cli device start --shell-exec-bridge).

Dependencies:
    - stdlib json, urllib

Notes:
    - cwd must be a host absolute path allowed by MOTET_SHELL_BRIDGE_CWD_ALLOWLIST on the bridge.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from ..protocol import err


def _base_url() -> str:
    return (os.getenv("MOTET_SHELL_BRIDGE_URL") or "").strip().rstrip("/")


def _token() -> str:
    return (os.getenv("MOTET_SHELL_BRIDGE_TOKEN") or "").strip()


def exec_via_bridge(
    argv: List[str],
    cwd: str,
    timeout_seconds: Optional[int],
) -> Dict[str, Any]:
    """Run subprocess on host via bridge; always returns a result dict (success or err-shaped)."""
    base = _base_url()
    if not base:
        return err(
            "MOTET_SHELL_BRIDGE_URL is not set; start the device with "
            "`motet-cli device start --shell-exec-bridge` and MOTET_SHELL_BRIDGE_CWD_ALLOWLIST on the host."
        )
    tok = _token()
    if not tok:
        return err(
            "MOTET_SHELL_BRIDGE_URL is set but MOTET_SHELL_BRIDGE_TOKEN is missing"
        )

    payload: Dict[str, Any] = {"argv": list(argv), "cwd": cwd}
    if timeout_seconds is not None:
        payload["timeout_seconds"] = int(timeout_seconds)

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = f"{base}/exec"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Content-Length", str(len(body)))

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace")[:800]
        return err(f"shell bridge HTTP {e.code}: {msg}")
    except urllib.error.URLError as e:
        return err(f"shell bridge connection failed: {e}")
    except Exception as exc:
        return err(str(exc))

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return err("shell bridge returned non-JSON body")

    if not isinstance(data, dict):
        return err("shell bridge returned invalid payload")

    if "error" in data:
        return err(str(data.get("error")))

    return {
        "returncode": int(data.get("returncode", -1)),
        "stdout": str(data.get("stdout") or ""),
        "stderr": str(data.get("stderr") or ""),
        "timed_out": bool(data.get("timed_out", False)),
        "stdout_truncated": bool(data.get("stdout_truncated", False)),
        "stderr_truncated": bool(data.get("stderr_truncated", False)),
    }


__all__ = ["exec_via_bridge"]
