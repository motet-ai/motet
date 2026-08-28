import asyncio
import pytest

from motet.core.tools import registry
from motet.core import MotetStack, Message, Config


def test_registry_describe_and_categories_present():
    items = registry.describe()
    # Ensure math tool is present with category (may be namespaced as core.math_eval)
    math = next((t for t in items if (t.get("name") or "").endswith("math_eval") or t.get("name") == "math_eval"), None)
    assert math is not None, "math_eval not found in registry.describe()"
    assert math.get("category") == "math"
    # Ensure http_post has secure defaults (may be namespaced as core.http_post)
    post = next((t for t in items if (t.get("name") or "").endswith("http_post") or t.get("name") == "http_post"), None)
    assert post is not None
    x = post.get("x-imf") or {}
    obs = x.get("observation", {})
    # Secure defaults: store False and/or contextualize False (API may expose contextualize only)
    assert obs.get("store", False) is False or obs.get("contextualize") is False
    # Ensure file_read avoids storing by default
    read = next((t for t in items if (t.get("name") or "").endswith("file_read") or t.get("name") == "file_read"), None)
    assert read is not None
    rx = read.get("x-imf") or {}
    robs = rx.get("observation", {})
    assert robs.get("store", False) is False or robs.get("contextualize") is False




