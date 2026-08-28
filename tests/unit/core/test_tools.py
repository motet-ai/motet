from __future__ import annotations

import re
import random
import string

from motet.core.tools import registry


def fuzz_token(n: int = 8) -> str:
    alphabet = string.ascii_letters + string.digits + ":/_-.?=&"
    return "".join(random.choice(alphabet) for _ in range(n))


def test_parse_line_known_triggers_roundtrip():
    # math (registry may return core.math_eval)
    s = "math: 1+2*3"
    p = registry.parse_line(s)
    assert p and (p["name"] == "math_eval" or p["name"].endswith("math_eval"))
    assert p["params"]["expression"].replace(" ", "") == "1+2*3"
    # http (registry may return core.http_get or core.http_get_browser)
    s = "http_get: https://example.com?q=1"
    p = registry.parse_line(s)
    assert p and ("http_get" in p["name"] or p["name"].endswith("http_get"))
    assert p["params"]["url"].startswith("https://example.com")
    # read
    s = "read: /tmp/file.txt"
    p = registry.parse_line(s)
    assert p and (p["name"] == "file_read" or p["name"].endswith("file_read"))
    assert p["params"]["path"].endswith("file.txt")


def test_parse_line_ignores_unknown_and_empty():
    assert registry.parse_line("") is None
    assert registry.parse_line("   ") is None
    assert registry.parse_line("unknown: do x") is None


def test_parse_line_no_injection_into_other_tools():
    # ensure http trigger doesn't parse into math (name may be core.http_get_browser etc.)
    s = "http: math:1+1"
    p = registry.parse_line(s)
    assert p and ("http_get" in p["name"] or p["name"].endswith("http_get"))
    assert "http" in p["params"]["url"] or p["params"]["url"].startswith("http")


def test_fuzz_parse_line_never_crashes():
    # Weak property-based fuzzing: ensure parser never raises and returns either None or a dict with required keys
    for _ in range(100):
        trig = random.choice(["math:", "http:", "https:", "http_get:", "read:", "junk:", ""])  # include unknown
        suffix = fuzz_token(16)
        s = f"{trig}{suffix}"
        res = None
        try:
            res = registry.parse_line(s)
        except Exception as exc:  # pragma: no cover
            assert False, f"parser raised on input {s!r}: {exc}"
        if res is not None:
            assert isinstance(res, dict)
            assert "name" in res and "params" in res
            assert isinstance(res["params"], dict)


