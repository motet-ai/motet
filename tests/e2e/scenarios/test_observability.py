import os

from motet import tracing


def test_file_trace_roundtrip(tmp_path):
    os.environ["MOTET_TRACE_ENABLED"] = "true"
    os.environ.pop("MOTET_TRACE_BACKEND", None)
    os.environ["MOTET_TRACE_DIR"] = str(tmp_path)
    tid = "t123"
    tracing.start_trace(tid, {"x": 1})
    tracing.record_event(tid, {"kind": "step", "a": 1})
    tracing.end_trace(tid, {"ok": True})
    items = tracing.load_trace(tid)
    assert len(items) >= 2
    listed = tracing.list_traces(limit=5)
    assert any(it.get("trace_id") == tid for it in listed)


