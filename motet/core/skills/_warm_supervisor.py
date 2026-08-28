"""
Motet — Stateful Runner Supervisor

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    The in-container long-lived process that powers ``lifetime: stateful`` runners.
    The:class:`WorkspaceContainerManager` ships **this exact source file**
    into the per-workspace container, then starts it via ``docker exec -d``.

    Responsibilities:
    1. Import the runner author's skill module from a fixed path
         (``/motet/skill_module.py``) so it is loaded *once*; any module-level
         state (loaded models, open connections, counters) survives across
         every dispatch into this container.
    2. Validate that the module exports a ``handle(params: dict) -> dict``
         callable. Authors that omit it get a clear startup-time failure.
    3. Listen on a UNIX socket (default ``/motet/warm.sock``) for length-
         prefixed JSON requests, hand each one to ``handle``, write a
         length-prefixed JSON response.
    4. Drop a single bootstrap marker file (``/motet/.bootstrapped``) once
         the listener is accepting connections, so the manager can poll
         that path before issuing the first dispatch instead of racing on
         supervisor startup.

    The IPC framing is intentionally trivial:

        | 4 bytes (big-endian uint32, payload length) | UTF-8 JSON payload |

    Request payload shape::

    {"id": "<uuid-or-string>", "params": {... author args... }}

    Response payload shape::

        # success
    {"id": "<echo>", "ok": true, "result": {... }, "stdout": "...", "stderr": "..."}

        # failure
    {"id": "<echo>", "ok": false, "error": "ValueError:...",
         "traceback": "...", "stdout": "...", "stderr": "..."}

Dependencies:
    Python standard library only — the supervisor must run inside
    ``python-minimal`` and other lean image stacks without extra installs.

Usage:
    Invoked by ``WorkspaceContainerManager._bootstrap_warm`` as::

        python3 -u /motet/_warm_supervisor.py \\
            --module /motet/skill_module.py \\
            --socket /motet/warm.sock \\
            --marker /motet/.bootstrapped

Notes:
    - **Single-threaded by design.** A warm container handles one request
      at a time; concurrent calls within the same conversation serialize
      here. This matches the per-conversation execution model described
      in (a conversation is sequential by nature) and keeps the
      author contract simple (no thread-safety obligations on
      ``handle``). Multi-tenant concurrency happens at the *container*
      level — different conversations get different containers.
    - **stdout / stderr capture.** Per-call output written to
      ``sys.stdout`` / ``sys.stderr`` by the user's ``handle`` is captured
      and round-tripped in the response so tool results stay legible to
      the LLM (matching the ``core.worker_exec`` envelope shape).
    - **Crash policy.** Any exception inside ``handle`` is reported as a
      structured error (``ok: false``); the supervisor stays up so the
      next call has a fresh chance. A startup failure (bad module, no
      ``handle``) writes the failure to stderr and exits non-zero so the
      manager surfaces a clear error on the very first call.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import socket
import struct
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Callable, Dict, Tuple

_LENGTH_HEADER = struct.Struct(">I")
_MAX_FRAME_BYTES = 8 * 1024 * 1024  # 8 MiB cap per request/response.


def _load_handle(module_path: str) -> Callable[[Dict[str, Any]], Any]:
    spec = importlib.util.spec_from_file_location("motet_skill_module", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import skill module from {module_path!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["motet_skill_module"] = module
    spec.loader.exec_module(module)
    handle = getattr(module, "handle", None)
    if handle is None or not callable(handle):
        raise RuntimeError(
            f"skill module {module_path!r} does not export a callable named 'handle'; "
            "stateful runners must define `def handle(params: dict) -> dict:`"
        )
    return handle


def _read_frame(conn: socket.socket) -> bytes:
    header = _read_exact(conn, _LENGTH_HEADER.size)
    if not header:
        return b""
    (length,) = _LENGTH_HEADER.unpack(header)
    if length == 0:
        return b""
    if length > _MAX_FRAME_BYTES:
        raise ValueError(
            f"warm supervisor frame too large: {length} > {_MAX_FRAME_BYTES} bytes"
        )
    return _read_exact(conn, length)


def _read_exact(conn: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return bytes(buf)
        buf.extend(chunk)
    return bytes(buf)


def _write_frame(conn: socket.socket, payload: bytes) -> None:
    if len(payload) > _MAX_FRAME_BYTES:
        raise ValueError(
            f"warm supervisor response too large: {len(payload)} > {_MAX_FRAME_BYTES} bytes"
        )
    conn.sendall(_LENGTH_HEADER.pack(len(payload)) + payload)


def _serve_one(
    conn: socket.socket, handle: Callable[[Dict[str, Any]], Any]
) -> None:
    try:
        raw = _read_frame(conn)
    except Exception as exc:
        _write_frame(
            conn,
            json.dumps(
                {
                    "id": "",
                    "ok": False,
                    "error": f"failed to read request: {exc}",
                    "traceback": traceback.format_exc(),
                    "stdout": "",
                    "stderr": "",
                }
            ).encode("utf-8"),
        )
        return

    if not raw:
        return

    try:
        request = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        _write_frame(
            conn,
            json.dumps(
                {
                    "id": "",
                    "ok": False,
                    "error": f"invalid request JSON: {exc}",
                    "traceback": traceback.format_exc(),
                    "stdout": "",
                    "stderr": "",
                }
            ).encode("utf-8"),
        )
        return

    request_id = ""
    if isinstance(request, dict):
        rid = request.get("id")
        if isinstance(rid, str):
            request_id = rid

    if not isinstance(request, dict) or not isinstance(request.get("params"), dict):
        _write_frame(
            conn,
            json.dumps(
                {
                    "id": request_id,
                    "ok": False,
                    "error": "request must be {'id': str, 'params': dict}",
                    "traceback": "",
                    "stdout": "",
                    "stderr": "",
                }
            ).encode("utf-8"),
        )
        return

    response, captured_out, captured_err = _invoke(handle, request["params"])
    response["id"] = request_id
    response["stdout"] = captured_out
    response["stderr"] = captured_err
    _write_frame(conn, json.dumps(response, default=_json_default).encode("utf-8"))


def _invoke(
    handle: Callable[[Dict[str, Any]], Any], params: Dict[str, Any]
) -> Tuple[Dict[str, Any], str, str]:
    out_buf, err_buf = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            result = handle(params)
        # ``handle`` is allowed to return any JSON-serializable value; we
        # wrap non-dict returns in {"value": ...} so the wire shape stays
        # uniform on the manager side.
        if not isinstance(result, dict):
            result = {"value": result}
        return (
            {"ok": True, "result": result},
            out_buf.getvalue(),
            err_buf.getvalue(),
        )
    except Exception as exc:
        return (
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            },
            out_buf.getvalue(),
            err_buf.getvalue(),
        )


def _json_default(value: Any) -> Any:
    # Best-effort: stringify anything the author returned that isn't
    # JSON-native so a sloppy handle() doesn't crash the response path.
    return repr(value)


def _bind_socket(socket_path: str) -> socket.socket:
    # Remove any stale socket file from a prior crashed supervisor.
    try:
        if os.path.exists(socket_path):
            os.unlink(socket_path)
    except OSError as exc:
        # If we can't unlink, bind() will fail loud below — that's fine.
        print(
            f"warm-supervisor: could not unlink stale socket {socket_path!r}: {exc}",
            file=sys.stderr,
        )

    parent = os.path.dirname(socket_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(socket_path)
    # The supervisor handles one connection at a time; a small backlog
    # smooths over the manager racing into the second exec while the
    # first response is still draining.
    srv.listen(8)
    try:
        os.chmod(socket_path, 0o600)
    except OSError:
        pass
    return srv


def _write_marker(marker_path: str) -> None:
    parent = os.path.dirname(marker_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(marker_path, "w", encoding="utf-8") as fh:
        fh.write("ready\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="motet-warm-supervisor")
    parser.add_argument("--module", required=True, help="Path to the user's skill module (.py)")
    parser.add_argument("--socket", required=True, help="UNIX socket path to listen on")
    parser.add_argument("--marker", required=True, help="Bootstrap marker file path")
    args = parser.parse_args(argv)

    try:
        handle = _load_handle(args.module)
    except Exception as exc:
        print(f"warm-supervisor: {exc}", file=sys.stderr)
        return 2

    srv = _bind_socket(args.socket)
    try:
        _write_marker(args.marker)
    except Exception as exc:
        print(
            f"warm-supervisor: failed to write marker {args.marker!r}: {exc}",
            file=sys.stderr,
        )
        srv.close()
        return 3

    print(
        f"warm-supervisor: listening on {args.socket} (module={args.module})",
        flush=True,
    )

    try:
        while True:
            conn, _ = srv.accept()
            try:
                _serve_one(conn, handle)
            except Exception as exc:
                print(
                    f"warm-supervisor: connection error: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(
            f"warm-supervisor: accept loop exited unexpectedly: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 4
    finally:
        try:
            srv.close()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
