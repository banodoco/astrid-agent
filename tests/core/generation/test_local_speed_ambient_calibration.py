"""Scheduling variation must not hide a new operation inside one job/poll."""

import copy
import os
import sqlite3
import time
from collections import Counter

import pytest

from tests._local_speed_baseline import CALLERS, make_baseline
from tests._local_speed_calibration import PARENT, calibration, compare_calibrated
from tests._local_speed_runtime import RuntimeObserver
from tests.core.generation.test_local_speed_baseline import reports
from tests.core.generation.test_local_speed_process_observer import observer

SETTINGS = {"host_poll_seconds": "product_default", "machine_poll_seconds": "product_default",
            "sdk_poll_seconds": "product_default"}
ROUTES = ("POST /v1/tasks", "POST /v1/tasks/claim", "POST /v1/attempts/<attempt>/settle")


def probe_row(caller, index, polls, *, extra_sleep=False, extra_commit=False, extra_sql_commit=False, extra_noop_commit=False):
    """Exercise real sleeps and SQLite commits, without changing product files."""
    meter, connection = observer(), sqlite3.connect(":memory:")
    meter.role = PARENT
    connection.execute("CREATE TABLE proof (value INTEGER)")
    meter.runtime_connection = connection
    statements = []
    def trace(statement):
        if statement == "COMMIT":
            statements.append(statement)
            meter.event("runtime_commit_statement", write_context=meter.runtime_write_context(None))
    connection.set_trace_callback(trace)
    runtime = RuntimeObserver.__new__(RuntimeObserver)
    runtime.process, runtime.requests, runtime.background = meter, [], []
    namespace = {"__name__": "astrid.sdk.invocation", "time": time}
    exec(compile(
        "def _wait_for_kernel_task_loop(show, polls, extra):\n"
        "    for index in range(polls):\n"
        "        show()\n"
        "        time.sleep(0)\n"
        "        if extra and index == 0:\n"
        "            time.sleep(0)\n", "<per-job-sleep-injection>", "exec"), namespace)

    class Status:
        def show(self):
            return None

    meter.start()
    begin = time.perf_counter_ns()
    try:
        for index_in_job, route in enumerate((ROUTES[0], *([ROUTES[1]] * polls), ROUTES[2])):
            span = runtime._span("runtime_request_span", "http_request", route)
            span["scope"] = id(span)
            runtime.requests.append(span)
            meter.context.scope, meter.context.route = span["scope"], route
            for operation_index in range(2 if (extra_commit or extra_sql_commit) and index_in_job == 0 else 1):
                connection.execute("INSERT INTO proof VALUES (1)")
                if extra_sql_commit and index_in_job == 0 and operation_index == 1:
                    connection.execute("COMMIT")
                else:
                    connection.commit()
            if extra_noop_commit and index_in_job == 0:
                connection.commit()
            runtime._finish(span)
            meter.context.scope = meter.context.route = None
        namespace["_wait_for_kernel_task_loop"](Status().show, polls, extra_sleep)
        end = time.perf_counter_ns()
        parent = meter.snapshot(begin, end)
    finally:
        meter.stop()
        connection.close()
    _, original = reports()
    row = copy.deepcopy(next(row for row in original if row["caller"] == caller))
    worker = {"role": "pack_worker", "pid": -1, "counts": {}, "output_hash_streams": 1,
              "wait_sites": {}, "wait_invocation_maxima": {}, "poll_sites": [], "poll_wait_maxima": {}}
    row.update(sample_index=index, gate_version=3, harness_settings=SETTINGS,
               process_observations=[parent, worker], python_counts=parent["counts"],
               wait_coverage=["event_wait", "condition_wait", "asyncio_sleep", "selector_wait"],
               runtime_main_connection_commit_statements=len(statements),
               runtime_http_paths=dict(Counter(span["route"] for span in runtime.requests)),
               runtime_operation_scopes=runtime.operation_scopes(begin, end),
               write_spans=[dict(span, pid=os.getpid()) for span in parent["write_spans"]],
               sqlite={"before": {"journal_mode": "memory", "synchronous": 2},
                       "after": {"journal_mode": "memory", "synchronous": 2}})
    return row


def proof(polls=(2, 3, 4, 5, 6), **injections):
    timing, _ = reports()
    for row in timing:
        row.update(gate_version=3, harness_settings=SETTINGS)
    counters = [probe_row(caller, index, count, **injections)
                for caller in CALLERS for index, count in enumerate(polls)]
    baseline = make_baseline(timing, counters, system="Linux", sources={"agent": "base", "runtime": "same-runtime"})
    baseline["pair_id"] = "same-runner-pair"
    return {"baseline": baseline, "counters": counters}


