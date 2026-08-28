"""
Motet - Host shell exec bridge for Docker local workers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-30

Description:
    Loopback HTTP server that runs subprocesses on the host with argv-only invocation (no shell),
    cwd constrained to MOTET_SHELL_BRIDGE_CWD_ALLOWLIST, optional basename allowlist for argv[0].

Dependencies:
    - stdlib only + host PATH for executed binaries

Usage:
    MOTET_SHELL_BRIDGE_TOKEN=... MOTET_SHELL_BRIDGE_CWD_ALLOWLIST=/safe/dir python -m motet_sdk.cli.shell_bridge

Notes:
    - First stdout line is the TCP port.
    - POST /exec with JSON {"argv": [...], "cwd": "...", "timeout_seconds": optional}.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, List, Optional, Set

_TOKEN = ""
_DEFAULT_MAX_OUT = 262_144
_DEFAULT_MAX_TIMEOUT = 120
_DEFAULT_MAX_ARGV = 64
_MAX_BODY = 65_536


def _cwd_roots() -> List[str]:
    raw = os.getenv("MOTET_SHELL_BRIDGE_CWD_ALLOWLIST", "").strip()
    if not raw:
        return []
    return [os.path.abspath(p.strip()) for p in raw.split(",") if p.strip()]


def _command_allowlist() -> Optional[Set[str]]:
    raw = os.getenv("MOTET_SHELL_BRIDGE_COMMAND_ALLOWLIST", "").strip()
    if not raw:
        return None
    return {x.strip() for x in raw.split(",") if x.strip()}


def _max_output_bytes() -> int:
    return int(os.getenv("MOTET_SHELL_EXEC_MAX_OUTPUT_BYTES") or _DEFAULT_MAX_OUT)


def _max_timeout() -> int:
    return int(os.getenv("MOTET_SHELL_BRIDGE_MAX_TIMEOUT") or _DEFAULT_MAX_TIMEOUT)


def _max_argv() -> int:
    return int(os.getenv("MOTET_SHELL_BRIDGE_MAX_ARGV") or _DEFAULT_MAX_ARGV)


def _is_under_root(path: str, roots: List[str]) -> bool:
    ap = os.path.abspath(path)
    return any(ap == r or ap.startswith(r + os.sep) for r in roots)


def _validate_request(
    data: dict[str, Any],
) -> tuple[Optional[str], Optional[List[str]], Optional[str], int]:
    """
    Return (error_msg, argv, cwd_abs, timeout_sec) — argv/cwd None if error.
    """
    roots = _cwd_roots()
    if not roots:
        return "host bridge missing MOTET_SHELL_BRIDGE_CWD_ALLOWLIST", None, None, 0

    argv = data.get("argv")
    if not isinstance(argv, list) or len(argv) == 0:
        return "argv must be a non-empty list of strings", None, None, 0
    if len(argv) > _max_argv():
        return f"too many argv entries (max {_max_argv()})", None, None, 0
    out_argv: List[str] = []
    for i, a in enumerate(argv):
        if not isinstance(a, str):
            return f"argv[{i}] must be a string", None, None, 0
        if "\x00" in a:
            return "argv contains null byte", None, None, 0
        out_argv.append(a)

    cmd_check = _command_allowlist()
    if cmd_check is not None:
        base = os.path.basename(out_argv[0])
        if base not in cmd_check and out_argv[0] not in cmd_check:
            return f"command not in MOTET_SHELL_BRIDGE_COMMAND_ALLOWLIST: {base!r}", None, None, 0

    cwd_raw = data.get("cwd")
    if cwd_raw is None or str(cwd_raw).strip() == "":
        return "cwd is required and must be under MOTET_SHELL_BRIDGE_CWD_ALLOWLIST", None, None, 0
    if not isinstance(cwd_raw, str):
        return "cwd must be a string", None, None, 0
    cwd_abs = os.path.abspath(os.path.expanduser(cwd_raw.strip()))
    if not _is_under_root(cwd_abs, roots):
        return "cwd not in MOTET_SHELL_BRIDGE_CWD_ALLOWLIST", None, None, 0
    if not os.path.isdir(cwd_abs):
        return "cwd is not a directory", None, None, 0

    timeout = data.get("timeout_seconds")
    if timeout is None:
        timeout = 30
    try:
        tsec = int(timeout)
    except (TypeError, ValueError):
        return "timeout_seconds must be an integer", None, None, 0
    mt = _max_timeout()
    if tsec < 1 or tsec > mt:
        return f"timeout_seconds must be between 1 and {mt}", None, None, 0

    return None, out_argv, cwd_abs, tsec


class ShellBridgeHandler(BaseHTTPRequestHandler):
    server_version = "MotetShellBridge/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _unauthorized(self) -> None:
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":"unauthorized"}')

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        global _TOKEN
        auth = (self.headers.get("Authorization") or "").strip()
        return auth == f"Bearer {_TOKEN}".strip()

    def do_POST(self) -> None:
        path_only = self.path.split("?", 1)[0]
        if path_only not in ("/exec", "/exec/"):
            self._json(404, {"error": "not found"})
            return
        if not self._auth_ok():
            self._unauthorized()
            return

        length_raw = self.headers.get("Content-Length")
        if not length_raw:
            self._json(411, {"error": "Content-Length required"})
            return
        try:
            n = int(length_raw)
        except ValueError:
            self._json(400, {"error": "invalid Content-Length"})
            return
        if n > _MAX_BODY:
            self._json(413, {"error": "body too large"})
            return
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "invalid JSON"})
            return
        if not isinstance(data, dict):
            self._json(400, {"error": "body must be a JSON object"})
            return

        err, argv, cwd_abs, tsec = _validate_request(data)
        if err:
            self._json(400, {"error": err})
            return
        assert argv is not None and cwd_abs is not None

        cap = _max_output_bytes()
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd_abs,
                capture_output=True,
                text=True,
                timeout=tsec,
                shell=False,
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired:
            self._json(200, {
                "returncode": -1,
                "stdout": "",
                "stderr": "timeout",
                "timed_out": True,
                "stdout_truncated": False,
                "stderr_truncated": False,
            })
            return
        except Exception as e:
            self._json(500, {"error": str(e)})
            return

        out = proc.stdout or ""
        err_s = proc.stderr or ""
        ot = len(out.encode("utf-8")) > cap
        et = len(err_s.encode("utf-8")) > cap
        if ot:
            out = out.encode("utf-8")[:cap].decode("utf-8", errors="replace")
        if et:
            err_s = err_s.encode("utf-8")[:cap].decode("utf-8", errors="replace")

        self._json(200, {
            "returncode": proc.returncode,
            "stdout": out,
            "stderr": err_s,
            "timed_out": False,
            "stdout_truncated": ot,
            "stderr_truncated": et,
        })


def main() -> None:
    global _TOKEN
    raw_token = os.getenv("MOTET_SHELL_BRIDGE_TOKEN", "").strip()
    if not raw_token:
        print("MOTET_SHELL_BRIDGE_TOKEN missing", file=sys.stderr)
        sys.exit(2)
    if not _cwd_roots():
        print("MOTET_SHELL_BRIDGE_CWD_ALLOWLIST missing or empty", file=sys.stderr)
        sys.exit(2)
    _TOKEN = raw_token

    httpd = HTTPServer(("127.0.0.1", 0), ShellBridgeHandler)
    port = httpd.server_address[1]
    print(port, flush=True)

    def _stop(*_args: object) -> None:
        httpd.shutdown()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
