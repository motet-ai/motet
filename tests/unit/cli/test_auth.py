"""
Motet - CLI Authentication Tests

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Unit tests for CLI authentication helper (_auth.py).
    Tests JWT, service account, and header-based authentication.

Dependencies:
    - pytest: Testing framework
    - unittest.mock: Mocking utilities
    - pathlib: File path handling

Usage:
    pytest tests/unit/cli/test_auth.py -v

Notes:
    - Part of ADR-0055 Week 2-3: CLI JWT Support
"""

import os
import json
import pytest
from pathlib import Path
from unittest.mock import patch, mock_open
from tempfile import TemporaryDirectory

from motet.cli._auth import (
    get_api_headers,
    store_credentials,
    clear_credentials,
    get_stored_token,
    get_credentials_path
)


@pytest.fixture
def temp_credentials_dir():
    """Create a temporary directory for credentials testing."""
    with TemporaryDirectory() as tmpdir:
        with patch('motet_sdk.cli._auth.get_credentials_path') as mock_path:
            mock_path.return_value = Path(tmpdir) / ".motet" / "credentials.json"
            yield tmpdir


@pytest.fixture
def clean_env():
    """Clean environment variables before and after test."""
    # Save original values
    original = {}
    for key in ['MOTET_JWT_TOKEN', 'MOTET_SERVICE_ACCOUNT_TOKEN', 
                'MOTET_PRINCIPAL_ID', 'MOTET_TENANT_ID', 
                'MOTET_API_KEY', 'MOTET_API_KEY', 'MOTET_PRINCIPAL_ID', 'MOTET_TENANT_ID']:
        original[key] = os.environ.get(key)
        if key in os.environ:
            del os.environ[key]
    
    yield
    
    # Restore original values
    for key, value in original.items():
        if value is not None:
            os.environ[key] = value
        elif key in os.environ:
            del os.environ[key]


@pytest.mark.unit
def test_get_api_headers_jwt_token(clean_env):
    """Test that JWT token from environment is used."""
    os.environ['MOTET_JWT_TOKEN'] = 'test-jwt-token'
    
    headers = get_api_headers()
    
    assert headers['Authorization'] == 'Bearer test-jwt-token'
    assert 'X-Principal-Id' not in headers
    assert 'X-Tenant-Id' not in headers


@pytest.mark.unit
def test_get_api_headers_service_account_token(clean_env):
    """Test that service account token from environment is used."""
    os.environ['MOTET_SERVICE_ACCOUNT_TOKEN'] = 'sa_test_token'
    
    headers = get_api_headers()
    
    assert headers['Authorization'] == 'Bearer sa_test_token'
    assert 'X-Principal-Id' not in headers
    assert 'X-Tenant-Id' not in headers


@pytest.mark.unit
def test_get_api_headers_jwt_priority_over_service_account(clean_env):
    """Test that JWT token takes priority over service account token."""
    os.environ['MOTET_JWT_TOKEN'] = 'jwt-token'
    os.environ['MOTET_SERVICE_ACCOUNT_TOKEN'] = 'sa-token'
    
    headers = get_api_headers()
    
    assert headers['Authorization'] == 'Bearer jwt-token'
    assert 'sa-token' not in headers['Authorization']


@pytest.mark.unit
def test_get_api_headers_stored_credentials(temp_credentials_dir, clean_env):
    """Test that stored credentials are used when env vars are not set."""
    creds_path = Path(temp_credentials_dir) / ".motet" / "credentials.json"
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(creds_path, 'w') as f:
        json.dump({"jwt_token": "stored-jwt-token"}, f)
    creds_path.chmod(0o600)
    
    with patch('motet_sdk.cli._auth.get_credentials_path', return_value=creds_path):
        headers = get_api_headers()
    
    assert headers['Authorization'] == 'Bearer stored-jwt-token'


@pytest.mark.unit
def test_get_api_headers_stored_service_account(temp_credentials_dir, clean_env):
    """Test that stored service account token is used."""
    creds_path = Path(temp_credentials_dir) / ".motet" / "credentials.json"
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(creds_path, 'w') as f:
        json.dump({"service_account_token": "stored-sa-token"}, f)
    creds_path.chmod(0o600)
    
    with patch('motet_sdk.cli._auth.get_credentials_path', return_value=creds_path):
        headers = get_api_headers()
    
    assert headers['Authorization'] == 'Bearer stored-sa-token'