def test_five_base_controls_name_each_spread_and_accept_independent_base_variation():
    base = proof()
    policy = calibration(base)
    item = policy["callers"]["resident_sdk"]["metrics"]["python.sleep_attempt"]
    assert item == {"samples": [2, 3, 4, 5, 6], "minimum": 2, "maximum": 6, "spread": 4,
                    "margin": 4, "limit": 10, "group": "whole_workload_wait_volume",
                    "policy": "observed_maximum_plus_range"}
    assert compare_calibrated(proof((3, 4, 5, 6, 7)), base, policy) == []
    assert policy["reference_eligible"] is False


@pytest.mark.parametrize("injection,expected", [
    ("extra_sleep", "per_poll.parent_runtime_host_sdk.sleep_attempt|astrid.sdk.invocation:_wait_for_kernel_task_loop"),
    ("extra_commit", "per_request.POST /v1/tasks.runtime_outer_commit_success"),
    ("extra_sql_commit", "per_request.POST /v1/tasks.runtime_commit_statement"),
])
def test_real_extra_per_job_operation_fails_inside_the_ambient_allowance(injection, expected):
    base = proof()
    actual = proof((3, 3, 3, 3, 3), **{injection: True})
    policy = calibration(base)
    failures = compare_calibrated(actual, base, policy)
    assert any(failure["metric"] == expected and failure["actual"] == 2 and failure["limit"] == 1 for failure in failures)
    assert not any(failure["metric"] in {"python.sleep_attempt", "runtime_commit_statements"} for failure in failures)


def test_extra_noop_commit_is_distinct_from_successful_outer_commits_and_still_fails():
    base, actual = proof(), proof((3, 3, 3, 3, 3), extra_noop_commit=True)
    failures = compare_calibrated(actual, base, calibration(base))
    assert any(failure["metric"] == "per_request.POST /v1/tasks.runtime_commit_without_transaction"
               and failure["actual"] == 1 and failure["limit"] == 0 for failure in failures)


def test_actual_commit_outside_the_transaction_helper_is_still_observed():
    meter, connection = observer(), sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE proof(value INTEGER)")
    meter.runtime_connection = connection
    meter.runtime_commit_code = (lambda: None).__code__
    meter.start()
    try:
        connection.execute("INSERT INTO proof VALUES(1)")
        connection.commit()
        row = meter.snapshot()
    finally:
        meter.stop()
        connection.close()
    assert row["counts"]["runtime_outer_commit_success"] == 1
    assert row["write_spans"][0]["write_context"] == "unattributed_runtime"


def test_three_job_route_commits_remain_three_while_claim_poll_volume_varies():
    row = probe_row("resident_sdk", 0, 1)
    assert row["runtime_main_connection_commit_statements"] == 3
    assert [scope["counts"]["runtime_outer_commit_success"] for scope in row["runtime_operation_scopes"]] == [1, 1, 1]
    assert len([span for span in row["write_spans"] if span["kind"] == "runtime_outer_commit_success"]) == 3


def test_stable_worker_counter_has_no_margin_and_one_extra_still_fails():
    base = proof()
    actual = copy.deepcopy(base)
    for row in actual["counters"]:
        row["process_observations"][1]["counts"]["sleep_attempt"] = 1
    policy = calibration(base)
    assert policy["callers"]["resident_sdk"]["metrics"]["process.pack_worker.sleep_attempt"]["margin"] == 0
    assert any(failure["metric"] == "process.pack_worker.sleep_attempt" for failure in compare_calibrated(actual, base, policy))


def test_control_sample_shortfall_and_candidate_derived_limits_are_refused():
    base = proof()
    with pytest.raises(ValueError, match="five distinct"):
        calibration(proof((2, 3, 4, 5)))
    policy = calibration(base)
    policy["callers"]["resident_sdk"]["metrics"]["python.sleep_attempt"]["limit"] += 1
    with pytest.raises(ValueError, match="only from.*base"):
        compare_calibrated(base, base, policy)


@pytest.mark.parametrize("field", ["wait_sites", "poll_wait_maxima"])
def test_lost_wait_coverage_is_not_an_observed_zero(field):
    base = proof()
    actual = copy.deepcopy(base)
    actual["counters"][0]["process_observations"][0].pop(field)
    with pytest.raises(ValueError, match="coverage is incomplete"):
        compare_calibrated(actual, base, calibration(base))


def test_unrelated_runner_or_dependency_cannot_use_the_calibration():
    base = proof()
    for field in ("pair_id", "runtime"):
        actual = copy.deepcopy(base)
        if field == "pair_id":
            actual["baseline"][field] = "another-runner"
        else:
            actual["baseline"]["sources"][field] = "new-runtime"
        with pytest.raises(ValueError, match="same runner|dependency mismatch"):
            compare_calibrated(actual, base, calibration(base))
