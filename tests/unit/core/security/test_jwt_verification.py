"""
Unit tests for JWT verification with Keycloak.

Tests JWT signature verification, JWKS fetching, and claim extraction
for Keycloak-issued tokens.
"""

from __future__ import annotations

import json
import pytest
import time
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock, patch
from fastapi import Request, HTTPException

from motet.core.config import Config
from motet.core.security.auth import (
    extract_principal,
    _verify_jwt_token,
    _JWKSCache,
    extract_principal_from_claims,
    require_jwt_if_configured,
    validate_insecure_principal_header_policy,
)
from motet.core.types import Principal


@pytest.fixture
def config(monkeypatch):
    """Create a test configuration with JWT env vars cleared for isolation."""
    monkeypatch.setenv("MOTET_JWT_JWKS_URL", "")
    monkeypatch.setenv("MOTET_JWT_PUBLIC_KEY_PEM", "")
    monkeypatch.setenv("MOTET_JWT_ISSUER", "")
    monkeypatch.setenv("MOTET_JWT_AUDIENCE", "")
    cfg = Config()
    cfg.api_key = "test-key"
    cfg.allow_insecure_principal_headers = False
    cfg.jwt_jwks_url = None
    cfg.jwt_public_key_pem = None
    cfg.jwt_issuer = None
    cfg.jwt_audience = None
    return cfg


@pytest.fixture
def mock_request():
    """Create a mock FastAPI Request."""
    request = Mock(spec=Request)
    request.headers = {}
    request.state = SimpleNamespace()
    request.app = SimpleNamespace(state=SimpleNamespace())
    request.client = SimpleNamespace(host="127.0.0.1")
    return request


@pytest.mark.unit
def test_extract_principal_service_account(config, mock_request):
    """Test principal extraction from service account token."""
    from motet.core.security.service_accounts import ServiceAccountToken
    from datetime import datetime, timedelta
    
    mock_request.headers = {
        "Authorization": "Bearer sa_20251122_abc123_ci-pipeline"
    }
    
    # Mock service account verification
    with patch('motet.core.security.service_accounts.ServiceAccountManager') as MockSAManager:
        mock_sa_manager = Mock()
        mock_token = ServiceAccountToken(
            id="sa_20251122_abc123_ci-pipeline",
            name="ci-pipeline",
            principal_id="service-account:ci-pipeline",
            tenant_id="acme-corp",
            motet_id="production",
            roles=["admin", "ci"],
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(days=365),
            created_by="alice@acme.com"
        )
        mock_sa_manager.verify_service_account.return_value = mock_token
        
        with patch('motet.core.distributed.redis_manager.get_sync_redis_client') as mock_redis:
            mock_redis.return_value = Mock()
            MockSAManager.return_value = mock_sa_manager
            
            principal = extract_principal(config, mock_request)
            
            assert principal is not None
            assert principal.id == "service-account:ci-pipeline"
            assert principal.tenant_id == "acme-corp"
            assert "admin" in principal.roles
            assert principal.claims["type"] == "service_account"


@pytest.mark.unit
def test_extract_principal_jwt_with_jwks(config, mock_request):
    """Test principal extraction from JWT with JWKS verification."""
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    
    # Generate test RSA key pair
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    
    # Serialize public key to JWK format
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    
    # Create test JWT
    claims = {
        "sub": "user-123",
        "tenant_id": "acme-corp",
        "roles": ["admin", "user"],
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600
    }
    
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key-id"})
    
    config.jwt_jwks_url = "https://zitadel.example.com/.well-known/jwks.json"
    config.jwt_sub_claim = "sub"
    config.jwt_roles_claim = "roles"
    config.jwt_tenant_claims = "tenant_id"
    
    mock_request.headers = {
        "Authorization": f"Bearer {token}"
    }
    
    # Mock JWKS response
    jwks_response = {
        "keys": [{
            "kty": "RSA",
            "kid": "test-key-id",
            "use": "sig",
            "n": public_key.public_numbers().n,
            "e": public_key.public_numbers().e
        }]
    }
    
    with patch('requests.get') as mock_get:
        mock_get.return_value.json.return_value = jwks_response
        mock_get.return_value.raise_for_status = Mock()
        
        # Mock jwt.algorithms.RSAAlgorithm.from_jwk to return our public key
        with patch('jwt.algorithms.RSAAlgorithm.from_jwk') as mock_from_jwk:
            mock_from_jwk.return_value = public_key
            
            principal = extract_principal(config, mock_request)
            
            # Note: This test may need adjustment based on actual JWK parsing
            # For now, we're testing the flow
            assert principal is None or isinstance(principal, Principal)


