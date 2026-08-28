"""
Motet - Artifact Range Utility Tests (ADR-0118 Phase A)
"""

import pytest

from motet.core.artifacts.range_utils import (
    ByteRangeError,
    artifact_payload_to_bytes,
    parse_byte_range,
    slice_payload_bytes,
)


def test_artifact_payload_to_bytes_variants():
    assert artifact_payload_to_bytes(b"abc") == b"abc"
    assert artifact_payload_to_bytes("hi") == b"hi"
    assert artifact_payload_to_bytes({"a": 1}) == b'{"a": 1}'


def test_parse_byte_range_closed():
    start, end = parse_byte_range("bytes=0-99", 1000)
    assert (start, end) == (0, 99)


def test_parse_byte_range_open_ended():
    start, end = parse_byte_range("bytes=500-", 1000)
    assert (start, end) == (500, 999)


def test_parse_byte_range_suffix():
    start, end = parse_byte_range("bytes=-100", 1000)
    assert (start, end) == (900, 999)


def test_parse_byte_range_unsatisfiable():
    with pytest.raises(ByteRangeError):
        parse_byte_range("bytes=1000-", 1000)


def test_slice_payload_bytes_inclusive():
    data = b"0123456789"
    assert slice_payload_bytes(data, 2, 5) == b"2345"