@pytest.mark.unit
def test_get_api_headers_fallback_to_headers(temp_credentials_dir, clean_env):
    """Test fallback to header-based auth when no tokens are available."""
    creds_path = Path(temp_credentials_dir) / ".motet" / "credentials.json"
    with patch('motet_sdk.cli._auth.get_credentials_path', return_value=creds_path):
        os.environ['MOTET_PRINCIPAL_ID'] = 'test-principal'
        os.environ['MOTET_TENANT_ID'] = 'test-tenant'

        headers = get_api_headers()

        assert headers['X-Principal-Id'] == 'test-principal'
        assert headers['X-Tenant-Id'] == 'test-tenant'
        assert 'Authorization' not in headers


@pytest.mark.unit
def test_get_api_headers_default_headers(temp_credentials_dir, clean_env):
    """Test default header values when nothing is set."""
    creds_path = Path(temp_credentials_dir) / ".motet" / "credentials.json"
    with patch('motet_sdk.cli._auth.get_credentials_path', return_value=creds_path):
        headers = get_api_headers()

        assert headers['X-Principal-Id'] == 'cli-user'
        assert headers['X-Tenant-Id'] == 'default'
        assert 'Authorization' not in headers


@pytest.mark.unit
def test_get_api_headers_api_key(clean_env):
    """Test that API key is included in headers."""
    os.environ['MOTET_API_KEY'] = 'test-api-key'
    
    headers = get_api_headers()
    
    assert headers['X-API-Key'] == 'test-api-key'


@pytest.mark.unit
def test_get_api_headers_imf_api_key_fallback(clean_env):
    """Test that MOTET_API_KEY is used as fallback."""
    os.environ['MOTET_API_KEY'] = 'motet-api-key'
    
    headers = get_api_headers()
    
    assert headers['X-API-Key'] == 'motet-api-key'


@pytest.mark.unit
def test_get_api_headers_imf_principal_tenant_fallback(temp_credentials_dir, clean_env):
    """Test that MOTET_* env vars are used as fallback."""
    creds_path = Path(temp_credentials_dir) / ".motet" / "credentials.json"
    with patch('motet_sdk.cli._auth.get_credentials_path', return_value=creds_path):
        os.environ['MOTET_PRINCIPAL_ID'] = 'imf-principal'
        os.environ['MOTET_TENANT_ID'] = 'imf-tenant'

        headers = get_api_headers()

        assert headers['X-Principal-Id'] == 'imf-principal'
        assert headers['X-Tenant-Id'] == 'imf-tenant'


@pytest.mark.unit
def test_store_credentials_jwt(temp_credentials_dir):
    """Test storing JWT token in credentials file."""
    creds_path = Path(temp_credentials_dir) / ".motet" / "credentials.json"
    
    with patch('motet_sdk.cli._auth.get_credentials_path', return_value=creds_path):
        store_credentials(jwt_token="test-jwt-token")
    
    assert creds_path.exists()
    assert creds_path.stat().st_mode & 0o777 == 0o600  # Check permissions
    
    with open(creds_path) as f:
        creds = json.load(f)
    
    assert creds['jwt_token'] == 'test-jwt-token'


@pytest.mark.unit
def test_store_credentials_service_account(temp_credentials_dir):
    """Test storing service account token in credentials file."""
    creds_path = Path(temp_credentials_dir) / ".motet" / "credentials.json"
    
    with patch('motet_sdk.cli._auth.get_credentials_path', return_value=creds_path):
        store_credentials(sa_token="sa_test_token")
    
    assert creds_path.exists()
    
    with open(creds_path) as f:
        creds = json.load(f)
    
    assert creds['service_account_token'] == 'sa_test_token'


@pytest.mark.unit
def test_store_credentials_both(temp_credentials_dir):
    """Test storing both JWT and service account token."""
    creds_path = Path(temp_credentials_dir) / ".motet" / "credentials.json"
    
    with patch('motet_sdk.cli._auth.get_credentials_path', return_value=creds_path):
        store_credentials(jwt_token="jwt-token", sa_token="sa-token")
    
    with open(creds_path) as f:
        creds = json.load(f)
    
    assert creds['jwt_token'] == 'jwt-token'
    assert creds['service_account_token'] == 'sa-token'