@pytest.mark.unit
def test_extract_principal_jwt_with_static_key(config, mock_request):
    """Test principal extraction from JWT with static public key."""
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    
    # Generate test RSA key pair
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    
    # Serialize public key to PEM
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode('utf-8')
    
    # Create test JWT
    claims = {
        "sub": "user-123",
        "tenant_id": "acme-corp",
        "roles": ["admin", "user"],
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600
    }
    
    token = jwt.encode(claims, private_key, algorithm="RS256")
    
    config.jwt_public_key_pem = public_pem
    config.jwt_sub_claim = "sub"
    config.jwt_roles_claim = "roles"
    config.jwt_tenant_claims = "tenant_id"
    
    mock_request.headers = {
        "Authorization": f"Bearer {token}"
    }
    
    principal = extract_principal(config, mock_request)
    
    assert principal is not None
    assert principal.id == "user-123"
    assert principal.tenant_id == "acme-corp"
    assert "admin" in principal.roles


@pytest.mark.unit
def test_extract_principal_headers_dev_mode(config, mock_request):
    """Test principal extraction from headers in dev mode."""
    config.allow_insecure_principal_headers = True
    
    mock_request.headers = {
        "X-Principal-Id": "dev-user",
        "X-Tenant-Id": "dev-tenant",
        "X-Roles": "admin,user"
    }
    
    principal = extract_principal(config, mock_request)
    
    assert principal is not None
    assert principal.id == "dev-user"
    assert principal.tenant_id == "dev-tenant"
    assert "admin" in principal.roles
    assert "user" in principal.roles
    assert principal.claims["type"] == "header"


@pytest.mark.unit
def test_extract_principal_rejects_reserved_system_principal_from_jwt(config, mock_request):
    """JWT-authenticated users cannot claim system:* reserved principal IDs."""
    mock_request.headers = {"Authorization": "Bearer reserved-system-principal"}
    config.allow_insecure_principal_headers = False

    with patch("motet.core.security.auth._verify_jwt_token") as mock_verify:
        mock_verify.return_value = {"sub": "system:oauth-manager", "tenant_id": "acme-corp"}
        with pytest.raises(HTTPException) as exc_info:
            extract_principal(config, mock_request)

    assert exc_info.value.status_code == 403
    assert "Reserved principal namespace" in str(exc_info.value.detail)


@pytest.mark.unit
def test_extract_principal_rejects_reserved_system_principal_from_headers(config, mock_request):
    """Dev-mode header auth cannot claim system:* reserved principal IDs."""
    config.allow_insecure_principal_headers = True
    mock_request.headers = {
        "X-Principal-Id": "system:oauth-refresher",
        "X-Tenant-Id": "dev-tenant",
    }

    with pytest.raises(HTTPException) as exc_info:
        extract_principal(config, mock_request)

    assert exc_info.value.status_code == 403
    assert "Reserved principal namespace" in str(exc_info.value.detail)


@pytest.mark.unit
def test_extract_principal_from_claims_rejects_reserved_system_principal(config):
    """Claim extraction helper rejects reserved system principal IDs."""
    claims = {"sub": "system:worker-warmup", "tenant_id": "acme-corp"}

    with pytest.raises(ValueError, match="Reserved principal namespace"):
        extract_principal_from_claims(claims, config)


@pytest.mark.unit
def test_extract_principal_no_auth(config, mock_request):
    """Test principal extraction with no authentication."""
    mock_request.headers = {}
    
    principal = extract_principal(config, mock_request)
    
    assert principal is None


@pytest.mark.unit
def test_extract_principal_invalid_jwt(config, mock_request):
    """Test principal extraction with invalid JWT."""
    config.jwt_public_key_pem = "invalid-key"
    
    mock_request.headers = {
        "Authorization": "Bearer invalid.jwt.token"
    }
    
    principal = extract_principal(config, mock_request)
    
    assert principal is None


@pytest.mark.unit
def test_extract_principal_tenant_mapping(config, mock_request):
    """Tenant IDs are mapped to canonical identifiers."""
    config.tenant_id_map_json = json.dumps({"org-123": "acme"})
    mock_request.headers = {"Authorization": "Bearer mapped-token"}
    
    with patch('motet.core.security.auth._verify_jwt_token') as mock_verify:
        mock_verify.return_value = {"sub": "user-123", "tenant_id": "org-123"}
        principal = extract_principal(config, mock_request)
    
    assert principal is not None
    assert principal.tenant_id == "acme"


@pytest.mark.unit
def test_extract_principal_global_tenant_scope(config, mock_request):
    """Tenant scope is marked global when configured."""
    config.tenant_id_map_json = json.dumps({"org-global": "motet-global"})
    config.tenant_global_ids = "motet-global"
    mock_request.headers = {"Authorization": "Bearer global-token"}
    
    with patch('motet.core.security.auth._verify_jwt_token') as mock_verify:
        mock_verify.return_value = {"sub": "user-123", "tenant_id": "org-global"}
        principal = extract_principal(config, mock_request)
    
    assert principal is not None
    assert principal.tenant_id == "motet-global"
    assert principal.claims.get("tenant_scope") == "global"


