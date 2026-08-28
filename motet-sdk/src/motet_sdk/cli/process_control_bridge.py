"""
Motet - Host process control bridge for Docker local workers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-30

Description:
    Loopback HTTP server: list host processes and terminate PIDs that fall under the same
    cwd/exe policy as MOTET_PROCESS_CONTROL_CWD_ALLOWLIST (or MOTET_SHELL_BRIDGE_CWD_ALLOWLIST).

Dependencies:
    - psutil

Usage:
    MOTET_PROCESS_CONTROL_BRIDGE_TOKEN=secret \\
    MOTET_PROCESS_CONTROL_CWD_ALLOWLIST=/proj \\
    python -m motet_sdk.cli.process_control_bridge

Notes:
    - First stdout line is the TCP port.
    - POST /list — JSON {"limit": optional}
    - POST /terminate — JSON {"pid": int, "signal": optional, default SIGTERM}
                            (signals: SIGTERM, SIGKILL, SIGINT only)
"""

from __future__ import annotations

import json
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional

_TOKEN = ""
_MAX_BODY = 32_768
_DEFAULT_LIST_LIMIT = 200
_MAX_LIST_LIMIT = 2000


def _roots() -> List[str]:
    raw = (
        os.getenv("MOTET_PROCESS_CONTROL_CWD_ALLOWLIST", "").strip()
        or os.getenv("MOTET_SHELL_BRIDGE_CWD_ALLOWLIST", "").strip()
    )
    if not raw:
        return []
    return [os.path.abspath(p.strip()) for p in raw.split(",") if p.strip()]


def _is_under_root(path: str, roots: List[str]) -> bool:
    ap = os.path.abspath(path)
    return any(ap == r or ap.startswith(r + os.sep) for r in roots)


def _proc_matches_roots(proc: Any, roots: List[str]) -> bool:
    """True if process cwd or executable path lies under an allowlisted root."""
    import psutil

    if not isinstance(proc, psutil.Process):
        return False
    cwd: Optional[str] = None
    try:
        cwd = proc.cwd()
    except Exception:
        pass
    if cwd and _is_under_root(cwd, roots):
        return True
    exe: Optional[str] = None
    try:
        exe = proc.exe()
    except Exception:
        pass
    if exe:
        if _is_under_root(exe, roots):
            return True
        exedir = os.path.dirname(exe)
        if exedir and _is_under_root(exedir, roots):
            return True
    return False


def _list_limit(data: Dict[str, Any]) -> tuple[Optional[str], int]:
    lim = data.get("limit")
    if lim is None:
        return None, _DEFAULT_LIST_LIMIT
    try:
        n = int(lim)
    except (TypeError, ValueError):
        return "limit must be an integer", _DEFAULT_LIST_LIMIT
    mx = int(os.getenv("MOTET_PROCESS_CONTROL_LIST_LIMIT") or _MAX_LIST_LIMIT)
    if n < 1 or n > mx:
        return f"limit must be between 1 and {mx}", _DEFAULT_LIST_LIMIT
    return None, n


def _normalize_signal(name: str) -> str:
    n = (name or "SIGTERM").strip().upper()
    if n in ("TERM",):
        n = "SIGTERM"
    if n in ("KILL",):
        n = "SIGKILL"
    if n in ("INT",):
        n = "SIGINT"
    if n not in ("SIGTERM", "SIGKILL", "SIGINT"):
        raise ValueError("signal must be SIGTERM, SIGKILL, or SIGINT")
    return n


def _send_signal(proc: Any, sig_name: str) -> None:
    """Cross-platform: on Windows only terminate/kill maps cleanly."""
    if sys.platform == "win32":
        if sig_name == "SIGKILL":
            proc.kill()
        else:
            proc.terminate()
        return
    if sig_name == "SIGTERM":
        proc.terminate()
    elif sig_name == "SIGKILL":
        proc.kill()
    else:
        proc.send_signal(signal.SIGINT)


