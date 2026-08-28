"""
Unit tests for worker lifecycle backends (ADR-0067).

Tests Docker backend with mocked subprocess so no real Docker is required.
"""

from __future__ import annotations

import os
from unittest.mock import patch, MagicMock

import pytest

from motet.core.distributed.worker_lifecycle_backends import (
    DockerLifecycleBackend,
    get_lifecycle_backend,
    ENV_LIFECYCLE_BACKEND,
    DEFAULT_BACKEND,
)


class TestDockerLifecycleBackend:
    """Unit tests for DockerLifecycleBackend."""

    def test_start_worker_returns_error_when_container_not_found(self):
        """When _get_worker_container returns None, start_worker returns success=False."""
        backend = DockerLifecycleBackend()
        with patch.object(backend, "_get_worker_container", return_value=None):
            result = backend.start_worker("cloud_worker1")
        assert result["success"] is False
        assert "error" in result
        assert "container" not in result or result.get("container") is None

    def test_start_worker_returns_already_running_when_container_running(self):
        """When container is already running, returns success with note already_running."""
        backend = DockerLifecycleBackend()
        with patch.object(backend, "_get_worker_container", return_value="motet_dev-worker-1-1"):
            with patch.object(backend, "_is_container_running", return_value=True):
                result = backend.start_worker("cloud_worker1")
        assert result["success"] is True
        assert result.get("method") == "docker_start"
        assert result.get("note") == "already_running"
        assert result.get("container") == "motet_dev-worker-1-1"

    def test_start_worker_calls_docker_start_when_container_stopped(self):
        """When container is stopped, runs docker start and returns success on returncode 0."""
        backend = DockerLifecycleBackend()
        with patch.object(backend, "_get_worker_container", return_value="motet_dev-worker-1-1"):
            with patch.object(backend, "_is_container_running", return_value=False):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                    result = backend.start_worker("cloud_worker1")
        assert result["success"] is True
        assert result.get("method") == "docker_start"
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[:2] == ["docker", "start"]

    def test_start_worker_returns_error_when_docker_start_fails(self):
        """When docker start returns non-zero, returns success=False with error."""
        backend = DockerLifecycleBackend()
        with patch.object(backend, "_get_worker_container", return_value="motet_dev-worker-1-1"):
            with patch.object(backend, "_is_container_running", return_value=False):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(
                        returncode=1, stdout="", stderr="No such container"
                    )
                    result = backend.start_worker("cloud_worker1")
        assert result["success"] is False
        assert "error" in result

    def test_stop_worker_returns_error_when_container_not_found(self):
        """When container not found, stop_worker returns success=False."""
        backend = DockerLifecycleBackend()
        with patch.object(backend, "_get_worker_container", return_value=None):
            result = backend.stop_worker("cloud_worker1", timeout_seconds=30)
        assert result["success"] is False
        assert "error" in result

    def test_stop_worker_returns_already_stopped_when_not_running(self):
        """When container not running, returns success with note already_stopped."""
        backend = DockerLifecycleBackend()
        with patch.object(backend, "_get_worker_container", return_value="motet_dev-worker-1-1"):
            with patch.object(backend, "_is_container_running", return_value=False):
                result = backend.stop_worker("cloud_worker1", timeout_seconds=30)
        assert result["success"] is True
        assert result.get("note") == "already_stopped"

    def test_restart_worker_returns_error_when_container_not_found(self):
        """When container not found, restart_worker returns success=False."""
        backend = DockerLifecycleBackend()
        with patch.object(backend, "_get_worker_container", return_value=None):
            result = backend.restart_worker("cloud_worker1")
        assert result["success"] is False
        assert "error" in result

    def test_restart_worker_calls_docker_restart_and_returns_success(self):
        """When container found, runs docker restart and returns success on returncode 0."""
        backend = DockerLifecycleBackend()
        with patch.object(backend, "_get_worker_container", return_value="motet_dev-worker-1-1"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                result = backend.restart_worker("cloud_worker1")
        assert result["success"] is True
        assert result.get("method") == "docker_restart"
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[:2] == ["docker", "restart"]


class TestGetLifecycleBackend:
    """Tests for get_lifecycle_backend() factory."""

    def test_returns_docker_backend_by_default(self):
        """With MOTET_LIFECYCLE_BACKEND unset or docker, returns DockerLifecycleBackend."""
        with patch.dict(os.environ, {}, clear=False):
            if ENV_LIFECYCLE_BACKEND in os.environ:
                del os.environ[ENV_LIFECYCLE_BACKEND]
            backend = get_lifecycle_backend()
        assert isinstance(backend, DockerLifecycleBackend)

    def test_returns_docker_backend_when_explicitly_set(self):
        """When MOTET_LIFECYCLE_BACKEND=docker, returns DockerLifecycleBackend."""
        with patch.dict(os.environ, {ENV_LIFECYCLE_BACKEND: "docker"}):
            backend = get_lifecycle_backend()
        assert isinstance(backend, DockerLifecycleBackend)

    def test_returns_docker_backend_when_http_but_no_module(self):
        """When MOTET_LIFECYCLE_BACKEND=http but HttpLifecycleBackend not implemented, falls back to Docker."""
        with patch.dict(os.environ, {ENV_LIFECYCLE_BACKEND: "http"}):
            backend = get_lifecycle_backend()
        assert isinstance(backend, DockerLifecycleBackend)