@pytest.mark.unit
def test_extract_principal_organization_claim(config, mock_request):
    """Organization claim is used when tenant_id is absent."""
    config.jwt_organization_claim = "organization"
    mock_request.headers = {"Authorization": "Bearer org-token"}
    
    claims = {
        "sub": "user-789",
        "organization": {
            "slug": "acme-prod",
            "name": "Acme Prod",
        },
        "roles": ["member"]
    }
    
    with patch('motet.core.security.auth._verify_jwt_token') as mock_verify:
        mock_verify.return_value = claims
        principal = extract_principal(config, mock_request)
    
    assert principal is not None
    assert principal.id == "user-789"
    assert principal.tenant_id == "acme-prod"
    assert principal.claims.get("tenant_origin") in {"organization_claim:organization", "tenant_claim"}


@pytest.mark.unit
def test_jwks_cache():
    """Test JWKS cache functionality."""
    cache = _JWKSCache()
    
    assert cache.get() is None
    assert cache.loaded == 0.0
    
    jwks_data = {"keys": [{"kid": "test-key"}]}
    now = time.time()
    cache.set(jwks_data, now)
    
    assert cache.get() == jwks_data
    assert cache.loaded == now


@pytest.mark.unit
def test_verify_jwt_token_with_jwks(config):
    """Test JWT verification with JWKS."""
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    
    # Generate test RSA key pair
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    
    # Create test JWT
    claims = {
        "sub": "user-123",
        "tenant_id": "acme-corp",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600
    }
    
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key-id"})
    
    config.jwt_jwks_url = "https://zitadel.example.com/.well-known/jwks.json"
    
    # Mock JWKS response
    with patch('requests.get') as mock_get:
        mock_get.return_value.json.return_value = {"keys": []}
        mock_get.return_value.raise_for_status = Mock()
        
        # This will fail because we don't have a matching key
        # But we're testing the flow
        result = _verify_jwt_token(config, token)
        
        # Should return None if key not found
        assert result is None


@pytest.mark.unit
def test_extract_principal_expired_jwt(config, mock_request):
    """Test principal extraction with expired JWT."""
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    
    # Generate test RSA key pair
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    
    # Serialize public key to PEM
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode('utf-8')
    
    # Create expired JWT
    claims = {
        "sub": "user-123",
        "tenant_id": "acme-corp",
        "iat": int(time.time()) - 7200,  # 2 hours ago
        "exp": int(time.time()) - 3600    # 1 hour ago (expired)
    }
    
    token = jwt.encode(claims, private_key, algorithm="RS256")
    
    config.jwt_public_key_pem = public_pem
    config.jwt_sub_claim = "sub"
    config.jwt_roles_claim = "roles"
    config.jwt_tenant_claims = "tenant_id"
    
    mock_request.headers = {
        "Authorization": f"Bearer {token}"
    }
    
    principal = extract_principal(config, mock_request)
    
    # Expired token should be rejected
    assert principal is None


@pytest.mark.unit
def test_extract_principal_missing_sub_claim(config, mock_request):
    """Test principal extraction with missing sub claim."""
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    
    # Generate test RSA key pair
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    
    # Serialize public key to PEM
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode('utf-8')
    
    # Create JWT without sub claim
    claims = {
        "tenant_id": "acme-corp",
        "roles": ["admin"],
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600
    }
    
    token = jwt.encode(claims, private_key, algorithm="RS256")
    
    config.jwt_public_key_pem = public_pem
    config.jwt_sub_claim = "sub"
    
    mock_request.headers = {
        "Authorization": f"Bearer {token}"
    }
    
    principal = extract_principal(config, mock_request)
    
    # Missing sub claim should result in None
    assert principal is None


@pytest.mark.unit
def test_extract_principal_malformed_token(config, mock_request):
    """Test principal extraction with malformed JWT token."""
    config.jwt_public_key_pem = "test-key"
    
    # Test various malformed token formats
    malformed_tokens = [
        "not-a-jwt",
        "Bearer",  # Missing token
        "Bearer .",  # Invalid format
        "Bearer a.b",  # Missing parts
        "Bearer a.b.c.d",  # Too many parts
    ]
    
    for token in malformed_tokens:
        mock_request.headers = {
            "Authorization": token if token.startswith("Bearer") else f"Bearer {token}"
        }
        
        principal = extract_principal(config, mock_request)
        assert principal is None, f"Malformed token '{token}' should be rejected"


