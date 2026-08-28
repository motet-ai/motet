"""
Motet — Warm Session Client

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Per-dispatch bridge that runs *inside* the warm workspace container.
    The:class:`WorkspaceContainerManager` invokes this script via
    ``docker exec``; the script connects to the supervisor's UNIX socket,
    exchanges one length-prefixed JSON message, then prints the response
    on stdout for the manager to demux.

    Why this exists: the Docker Engine ``/exec`` endpoint cannot accept
    request bytes on stdin without a connection-upgrade ("hijacked")
    transport, which our minimal HTTP client deliberately doesn't speak
    (see ``docker_client.docker_exec_start`` — Slice A made the
    NotImplementedError loud). Routing the call through stdout instead
    keeps the manager's transport surface tiny.

    Inputs (env, both required):
    MOTET_WARM_REQUEST_B64  base64-encoded JSON request envelope
    MOTET_WARM_SOCKET       UNIX socket path inside the container

    Output:
    A single JSON envelope is written to stdout. Exit code is 0 when
    the supervisor returned ``ok: true``, 1 when ``ok: false``, and 2
    on any local IPC failure (so the manager can distinguish supervisor
    errors from infrastructure errors).

Notes:
    - **Stdlib only.** Same constraint as the supervisor — must run on
      ``python-minimal`` images without a ``pip install`` step.
    - **Single-shot.** Each dispatch is its own ``docker exec`` and its
      own client process. The supervisor reuses its imported module
      between calls; the client does not need to.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
from typing import Any, Dict

_LENGTH_HEADER = struct.Struct(">I")
_MAX_FRAME_BYTES = 8 * 1024 * 1024


def _read_exact(conn: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return bytes(buf)
        buf.extend(chunk)
    return bytes(buf)


def _read_frame(conn: socket.socket) -> bytes:
    header = _read_exact(conn, _LENGTH_HEADER.size)
    if len(header) != _LENGTH_HEADER.size:
        raise IOError("warm client: short read on response header")
    (length,) = _LENGTH_HEADER.unpack(header)
    if length == 0:
        return b""
    if length > _MAX_FRAME_BYTES:
        raise IOError(
            f"warm client: response too large: {length} > {_MAX_FRAME_BYTES} bytes"
        )
    body = _read_exact(conn, length)
    if len(body) != length:
        raise IOError(
            f"warm client: short read on response body: got {len(body)} of {length} bytes"
        )
    return body


def _write_frame(conn: socket.socket, payload: bytes) -> None:
    if len(payload) > _MAX_FRAME_BYTES:
        raise IOError(
            f"warm client: request too large: {len(payload)} > {_MAX_FRAME_BYTES} bytes"
        )
    conn.sendall(_LENGTH_HEADER.pack(len(payload)) + payload)


def _emit_local_error(message: str) -> int:
    envelope: Dict[str, Any] = {
        "id": "",
        "ok": False,
        "error": message,
        "traceback": "",
        "stdout": "",
        "stderr": "",
        "transport_error": True,
    }
    sys.stdout.write(json.dumps(envelope))
    sys.stdout.flush()
    return 2


def main() -> int:
    request_b64 = os.environ.get("MOTET_WARM_REQUEST_B64", "")
    socket_path = os.environ.get("MOTET_WARM_SOCKET", "")
    if not request_b64:
        return _emit_local_error("MOTET_WARM_REQUEST_B64 is empty")
    if not socket_path:
        return _emit_local_error("MOTET_WARM_SOCKET is empty")

    try:
        raw_request = base64.b64decode(request_b64.encode("ascii"))
    except Exception as exc:
        return _emit_local_error(f"MOTET_WARM_REQUEST_B64 is not valid base64: {exc}")

    try:
        # Validate it parses; the supervisor will validate shape.
        json.loads(raw_request.decode("utf-8"))
    except Exception as exc:
        return _emit_local_error(f"request payload is not valid JSON: {exc}")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(60.0)
        sock.connect(socket_path)
        _write_frame(sock, raw_request)
        response = _read_frame(sock)
    except Exception as exc:
        try:
            sock.close()
        except OSError:
            pass
        return _emit_local_error(f"warm supervisor IPC failed: {exc}")

    try:
        sock.close()
    except OSError:
        pass

    sys.stdout.write(response.decode("utf-8", errors="replace"))
    sys.stdout.flush()

    try:
        envelope = json.loads(response.decode("utf-8"))
        return 0 if envelope.get("ok") else 1
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
