from motet.interfaces.http import create_app


def test_tools_endpoint_describe_shape():
    # Use a known API key so tools listing is authenticated (API requires principal)
    import os
    os.environ["MOTET_API_KEY"] = "test-tools-key"
    os.environ.pop("MOTET_JWT_JWKS_URL", None)
    os.environ.pop("MOTET_JWT_PUBLIC_KEY_PEM", None)
    app = create_app()
    from fastapi.testclient import TestClient
    client = TestClient(app)
    headers = {"X-API-Key": "test-tools-key"}

    # Legacy shape (ADR-0053: /api/v1/tools)
    r = client.get("/api/v1/tools", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, dict)
    # Registry may use qualified names (core.math_eval) or bare (math_eval)
    math_key = "core.math_eval" if "core.math_eval" in data else "math_eval"
    assert math_key in data and "schema" in data[math_key]

    # New shape at /api/v1/tools/describe
    r2 = client.get("/api/v1/tools/describe", headers=headers)
    assert r2.status_code == 200
    data2 = r2.json()
    assert isinstance(data2, list)
    names = {t.get("name") for t in data2}
    assert "math_eval" in names or "core.math_eval" in names