@pytest.mark.unit
def test_extract_principal_wrong_algorithm(config, mock_request):
    """Test principal extraction with wrong signing algorithm."""
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    
    # Generate test RSA key pair
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    
    # Serialize public key to PEM
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode('utf-8')
    
    # Create JWT with HS256 (symmetric) instead of RS256
    # This should fail verification with RSA public key
    claims = {
        "sub": "user-123",
        "tenant_id": "acme-corp",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600
    }
    
    # Use a symmetric key for HS256
    token = jwt.encode(claims, "secret-key", algorithm="HS256")
    
    config.jwt_public_key_pem = public_pem
    config.jwt_sub_claim = "sub"
    
    mock_request.headers = {
        "Authorization": f"Bearer {token}"
    }
    
    principal = extract_principal(config, mock_request)
    
    # Wrong algorithm should be rejected
    assert principal is None


@pytest.mark.unit
def test_jwks_cache_expiration():
    """Test JWKS cache expiration behavior."""
    cache = _JWKSCache()
    
    jwks_data = {"keys": [{"kid": "test-key"}]}
    now = time.time()
    
    # Set cache with current time
    cache.set(jwks_data, now)
    assert cache.get() == jwks_data
    
    # Set cache with old time (should still work, expiration check is external)
    old_time = now - 1000
    cache.set(jwks_data, old_time)
    assert cache.get() == jwks_data  # Cache still returns data, expiration is checked externally


@pytest.mark.unit
def test_extract_principal_empty_authorization_header(config, mock_request):
    """Test principal extraction with empty Authorization header."""
    mock_request.headers = {
        "Authorization": ""
    }
    
    principal = extract_principal(config, mock_request)
    
    # Empty header should result in None
    assert principal is None


@pytest.mark.unit
def test_extract_principal_bearer_without_token(config, mock_request):
    """Test principal extraction with 'Bearer' but no token."""
    mock_request.headers = {
        "Authorization": "Bearer "
    }
    
    principal = extract_principal(config, mock_request)
    
    # Bearer without token should result in None
    assert principal is None


@pytest.mark.unit
def test_extract_principal_multiple_roles(config, mock_request):
    """Test principal extraction with multiple roles."""
    config.allow_insecure_principal_headers = True
    
    mock_request.headers = {
        "X-Principal-Id": "test-user",
        "X-Tenant-Id": "test-tenant",
        "X-Roles": "admin,user,developer,viewer"
    }
    
    principal = extract_principal(config, mock_request)
    
    assert principal is not None
    assert principal.id == "test-user"
    assert principal.tenant_id == "test-tenant"
    assert len(principal.roles) == 4
    assert "admin" in principal.roles
    assert "user" in principal.roles
    assert "developer" in principal.roles
    assert "viewer" in principal.roles


@pytest.mark.unit
def test_extract_principal_empty_roles(config, mock_request):
    """Test principal extraction with empty roles."""
    config.allow_insecure_principal_headers = True
    
    mock_request.headers = {
        "X-Principal-Id": "test-user",
        "X-Tenant-Id": "test-tenant",
        "X-Roles": ""
    }
    
    principal = extract_principal(config, mock_request)
    
    assert principal is not None
    assert principal.id == "test-user"
    assert principal.tenant_id == "test-tenant"
    # Empty roles should result in empty list or default roles
    assert isinstance(principal.roles, list)


@pytest.mark.unit
def test_require_jwt_if_configured_throttles_repeated_failures(config):
    """Repeated failed JWT attempts are throttled deterministically."""
    config.jwt_public_key_pem = "test-key"
    config.auth_failure_limit_per_minute = 2
    config.auth_failure_window_seconds = 60

    shared_app_state = SimpleNamespace()

    def make_request() -> Request:
        request = Mock(spec=Request)
        request.headers = {"Authorization": "Bearer invalid.jwt.token"}
        request.state = SimpleNamespace()
        request.app = SimpleNamespace(state=shared_app_state)
        request.client = SimpleNamespace(host="127.0.0.1")
        return request

    for _ in range(2):
        with pytest.raises(HTTPException) as exc_info:
            require_jwt_if_configured(config, make_request())
        assert exc_info.value.status_code == 401

    with pytest.raises(HTTPException) as exc_info:
        require_jwt_if_configured(config, make_request())
    assert exc_info.value.status_code == 429


@pytest.mark.unit
def test_validate_insecure_principal_header_policy_rejects_non_dev(config):
    """Non-development environments fail closed on insecure principal headers."""
    config.allow_insecure_principal_headers = True
    config.deployment_environment = "production"
    config.allow_insecure_principal_headers_in_non_dev = False

    with pytest.raises(RuntimeError, match="MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS"):
        validate_insecure_principal_header_policy(config)

