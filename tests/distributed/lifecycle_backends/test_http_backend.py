"""
Unit tests for HTTP lifecycle backend (ADR-0067).
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from motet.core.distributed.worker_lifecycle_backends_http import (
    HttpLifecycleBackend,
    LIFECYCLE_PATH,
)


class TestHttpLifecycleBackend:
    def test_url_uses_lifecycle_path_when_base_has_no_path(self):
        backend = HttpLifecycleBackend("https://webhook.example.com", timeout_seconds=30)
        assert backend._url == f"https://webhook.example.com{LIFECYCLE_PATH}"

    def test_url_does_not_duplicate_path_when_base_ends_with_lifecycle(self):
        backend = HttpLifecycleBackend(
            "https://webhook.example.com/lifecycle", timeout_seconds=30
        )
        assert backend._url == "https://webhook.example.com/lifecycle"

    @patch("httpx.Client")
    def test_restart_worker_returns_success_when_post_returns_200_and_success_true(
        self, mock_client_class
    ):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"success": true, "method": "http"}'
        mock_resp.json.return_value = {"success": True, "method": "http"}
        mock_resp.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = None
        mock_client_class.return_value = mock_client

        backend = HttpLifecycleBackend("https://example.com", timeout_seconds=10)
        result = backend.restart_worker("cloud_worker1")

        assert result["success"] is True
        assert result.get("method") == "http"
        mock_client.post.assert_called_once()
        call_kw = mock_client.post.call_args[1]
        assert call_kw["json"]["worker_id"] == "cloud_worker1"
        assert call_kw["json"]["action"] == "restart"

    @patch("httpx.Client")
    def test_restart_worker_returns_failure_when_post_returns_success_false(
        self, mock_client_class
    ):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"success": false, "error": "Redeploy failed"}'
        mock_resp.json.return_value = {"success": False, "error": "Redeploy failed"}
        mock_resp.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = None
        mock_client_class.return_value = mock_client

        backend = HttpLifecycleBackend("https://example.com", timeout_seconds=10)
        result = backend.restart_worker("cloud_worker1")

        assert result["success"] is False
        assert "error" in result

    @patch("httpx.Client")
    def test_stop_worker_passes_timeout_seconds(self, mock_client_class):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"success": true}'
        mock_resp.json.return_value = {"success": True}
        mock_resp.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = None
        mock_client_class.return_value = mock_client

        backend = HttpLifecycleBackend("https://example.com", timeout_seconds=10)
        backend.stop_worker("cloud_worker1", timeout_seconds=45)

        call_kw = mock_client.post.call_args[1]
        assert call_kw["json"]["action"] == "stop"
        assert call_kw["json"]["timeout_seconds"] == 45
