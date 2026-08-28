import pytest

from motet.core.types import Principal
from motet.interfaces.api.shared.identity import get_principal_context


def test_get_principal_context_requires_principal():
    with pytest.raises(ValueError, match="principal is required"):
        get_principal_context(None)


def test_get_principal_context_defaults_missing_tenant_on_principal():
    principal = Principal(id="u1", tenant_id=None, motet_id="m1", roles=[], claims={})
    motet_id, tenant_id, principal_id = get_principal_context(principal)
    assert motet_id == "m1"
    assert tenant_id == "default"
    assert principal_id == "u1"


def test_get_principal_context_rejects_empty_principal_id():
    principal = Principal(id="", tenant_id="t1", motet_id="m1", roles=[], claims={})
    with pytest.raises(ValueError, match="principal.id is required"):
        get_principal_context(principal)
