"""
Motet — Stateful runner supervisor unit tests (ADR-0106 Slice B)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

These tests pin the wire contract for the in-container supervisor
that powers ``lifetime: stateful`` runners. We spawn the supervisor as a
real subprocess on the host (UNIX sockets work the same way on the dev
box and inside the container), point it at a generated module that
defines ``handle(params)``, and exercise the protocol directly.

Pinning the supervisor at the wire level here lets us iterate on the
manager's docker-API plumbing in ``test_workspace_container_manager_stateful``
with confidence the supervisor itself does what the manager promises.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from motet.core.skills import _warm_supervisor as supervisor_module


# ---------------------------------------------------------------------------
# Wire helpers (mirror the supervisor's own framing)
# ---------------------------------------------------------------------------

_LENGTH = struct.Struct(">I")


def _request(params: Dict[str, Any], request_id: str = "req-1") -> bytes:
    return json.dumps({"id": request_id, "params": params}).encode("utf-8")


def _send_recv(sock_path: str, payload: bytes, timeout: float = 5.0) -> bytes:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(sock_path)
    try:
        s.sendall(_LENGTH.pack(len(payload)) + payload)
        header = s.recv(4)
        assert len(header) == 4, "supervisor returned a short header"
        (length,) = _LENGTH.unpack(header)
        body = bytearray()
        while len(body) < length:
            chunk = s.recv(length - len(body))
            if not chunk:
                raise RuntimeError("supervisor closed before full body")
            body.extend(chunk)
        return bytes(body)
    finally:
        s.close()


def _wait_for_marker(marker: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"supervisor did not write marker {marker} within {timeout}s")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def warm_runtime(tmp_path: Path) -> Tuple[Path, Path, Path, str, subprocess.Popen[bytes]]:
    """Spin up the supervisor against a generated counter module.

    Yields ``(module, socket, marker, sock_path, process)``. The
    counter module exposes ``handle(params)`` whose return depends on
    module-level state, which is the whole point of stateful mode.

    We deliberately *don't* use ``importlib.resources`` for the
    supervisor source path here — the supervisor is meant to run as a
    standalone script inside a container, so we invoke it the same way
    here on the host: ``python supervisor.py --module ... --socket ...``

    AF_UNIX path length is capped at ~104 chars on macOS, well below
    pytest's ``tmp_path``. We park the socket and marker in a short
    ``/tmp/motet-warm-tests-<pid>-<id>/`` directory so the supervisor
    can ``bind()`` regardless of the test runner's tmpdir layout.
    """
    import tempfile
    import uuid

    short_root = Path(tempfile.gettempdir()) / f"motet-warm-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    short_root.mkdir(parents=True, exist_ok=True)

    module = tmp_path / "skill_module.py"
    module.write_text(
        "from typing import Any, Dict\n"
        "_count = 0\n"
        "_history: list = []\n"
        "def handle(params: Dict[str, Any]) -> Dict[str, Any]:\n"
        "    global _count\n"
        "    label = str(params.get('label') or f'call-{_count + 1}')\n"
        "    if params.get('boom'):\n"
        "        raise RuntimeError('intentional crash')\n"
        "    if params.get('print_to_stdout'):\n"
        "        print('hello from handle')\n"
        "    _count += 1\n"
        "    _history.append(label)\n"
        "    return {'label': label, 'count': _count, 'history': list(_history)}\n",
        encoding="utf-8",
    )

    sock = short_root / "w.sock"
    marker = short_root / ".bootstrapped"

    supervisor_path = Path(supervisor_module.__file__).resolve()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(supervisor_path),
            "--module",
            str(module),
            "--socket",
            str(sock),
            "--marker",
            str(marker),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )

    try:
        try:
            _wait_for_marker(marker, timeout=5.0)
        except AssertionError:
            stdout = b""
            stderr = b""
            if proc.stdout:
                try:
                    stdout = proc.stdout.read(8192) or b""
                except Exception:
                    pass
            if proc.stderr:
                try:
                    stderr = proc.stderr.read(8192) or b""
                except Exception:
                    pass
            raise AssertionError(
                f"supervisor failed to start; stdout={stdout!r}; stderr={stderr!r}"
            )
        yield module, sock, marker, str(sock), proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)
        try:
            if sock.exists():
                sock.unlink()
            if marker.exists():
                marker.unlink()
            short_root.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_supervisor_round_trips_simple_request(
    warm_runtime: Tuple[Path, Path, Path, str, subprocess.Popen[bytes]],
) -> None:
    _module, _sock, _marker, sock_path, _proc = warm_runtime
    raw = _send_recv(sock_path, _request({"label": "first"}))
    envelope = json.loads(raw)
    assert envelope["id"] == "req-1"
    assert envelope["ok"] is True
    assert envelope["result"] == {
        "label": "first",
        "count": 1,
        "history": ["first"],
    }
    assert envelope["stdout"] == ""
    assert envelope["stderr"] == ""


def test_supervisor_persists_state_across_calls(
    warm_runtime: Tuple[Path, Path, Path, str, subprocess.Popen[bytes]],
) -> None:
    _module, _sock, _marker, sock_path, _proc = warm_runtime

    first = json.loads(_send_recv(sock_path, _request({"label": "a"}, "r1")))
    second = json.loads(_send_recv(sock_path, _request({"label": "b"}, "r2")))
    third = json.loads(_send_recv(sock_path, _request({"label": "c"}, "r3")))

    assert first["result"]["count"] == 1
    assert second["result"]["count"] == 2
    assert third["result"]["count"] == 3
    assert third["result"]["history"] == ["a", "b", "c"]
    assert third["id"] == "r3"


def test_supervisor_captures_stdout_from_handle(
    warm_runtime: Tuple[Path, Path, Path, str, subprocess.Popen[bytes]],
) -> None:
    _module, _sock, _marker, sock_path, _proc = warm_runtime
    envelope = json.loads(
        _send_recv(sock_path, _request({"label": "x", "print_to_stdout": True}))
    )
    assert envelope["ok"] is True
    assert "hello from handle" in envelope["stdout"]


def test_supervisor_returns_error_envelope_on_handle_exception_and_stays_up(
    warm_runtime: Tuple[Path, Path, Path, str, subprocess.Popen[bytes]],
) -> None:
    _module, _sock, _marker, sock_path, _proc = warm_runtime

    crash = json.loads(_send_recv(sock_path, _request({"boom": True}, "rc")))
    assert crash["ok"] is False
    assert crash["id"] == "rc"
    assert "RuntimeError" in crash["error"]
    assert "intentional crash" in crash["error"]
    assert "Traceback" in crash["traceback"]

    # Supervisor must stay up: a successful follow-up call still works.
    follow = json.loads(_send_recv(sock_path, _request({"label": "after-crash"}, "rf")))
    assert follow["ok"] is True
    assert follow["result"]["count"] == 1
    assert follow["result"]["label"] == "after-crash"


def test_supervisor_rejects_malformed_request(
    warm_runtime: Tuple[Path, Path, Path, str, subprocess.Popen[bytes]],
) -> None:
    _module, _sock, _marker, sock_path, _proc = warm_runtime
    # Missing "params" field; supervisor should reply with a structured
    # error rather than crash and reset state.
    bad_payload = json.dumps({"id": "rb", "not_params": {}}).encode("utf-8")
    envelope = json.loads(_send_recv(sock_path, bad_payload))
    assert envelope["ok"] is False
    assert envelope["id"] == "rb"
    assert "params" in envelope["error"].lower()


def test_supervisor_fails_fast_if_module_missing_handle(tmp_path: Path) -> None:
    """A bad module is a startup error, not a per-call error.

    Authors should see this immediately instead of getting per-call
    failures forever.
    """
    module = tmp_path / "no_handle.py"
    module.write_text("# intentionally empty -- no handle()\n", encoding="utf-8")
    sock = tmp_path / "warm.sock"
    marker = tmp_path / ".bootstrapped"

    supervisor_path = Path(supervisor_module.__file__).resolve()
    proc = subprocess.run(
        [
            sys.executable,
            str(supervisor_path),
            "--module",
            str(module),
            "--socket",
            str(sock),
            "--marker",
            str(marker),
        ],
        capture_output=True,
        timeout=5.0,
    )
    assert proc.returncode != 0
    assert b"handle" in proc.stderr
    assert not marker.exists()