class ProcessControlBridgeHandler(BaseHTTPRequestHandler):
    server_version = "MotetProcessControlBridge/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _unauthorized(self) -> None:
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":"unauthorized"}')

    def _json(self, code: int, payload: Dict[str, Any]) -> None:
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
        import psutil

        path_only = self.path.split("?", 1)[0]
        if path_only not in ("/list", "/list/", "/terminate", "/terminate/"):
            self._json(404, {"error": "not found"})
            return
        if not self._auth_ok():
            self._unauthorized()
            return

        roots = _roots()
        if not roots:
            self._json(503, {"error": "MOTET_PROCESS_CONTROL_CWD_ALLOWLIST (or shell cwd allowlist) not set"})
            return

        length_raw = self.headers.get("Content-Length")
        if not length_raw:
            data: Dict[str, Any] = {}
        else:
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

        if path_only in ("/list", "/list/"):
            err, limit = _list_limit(data)
            if err:
                self._json(400, {"error": err})
                return
            rows: List[Dict[str, Any]] = []
            try:
                for p in psutil.process_iter(["pid", "name"]):
                    try:
                        if not _proc_matches_roots(p, roots):
                            continue
                        cmdline: Any = None
                        try:
                            cmdline = p.cmdline()
                        except Exception:
                            pass
                        cmd_preview = ""
                        if cmdline and isinstance(cmdline, list):
                            joined = " ".join(str(x) for x in cmdline[:6])
                            cmd_preview = joined[:400]
                        cwd_s = ""
                        try:
                            cwd_s = p.cwd() or ""
                        except Exception:
                            pass
                        pinfo = p.info or {}
                        rows.append(
                            {
                                "pid": p.pid,
                                "name": str(pinfo.get("name") or ""),
                                "cwd": cwd_s,
                                "cmdline_preview": cmd_preview,
                            }
                        )
                        if len(rows) >= limit:
                            break
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        continue
            except Exception as e:
                self._json(500, {"error": str(e)})
                return
            self._json(200, {"processes": rows, "count": len(rows)})
            return

        # /terminate
        pid_raw = data.get("pid")
        if pid_raw is None:
            self._json(400, {"error": "pid is required"})
            return
        try:
            pid = int(pid_raw)
        except (TypeError, ValueError):
            self._json(400, {"error": "pid must be an integer"})
            return
        if pid <= 0:
            self._json(400, {"error": "invalid pid"})
            return
        if pid == os.getpid():
            self._json(400, {"error": "refusing to terminate bridge process"})
            return
        try:
            sig_name = _normalize_signal(str(data.get("signal") or "SIGTERM"))
        except ValueError as e:
            self._json(400, {"error": str(e)})
            return
        try:
            target = psutil.Process(pid)
        except psutil.NoSuchProcess:
            self._json(404, {"error": "no such process"})
            return
        except Exception as e:
            self._json(500, {"error": str(e)})
            return
        if not _proc_matches_roots(target, roots):
            self._json(403, {"error": "process not under cwd/exe allowlist"})
            return
        try:
            _send_signal(target, sig_name)
        except Exception as e:
            self._json(500, {"error": str(e)})
            return
        self._json(200, {"ok": True, "pid": pid, "signal": sig_name})


def main() -> None:
    global _TOKEN
    raw_token = os.getenv("MOTET_PROCESS_CONTROL_BRIDGE_TOKEN", "").strip()
    if not raw_token:
        print("MOTET_PROCESS_CONTROL_BRIDGE_TOKEN missing", file=sys.stderr)
        sys.exit(2)
    if not _roots():
        print(
            "MOTET_PROCESS_CONTROL_CWD_ALLOWLIST (or MOTET_SHELL_BRIDGE_CWD_ALLOWLIST) missing",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        import psutil  # noqa: F401
    except ImportError:
        print("psutil is required for process control bridge", file=sys.stderr)
        sys.exit(2)

    _TOKEN = raw_token

    httpd = HTTPServer(("127.0.0.1", 0), ProcessControlBridgeHandler)
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
