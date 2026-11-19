"""LS-12 proof completeness and D1 statistics, independent of product speed."""
from copy import deepcopy
import json

import pytest

from tests.ls12_first_job_run import CALLERS, PREFIX, distribution, parse_reports, require_junit


def _row(caller):
    return {"schema": "astrid.local-speed-observation.v1", "caller": caller,
            "sample_index": 0, "instrumented": False, "caller_seconds": 2.8,
            "standin_execution_seconds": [0.4], "standin_queue_seconds": [0.2],
            "attached_http_paths": {"POST /prompt": 1},
            "drive_controls": {"product_defaults": True},
            "proof": {"measured_task_id": caller, "verified_states": ["succeeded"],
                      "outputs": [{"bytes": 82, "sha256": "a" * 64, "dimensions": [16, 16]}]}}


def test_first_job_overhead_retains_queue_wait(tmp_path):
    path = tmp_path / "log"
    path.write_text("\n".join(PREFIX + json.dumps(_row(c)) for c in CALLERS))
    rows = parse_reports(path, instrumented=False, samples=1)
    assert [row["overhead_seconds"] for row in rows] == pytest.approx([2.4, 2.4])
    assert all(row["queue_time_subtracted"] is False for row in rows)


@pytest.mark.parametrize("defect", ["missing", "duplicate", "warmup", "nonfinite", "output", "queue", "mode", "submit"])
def test_incomplete_or_wrong_first_job_is_rejected(tmp_path, defect):
    rows = [_row(c) for c in sorted(CALLERS)]
    if defect == "missing": rows.pop()
    elif defect == "duplicate": rows[1] = deepcopy(rows[0])
    elif defect == "warmup": rows[0]["proof"]["warmup_task_id"] = "other"
    elif defect == "nonfinite": rows[0]["caller_seconds"] = float("nan")
    elif defect == "output": rows[0]["proof"]["outputs"] = []
    elif defect == "queue": rows[0]["standin_queue_seconds"] = [-1]
    elif defect == "mode": rows[0]["instrumented"] = True
    elif defect == "submit": rows[0]["attached_http_paths"]["POST /prompt"] = 2
    path = tmp_path / "log"
    path.write_text("\n".join(PREFIX + json.dumps(row) for row in rows))
    with pytest.raises(ValueError): parse_reports(path, instrumented=False, samples=1)


def test_counter_coverage_is_required(tmp_path):
    rows = [_row(c) for c in sorted(CALLERS)]
    for row in rows:
        row["instrumented"] = True
        row["observer_gaps"] = []
    path = tmp_path / "log"
    path.write_text("\n".join(PREFIX + json.dumps(row) for row in rows))
    with pytest.raises(ValueError, match="incomplete process/hash"):
        parse_reports(path, instrumented=True, samples=1)


@pytest.mark.parametrize("controls", [None, {}, {"product_defaults": False},
                                    {"product_defaults": 1},
                                    {"product_defaults": True, "poll_seconds": 0.02}])
def test_first_job_requires_product_cadence_evidence(tmp_path, controls):
    rows = [_row(c) for c in sorted(CALLERS)]
    if controls is None:
        rows[0].pop("drive_controls")
    else:
        rows[0]["drive_controls"] = controls
    path = tmp_path / "log"
    path.write_text("\n".join(PREFIX + json.dumps(row) for row in rows))
    with pytest.raises(ValueError, match="product-default host/machine cadence"):
        parse_reports(path, instrumented=False, samples=1)


def test_statistics_retain_slowest_p99_and_share_over_limit():
    result = distribution([1.0] * 19 + [4.0], 3.0)
    assert result == {"samples": 20, "median": 1.0, "p99": 4.0, "slowest": 4.0,
                      "share_at_or_over_limit": 0.05, "limit_seconds": 3.0,
                      "median_under_limit": True, "p99_under_limit": False}


@pytest.mark.parametrize("wrong", ["missing", "duplicate", "skipped", "failed"])
def test_junit_requires_exact_passing_first_job_ids(tmp_path, wrong):
    cases = [f'<testcase name="test_first_attached_job_observation[0-{c}]" />' for c in sorted(CALLERS)]
    if wrong == "missing": cases.pop()
    elif wrong == "duplicate": cases[1] = cases[0]
    elif wrong == "skipped": cases[0] = cases[0].replace(" />", "><skipped /></testcase>")
    else: cases[0] = cases[0].replace(" />", "><failure /></testcase>")
    path = tmp_path / "junit.xml"
    path.write_text('<testsuite>' + ''.join(cases) + '</testsuite>')
    with pytest.raises(ValueError): require_junit(path, 1)
