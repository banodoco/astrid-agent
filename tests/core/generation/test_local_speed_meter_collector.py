"""Collector invariants; synthetic records never qualify the product."""
import copy
import json
import pytest
from tests.local_speed_meter_run import REPORT_PREFIX, parse_reports, require_junit


def rows(instrumented):
    return [{"schema": "astrid.local-speed-observation.v1", "caller": caller,
             "instrumented": instrumented, "caller_seconds": 30.0,
             "standin_execution_seconds": [0.01], "attached_http_paths": {"POST /prompt": 1},
             "proof": {"warmup_task_id": caller + "1", "measured_task_id": caller + "2",
                       "verified_states": ["succeeded", "succeeded"], "outputs": [
                           {"phase": phase, "bytes": 1, "sha256": "a" * 64, "dimensions": [1, 1]}
                           for phase in ("warmup", "delivered")]},
             "observer_gaps": [], "process_observations": [{"pid": 1}], "output_hash_streams": 2}
            for caller in ("resident_sdk", "fresh_python_cli")]


def write(tmp_path, value):
    path = tmp_path / "log.txt"
    path.write_text("".join(REPORT_PREFIX + json.dumps(row) + "\n" for row in value))
    return path


@pytest.mark.parametrize("instrumented", [False, True])
def test_complete_pair_has_one_report_per_actual_caller(tmp_path, instrumented):
    assert len(parse_reports(write(tmp_path, rows(instrumented)), instrumented=instrumented)) == 2


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "mode", "output", "task", "submit", "coverage", "hash"])
def test_incomplete_or_falsely_green_report_is_rejected(tmp_path, mutation):
    value = rows(True)
    if mutation == "missing": value.pop()
    elif mutation == "duplicate": value[1] = copy.deepcopy(value[0])
    elif mutation == "mode": value[0]["instrumented"] = False
    elif mutation == "output": value[0]["proof"]["outputs"] = []
    elif mutation == "task": value[0]["proof"]["verified_states"] = ["queued", "succeeded"]
    elif mutation == "submit": value[0]["attached_http_paths"]["POST /prompt"] = 2
    elif mutation == "coverage": value[0]["observer_gaps"] = ["missing worker"]
    elif mutation == "hash": value[0]["output_hash_streams"] = 0
    with pytest.raises(ValueError): parse_reports(write(tmp_path, value), instrumented=True)


def test_junit_skips_are_not_passes(tmp_path):
    path = tmp_path / "results.xml"
    path.write_text('<testsuites><testsuite tests="2" failures="0" errors="0" skipped="1"/></testsuites>')
    with pytest.raises(ValueError): require_junit(path)