@pytest.mark.unit
def test_store_credentials_preserves_existing(temp_credentials_dir):
    """Test that storing new credentials preserves existing ones."""
    creds_path = Path(temp_credentials_dir) / ".motet" / "credentials.json"
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Store initial credentials
    with open(creds_path, 'w') as f:
        json.dump({"jwt_token": "existing-jwt"}, f)
    
    # Store new service account token
    with patch('motet_sdk.cli._auth.get_credentials_path', return_value=creds_path):
        store_credentials(sa_token="new-sa-token")
    
    with open(creds_path) as f:
        creds = json.load(f)
    
    assert creds['jwt_token'] == 'existing-jwt'
    assert creds['service_account_token'] == 'new-sa-token'


@pytest.mark.unit
def test_clear_credentials(temp_credentials_dir):
    """Test clearing stored credentials."""
    creds_path = Path(temp_credentials_dir) / ".motet" / "credentials.json"
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.touch()
    
    with patch('motet_sdk.cli._auth.get_credentials_path', return_value=creds_path):
        clear_credentials()
    
    assert not creds_path.exists()


@pytest.mark.unit
def test_clear_credentials_nonexistent(temp_credentials_dir):
    """Test clearing credentials when file doesn't exist."""
    creds_path = Path(temp_credentials_dir) / ".motet" / "credentials.json"
    
    with patch('motet_sdk.cli._auth.get_credentials_path', return_value=creds_path):
        # Should not raise an exception
        clear_credentials()
    
    assert not creds_path.exists()


@pytest.mark.unit
def test_get_stored_token_jwt(temp_credentials_dir):
    """Test retrieving stored JWT token."""
    creds_path = Path(temp_credentials_dir) / ".motet" / "credentials.json"
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(creds_path, 'w') as f:
        json.dump({"jwt_token": "stored-jwt"}, f)
    
    with patch('motet_sdk.cli._auth.get_credentials_path', return_value=creds_path):
        token = get_stored_token()
    
    assert token == 'stored-jwt'


@pytest.mark.unit
def test_get_stored_token_service_account(temp_credentials_dir):
    """Test retrieving stored service account token."""
    creds_path = Path(temp_credentials_dir) / ".motet" / "credentials.json"
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(creds_path, 'w') as f:
        json.dump({"service_account_token": "stored-sa"}, f)
    
    with patch('motet_sdk.cli._auth.get_credentials_path', return_value=creds_path):
        token = get_stored_token()
    
    assert token == 'stored-sa'


@pytest.mark.unit
def test_get_stored_token_prefers_jwt(temp_credentials_dir):
    """Test that JWT token is preferred over service account token."""
    creds_path = Path(temp_credentials_dir) / ".motet" / "credentials.json"
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(creds_path, 'w') as f:
        json.dump({
            "jwt_token": "jwt-token",
            "service_account_token": "sa-token"
        }, f)
    
    with patch('motet_sdk.cli._auth.get_credentials_path', return_value=creds_path):
        token = get_stored_token()
    
    assert token == 'jwt-token'


@pytest.mark.unit
def test_get_stored_token_nonexistent(temp_credentials_dir):
    """Test retrieving token when credentials file doesn't exist."""
    creds_path = Path(temp_credentials_dir) / ".motet" / "credentials.json"
    
    with patch('motet_sdk.cli._auth.get_credentials_path', return_value=creds_path):
        token = get_stored_token()
    
    assert token is None


@pytest.mark.unit
def test_get_stored_token_corrupted_file(temp_credentials_dir):
    """Test handling of corrupted credentials file."""
    creds_path = Path(temp_credentials_dir) / ".motet" / "credentials.json"
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(creds_path, 'w') as f:
        f.write("invalid json{")
    
    with patch('motet_sdk.cli._auth.get_credentials_path', return_value=creds_path):
        token = get_stored_token()
    
    assert token is None


@pytest.mark.unit
def test_get_credentials_path():
    """Test that credentials path is in home directory."""
    path = get_credentials_path()
    
    assert path.parent.name == ".motet"
    assert path.name == "credentials.json"
    assert str(path).startswith(str(Path.home()))

