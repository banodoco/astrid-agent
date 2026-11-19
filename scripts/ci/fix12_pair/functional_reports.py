"""Public subset of standard pytest reports; no assertion text or captured streams."""
import json
import os
import time
from pathlib import Path


def record(**fields):
    path = Path(os.environ["RUNNER_TEMP"]) / "fix12-receipt" / "pytest.jsonl"
    with path.open("a") as stream:
        stream.write(json.dumps({"monotonic_ns": time.monotonic_ns(), **fields}) + "\n")


def pytest_collection_finish(session):
    for item in session.items:
        marker = item.get_closest_marker("timeout")
        record(event="selected", nodeid=item.nodeid, timeout=list(marker.args) if marker else [])


def pytest_runtest_logstart(nodeid, location):
    record(event="test_start", nodeid=nodeid)


def pytest_runtest_logreport(report):
    record(event="phase_end", nodeid=report.nodeid, phase=report.when,
           outcome=report.outcome, duration_s=report.duration)


def pytest_sessionfinish(session, exitstatus):
    record(event="session_finish", exitstatus=int(exitstatus))
