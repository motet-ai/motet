import os
from motet import tracing


def test_replay_plan_parity(tmp_path):
    os.environ["MOTET_TRACE_ENABLED"] = "true"
    os.environ["MOTET_TRACE_DIR"] = str(tmp_path)
    tid = "parity1"
    tracing.start_trace(tid, {"x": 1})
    tracing.record_event(tid, {"kind": "plan", "names": ["math_eval", "file_read"]})
    tracing.record_event(tid, {"kind": "step", "name": "math_eval", "params": {"expression": "1+1"}})
    tracing.record_event(tid, {"kind": "step", "name": "file_read", "params": {"path": "/tmp/x"}})
    events = tracing.load_trace(tid)
    plan = next((e for e in events if e.get("kind") == "plan"), None)
    steps = [e for e in events if e.get("kind") == "step"]
    assert [n for n in (plan.get("names") if plan else [])] == [s.get("name") for s in steps]


