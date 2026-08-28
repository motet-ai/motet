"""
Unit tests for service account token management.

Tests service account creation, verification, revocation, and listing.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, MagicMock

from motet.core.security.service_accounts import (
    ServiceAccountManager,
    ServiceAccountToken
)


@pytest.fixture
def mock_redis():
    """Create a mock Redis client."""
    redis = Mock()
    redis.hset = Mock()
    redis.hgetall = Mock(return_value={})
    redis.expire = Mock()
    redis.sismember = Mock(return_value=False)
    redis.sadd = Mock()
    redis.delete = Mock()
    redis.scan_iter = Mock(return_value=iter([]))
    redis.exists = Mock(return_value=True)
    redis.scan = Mock(return_value=(0, []))
    redis.get = Mock(return_value=None)
    redis.set = Mock()
    return redis


@pytest.fixture
def sa_manager(mock_redis):
    """Create a ServiceAccountManager with mock Redis."""
    return ServiceAccountManager(mock_redis)


@pytest.mark.unit
def test_create_service_account(sa_manager, mock_redis):
    """Test service account creation."""
    token = sa_manager.create_service_account(
        name="ci-pipeline",
        tenant_id="acme-corp",
        motet_id="production",
        roles=["admin", "ci"],
        created_by="alice@acme.com",
        expires_days=365
    )
    
    # Verify token format
    assert token.startswith("sa_")
    assert "ci-pipeline" in token.lower()
    
    # Verify Redis operations
    assert mock_redis.hset.called
    assert mock_redis.expire.called
    
    # Get the stored data
    call_args = mock_redis.hset.call_args
    assert call_args is not None
    
    # hset can be called with (key, mapping=dict) or (key, name, value)
    if len(call_args[0]) >= 2:
        redis_key = call_args[0][0]
        stored_data = call_args[0][1] if isinstance(call_args[0][1], dict) else call_args.kwargs.get("mapping", {})
    else:
        redis_key = call_args[0][0]
        stored_data = call_args.kwargs.get("mapping", {})
    
    assert redis_key.endswith(redis_key.split("auth:service_account:", 1)[-1])
    assert "auth:service_account:sa_" in redis_key
    assert redis_key.startswith("acme-corp:auth:service_account:sa_")
    mock_redis.set.assert_called()
    locator_key, locator_tenant = mock_redis.set.call_args[0][:2]
    assert locator_key == f"motet:auth:service_account:{token}"
    assert locator_tenant == "acme-corp"
    assert stored_data["name"] == "ci-pipeline"
    assert stored_data["tenant_id"] == "acme-corp"
    assert stored_data["motet_id"] == "production"
    assert "admin" in stored_data["roles"] or '"admin"' in stored_data["roles"]


@pytest.mark.unit
def test_create_service_account_validation(sa_manager):
    """Test service account creation validation."""
    with pytest.raises(ValueError, match="name cannot be empty"):
        sa_manager.create_service_account(
            name="",
            tenant_id="acme-corp",
            motet_id="production",
            roles=["admin"],
            created_by="alice@acme.com"
        )
    
    with pytest.raises(ValueError, match="Tenant ID cannot be empty"):
        sa_manager.create_service_account(
            name="ci-pipeline",
            tenant_id="",
            motet_id="production",
            roles=["admin"],
            created_by="alice@acme.com"
        )

    with pytest.raises(ValueError, match="Motet ID cannot be empty"):
        sa_manager.create_service_account(
            name="ci-pipeline",
            tenant_id="acme-corp",
            motet_id="",
            roles=["admin"],
            created_by="alice@acme.com"
        )


@pytest.mark.unit
def test_verify_service_account_valid(sa_manager, mock_redis):
    """Test service account verification with valid token."""
    import json
    
    now = datetime.utcnow()
    expires_at = now + timedelta(days=365)
    
    # Setup mock Redis to return token data
    mock_redis.hgetall.return_value = {
        "id": "sa_20251122_abc123_ci-pipeline",
        "name": "ci-pipeline",
        "principal_id": "service-account:ci-pipeline",
        "tenant_id": "acme-corp",
        "motet_id": "production",
        "roles": json.dumps(["admin", "ci"]),
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "created_by": "alice@acme.com",
        "last_used_at": None
    }
    mock_redis.get.return_value = "acme-corp"
    
    token_meta = sa_manager.verify_service_account("sa_20251122_abc123_ci-pipeline")
    
    assert token_meta is not None
    assert token_meta.name == "ci-pipeline"
    assert token_meta.tenant_id == "acme-corp"
    assert "admin" in token_meta.roles
    assert "ci" in token_meta.roles
    
    # Verify last_used_at was updated
    assert mock_redis.hset.called
    last_call = mock_redis.hset.call_args
    assert "last_used_at" in last_call[0][1]


@pytest.mark.unit
def test_verify_service_account_force_thinking(sa_manager, mock_redis):
    """force_thinking bool/effort round-trip from Redis string storage."""
    import json

    now = datetime.utcnow()
    expires_at = now + timedelta(days=365)

    mock_redis.hgetall.return_value = {
        "id": "sa_20260729_abc123_cursor",
        "name": "cursor",
        "principal_id": "service-account:cursor",
        "tenant_id": "default",
        "motet_id": "default",
        "roles": json.dumps(["member"]),
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "created_by": "alice@acme.com",
        "last_used_at": None,
        "facade_mode": "agent",
        "allowed_models": json.dumps(["deepseek/*"]),
        "force_thinking": "True",
        "force_thinking_effort": "high",
        "agent_id": "cursor.backend",
    }
    mock_redis.get.return_value = "default"

    token_meta = sa_manager.verify_service_account("sa_20260729_abc123_cursor")

    assert token_meta is not None
    assert token_meta.force_thinking is True
    assert token_meta.force_thinking_effort == "high"
    assert token_meta.agent_id == "cursor.backend"
    assert token_meta.facade_mode == "agent"


@pytest.mark.unit
def test_verify_service_account_expired(sa_manager, mock_redis):
    """Test service account verification with expired token."""
    import json
    
    now = datetime.utcnow()
    expires_at = now - timedelta(days=1)  # Expired yesterday
    
    mock_redis.hgetall.return_value = {
        "id": "sa_20251122_abc123_ci-pipeline",
        "name": "ci-pipeline",
        "principal_id": "service-account:ci-pipeline",
        "tenant_id": "acme-corp",
        "motet_id": "production",
        "roles": json.dumps(["admin"]),
        "created_at": (now - timedelta(days=366)).isoformat(),
        "expires_at": expires_at.isoformat(),
        "created_by": "alice@acme.com"
    }
    mock_redis.get.return_value = "acme-corp"
    
    token_meta = sa_manager.verify_service_account("sa_20251122_abc123_ci-pipeline")
    
    assert token_meta is None


@pytest.mark.unit
def test_verify_service_account_revoked(sa_manager, mock_redis):
    """Test service account verification with revoked token."""
    mock_redis.sismember.return_value = True  # Token is revoked
    
    token_meta = sa_manager.verify_service_account("sa_20251122_abc123_ci-pipeline")
    
    assert token_meta is None


@pytest.mark.unit
def test_verify_service_account_not_found(sa_manager, mock_redis):
    """Test service account verification with non-existent token."""
    mock_redis.hgetall.return_value = {}  # Token not found
    
    token_meta = sa_manager.verify_service_account("sa_20251122_abc123_ci-pipeline")
    
    assert token_meta is None


@pytest.mark.unit
def test_verify_service_account_invalid_format(sa_manager):
    """Test service account verification with invalid token format."""
    token_meta = sa_manager.verify_service_account("invalid-token")
    
    assert token_meta is None


@pytest.mark.unit
def test_revoke_service_account(sa_manager, mock_redis):
    """Test service account revocation."""
    import json
    
    now = datetime.utcnow()
    expires_at = now + timedelta(days=365)
    
    # Setup mock to return valid token first
    mock_redis.hgetall.return_value = {
        "id": "sa_20251122_abc123_ci-pipeline",
        "name": "ci-pipeline",
        "principal_id": "service-account:ci-pipeline",
        "tenant_id": "acme-corp",
        "motet_id": "production",
        "roles": json.dumps(["admin"]),
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "created_by": "alice@acme.com"
    }
    mock_redis.get.return_value = "acme-corp"
    
    result = sa_manager.revoke_service_account("sa_20251122_abc123_ci-pipeline")
    
    assert result is True
    assert mock_redis.sadd.called  # Added to revocation list
    assert mock_redis.delete.called  # Deleted from storage


@pytest.mark.unit
def test_revoke_service_account_not_found(sa_manager, mock_redis):
    """Test revocation of non-existent service account."""
    mock_redis.hgetall.return_value = {}  # Token not found
    
    result = sa_manager.revoke_service_account("sa_20251122_abc123_ci-pipeline")
    
    assert result is False


@pytest.mark.unit
def test_list_service_accounts(sa_manager, mock_redis):
    """Test listing service accounts."""
    import json
    
    now = datetime.utcnow()
    expires_at = now + timedelta(days=365)
    
    # Setup mock to return token keys
    _sa_keys = [
        "acme-corp:auth:service_account:sa_20251122_abc123_ci-pipeline",
        "acme-corp:auth:service_account:sa_20251122_def456_deploy",
    ]
    mock_redis.scan_iter.side_effect = lambda match=None: iter(_sa_keys)
    
    # Setup mock to return token data
    def hgetall_side_effect(key):
        if "ci-pipeline" in key:
            return {
                "id": "sa_20251122_abc123_ci-pipeline",
                "name": "ci-pipeline",
                "principal_id": "service-account:ci-pipeline",
                "tenant_id": "acme-corp",
                "motet_id": "production",
                "roles": json.dumps(["admin"]),
                "created_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
                "created_by": "alice@acme.com"
            }
        elif "deploy" in key:
            return {
                "id": "sa_20251122_def456_deploy",
                "name": "deploy",
                "principal_id": "service-account:deploy",
                "tenant_id": "acme-corp",
                "motet_id": "production",
                "roles": json.dumps(["deploy"]),
                "created_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
                "created_by": "bob@acme.com"
            }
        return {}
    
    mock_redis.hgetall.side_effect = hgetall_side_effect
    
    accounts = sa_manager.list_service_accounts()
    
    assert len(accounts) == 2
    assert any(acc.name == "ci-pipeline" for acc in accounts)
    assert any(acc.name == "deploy" for acc in accounts)


@pytest.mark.unit
def test_list_service_accounts_filtered_by_tenant(sa_manager, mock_redis):
    """Test listing service accounts filtered by tenant."""
    import json
    
    now = datetime.utcnow()
    expires_at = now + timedelta(days=365)
    
    _sa_keys = [
        "acme-corp:auth:service_account:sa_20251122_abc123_ci-pipeline",
        "acme-corp:auth:service_account:sa_20251122_def456_deploy",
    ]
    mock_redis.scan_iter.side_effect = lambda match=None: iter(_sa_keys)
    
    def hgetall_side_effect(key):
        if "ci-pipeline" in key:
            return {
                "id": "sa_20251122_abc123_ci-pipeline",
                "name": "ci-pipeline",
                "principal_id": "service-account:ci-pipeline",
                "tenant_id": "acme-corp",
                "motet_id": "production",
                "roles": json.dumps(["admin"]),
                "created_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
                "created_by": "alice@acme.com"
            }
        elif "deploy" in key:
            return {
                "id": "sa_20251122_def456_deploy",
                "name": "deploy",
                "principal_id": "service-account:deploy",
                "tenant_id": "other-tenant",
                "motet_id": "sandbox",
                "roles": json.dumps(["deploy"]),
                "created_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
                "created_by": "bob@other.com"
            }
        return {}
    
    mock_redis.hgetall.side_effect = hgetall_side_effect
    
    accounts = sa_manager.list_service_accounts(tenant_id="acme-corp")
    
    assert len(accounts) == 1
    assert accounts[0].name == "ci-pipeline"
    assert accounts[0].tenant_id == "acme-corp"


@pytest.mark.unit
def test_list_service_accounts_filtered_by_motet(sa_manager, mock_redis):
    """Test listing service accounts filtered by motet."""
    import json
    
    now = datetime.utcnow()
    expires_at = now + timedelta(days=365)
    
    _sa_keys = [
        "acme-corp:auth:service_account:sa_20251122_abc123_ci-pipeline",
        "acme-corp:auth:service_account:sa_20251122_def456_deploy",
    ]
    mock_redis.scan_iter.side_effect = lambda match=None: iter(_sa_keys)
    
    def hgetall_side_effect(key):
        if "ci-pipeline" in key:
            return {
                "id": "sa_20251122_abc123_ci-pipeline",
                "name": "ci-pipeline",
                "principal_id": "service-account:ci-pipeline",
                "tenant_id": "acme-corp",
                "motet_id": "production",
                "roles": json.dumps(["admin"]),
                "created_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
                "created_by": "alice@acme.com"
            }
        elif "deploy" in key:
            return {
                "id": "sa_20251122_def456_deploy",
                "name": "deploy",
                "principal_id": "service-account:deploy",
                "tenant_id": "acme-corp",
                "motet_id": "staging",
                "roles": json.dumps(["deploy"]),
                "created_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
                "created_by": "bob@acme.com"
            }
        return {}
    
    mock_redis.hgetall.side_effect = hgetall_side_effect
    
    accounts = sa_manager.list_service_accounts(motet_id="production")
    
    assert len(accounts) == 1
    assert accounts[0].name == "ci-pipeline"
    assert accounts[0].motet_id == "production"


@pytest.mark.unit
def test_list_service_accounts_ignores_leftover_imf_keys(sa_manager, mock_redis):
    """Leftover imf: keys must not appear as live tokens."""
    mock_redis.scan_iter.side_effect = lambda match=None: iter(
        ["imf:auth:service_account:sa_20251122_abc123_ci-pipeline"]
    )
    mock_redis.hgetall.return_value = {}

    assert sa_manager.list_service_accounts() == []

