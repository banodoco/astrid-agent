"""A real retained process must supply counters for only its measured job.

This calibrates the meter protocol, not the future LS-8 product implementation.
The ordinary attached integration test retains its real Runtime/output proof.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

from tests.core.generation.test_local_speed_meter_collector import rows, write
from tests.local_speed_meter_run import parse_reports


def test_live_resident_process_excludes_warmup_and_is_accepted_with_zero_starts(tmp_path):
    code = r'''
import hashlib,json,pathlib,sys,threading
from tests._local_speed_process import ProcessObserver,sources
payload=b'resident-meter-result'
h,p=sources()
meter=ProcessObserver({hashlib.sha256(payload).hexdigest():len(payload)},'pack_worker',hash_source=h,observer_source=p)
meter.start()
try:
    for line in sys.stdin:
        command=json.loads(line)
        if command['kind']=='job':
            pathlib.Path(command['output']).write_bytes(payload)
            digest=hashlib.sha256(payload).hexdigest()
            threading.Event().wait(0)
            print(json.dumps({'sha256':digest,'bytes':len(payload)}),flush=True)
        elif command['kind']=='snapshot':
            print(json.dumps(meter.snapshot(command['start'],command['reply'])),flush=True)
        elif command['kind']=='stop':
            break
finally:
    meter.stop()
'''
    root = Path(__file__).resolve().parents[3]
    with subprocess.Popen([sys.executable, "-u", "-c", code], cwd=root,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
        def command(value):
            child.stdin.write(json.dumps(value) + "\n")
            child.stdin.flush()
            line = child.stdout.readline()
            assert line, child.stderr.read()
            return json.loads(line)
        warmup = command({"kind": "job", "output": str(tmp_path / "warmup")})
        start = time.monotonic_ns()
        measured = command({"kind": "job", "output": str(tmp_path / "result")})
        reply = time.monotonic_ns()
        report = command({"kind": "snapshot", "start": start, "reply": reply})
        child.stdin.write('{"kind":"stop"}\n')
        child.stdin.flush()
        assert child.wait(timeout=10) == 0
    assert warmup == measured
    assert (tmp_path / "warmup").read_bytes() == (tmp_path / "result").read_bytes() == b"resident-meter-result"
    assert report["start_ns"] < start
    assert report["interval"] == {"monotonic_start_ns": start, "monotonic_reply_ns": reply}
    assert report["counts"]["event_wait_attempt"] == 1
    assert report["output_hash_streams"] == 1
    assert report["coverage_gaps"] == []
    value = rows(True)
    for row in value:
        row["interval"] = report["interval"]
        row["process_start_counts"]["pack_worker"] = 0
        row["process_observations"].append(report)
    assert len(parse_reports(write(tmp_path, value), instrumented=True)) == 2


def test_resident_lifetime_report_cannot_substitute_for_job_interval(tmp_path):
    import pytest
    value = rows(True)
    value[0]["interval"] = {"monotonic_start_ns": 100, "monotonic_reply_ns": 200}
    value[0]["process_start_counts"]["pack_worker"] = 0
    value[0]["process_observations"].append({"role": "pack_worker", "pid": 2, "start_ns": 10,
        "end_ns": 300, "counts": {}, "coverage_gaps": []})
    with pytest.raises(ValueError, match="interval evidence"):
        parse_reports(write(tmp_path, value), instrumented=True)
