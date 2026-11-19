"""The four custody census children are gated by invocation, not an exit race."""

import copy
import subprocess
import sys
import time
from collections import Counter

import pytest

from tests._local_speed_baseline import CALLERS, make_baseline
from tests._local_speed_calibration import PARENT, calibration, compare_calibrated
from tests.core.generation.test_local_speed_ambient_calibration import SETTINGS, proof
from tests.core.generation.test_local_speed_baseline import reports
from tests.core.generation.test_local_speed_process_observer import observer

PRODUCER = "astrid.core.execution.process_group:_process_snapshot"
SITE = "sleep_attempt|subprocess:Popen._wait"
METRIC = "census." + PRODUCER
CONTAINERS = ("python.sleep_attempt", "process." + PARENT + ".sleep_attempt",
              "waitsite." + PARENT + "." + SITE)


def census_report(sleeps=3, *, children=4, twice=False, period=1_000_000,
                  unbalanced=False, unrelated=False):
    """Exact observation records exercise calibration without scheduling races."""
    records = []
    for index in range(children):
        waits = int(index < sleeps) + int(twice and index == 0)
        count = waits + int(unbalanced and index == 0)
        requests = {SITE: {"calls": count, "observed_calls": count, "unknown_calls": 0,
                           "minimum_ns": period, "maximum_ns": period}} if count else {}
        records.append({"invocation_id": index, "producer": PRODUCER, "tid": 12,
                        "child_pid": 100 + index, "spawned_children": 1,
                        "start_ns": 10 + index * 10, "end_ns": 19 + index * 10,
                        "carry_in": False, "carry_out": False, "command_known": True,
                        "returncode": 0, "wait_calls": waits + 1,
                        "missing_wait_results": 0, "nonterminal_waits": waits,
                        "terminal_waits": 1, "sleep_calls": count,
                        "sleep_requests": requests, "ambiguous_children": False})
    total = sum(record["sleep_calls"] for record in records) + int(unrelated)
    counts = {"census_start": children, "census_end": children,
              "process_start": children, "sleep_attempt": total, "sleep_success": total}
    return {"role": PARENT, "pid": 20, "counts": counts, "output_hash_streams": 0,
            "interval": {"monotonic_start_ns": 1, "monotonic_reply_ns": 1000},
            "process_starts": [{"child_pid": 100 + i, "census_invocation": i,
                                "tid": 12, "child_role": "external"} for i in range(children)],
            "census_invocations": records,
            "wait_sites": {SITE: total} if total else {},
            "wait_invocation_maxima": {SITE: 2 if twice or unbalanced else 1} if total else {},
            "poll_sites": ["subprocess:Popen._wait"],
            "poll_wait_maxima": {SITE: 2 if unbalanced else 1} if total else {},
            "sleep_requests": {SITE: {"calls": total, "observed_calls": total, "unknown_calls": 0,
                                      "minimum_ns": period, "maximum_ns": period}} if total else {},
            "poll_windows": [], "coverage_gaps": []}


def census_proof(sleeps=(3, 3, 3, 3, 3), **changes):
    result = proof((2, 2, 2, 2, 2))
    for row in result["counters"]:
        report = census_report(sleeps[row["sample_index"]], **changes)
        row["process_observations"].append(report)
        row["python_counts"] = dict(Counter(row["python_counts"]) + Counter(report["counts"]))
    timing, _ = reports()
    for row in timing:
        row.update(gate_version=3, harness_settings=SETTINGS)
    result["baseline"] = make_baseline(timing, result["counters"], system="Linux",
                                       sources={"agent": "base", "runtime": "same-runtime"})
    result["baseline"]["pair_id"] = "same-runner-pair"
    return result


@pytest.mark.parametrize("sleeps", [(2, 4, 3, 3, 4), (3, 2, 3, 4, 4), (0, 0, 0, 0, 0)])
def test_independent_base_repeats_accept_the_exit_race(sleeps):
    base, repeat = census_proof(), census_proof(sleeps)
    policy = calibration(base)
    assert compare_calibrated(repeat, base, policy) == []
    for caller in CALLERS:
        rule = policy["callers"][caller]["census_invariants"]
        assert rule["producer"] == PRODUCER
        assert rule["decision"] == "6970432614"
        assert rule["invocations"] == 4
        assert rule["maximum_nonterminal_waits"] == 1
        assert rule["minimum_requested_sleep_ns"] == 1_000_000
        for name in CONTAINERS:
            item = policy["callers"][caller]["metrics"][name]
            assert item["census_sleep_samples"] == [3] * 5
            assert item["raw_samples"] == [value + 3 for value in item["samples"]]


@pytest.mark.parametrize("change,field", [
    ({"children": 5}, "invocations"),
    ({"children": 3}, "invocations"),
    ({"twice": True, "sleeps": (2, 2, 2, 2, 2)}, "maximum_nonterminal_waits"),
    ({"period": 500_000}, "minimum_requested_sleep_ns"),
    ({"unbalanced": True}, "unbalanced_waits"),
])
def test_census_invariants_reject_one_changed_operation(change, field):
    base = census_proof()
    failures = compare_calibrated(census_proof(**change), base, calibration(base))
    assert any(failure["metric"] == METRIC + "." + field for failure in failures)


def test_an_unrelated_subprocess_wait_keeps_its_existing_exact_cap():
    base = census_proof()
    failures = compare_calibrated(census_proof(unrelated=True), base, calibration(base))
    assert any(failure["metric"] == CONTAINERS[-1] and failure["actual"] == 1
               and failure["limit"] == 0 for failure in failures)


def test_zero_wait_controls_accept_zero_wait_repeats_without_inventing_a_period():
    base, repeat = census_proof((0, 0, 0, 0, 0)), census_proof((0, 0, 0, 0, 0))
    policy = calibration(base)
    assert compare_calibrated(repeat, base, policy) == []
    for caller in CALLERS:
        assert policy["callers"][caller]["census_invariants"]["minimum_requested_sleep_ns"] is None
        assert policy["callers"][caller]["census_invariants"]["invocations"] == 4


def test_an_unobserved_base_period_cannot_authorize_a_new_wait():
    base = census_proof((0, 0, 0, 0, 0))
    policy = calibration(base)
    with pytest.raises(ValueError, match="census.*period.*unobserved"):
        compare_calibrated(census_proof((1, 1, 1, 1, 1)), base, policy)


@pytest.mark.parametrize("loss", ["map", "missing_result", "unknown_command", "ambiguous_child",
                                  "child_pid", "duplicate", "carry_in", "carry_out", "period",
                                  "spawn", "raw_count"])
def test_missing_census_identity_or_observation_is_rejected(loss):
    base = census_proof()
    actual = copy.deepcopy(base)
    report = actual["counters"][0]["process_observations"][-1]
    record = report["census_invocations"][0]
    if loss == "map":
        report.pop("census_invocations")
    elif loss == "missing_result":
        record["missing_wait_results"] = 1
    elif loss == "unknown_command":
        record["command_known"] = False
    elif loss == "ambiguous_child":
        record["ambiguous_children"] = True
    elif loss == "child_pid":
        record["child_pid"] = None
    elif loss == "duplicate":
        report["census_invocations"][1] = copy.deepcopy(record)
    elif loss in {"carry_in", "carry_out"}:
        record[loss] = True
    elif loss == "period":
        record["sleep_requests"][SITE]["unknown_calls"] = 1
    elif loss == "spawn":
        report["process_starts"].pop()
    else:
        report["counts"]["census_start"] += 1
    with pytest.raises(ValueError, match="census"):
        compare_calibrated(actual, base, calibration(base))


def test_other_counters_and_base_only_policy_remain_unchanged():
    base = census_proof()
    policy = calibration(base)
    without = calibration(proof((2, 2, 2, 2, 2)))
    for caller in CALLERS:
        for name, item in without["callers"][caller]["metrics"].items():
            if name not in CONTAINERS:
                assert policy["callers"][caller]["metrics"][name] == item
    forged = copy.deepcopy(policy)
    forged["callers"][CALLERS[0]]["census_invariants"]["invocations"] = 5
    with pytest.raises(ValueError, match="only from.*base"):
        compare_calibrated(base, base, forged)


@pytest.mark.parametrize("alias", [False, True])
def test_census_observer_follows_real_run_and_preinstalled_alias(alias):
    namespace = {"__name__": "astrid.core.execution.process_group", "subprocess": subprocess,
                 "saved_run": subprocess.run, "executable": sys.executable}
    exec(compile("def _process_snapshot():\n"
                 "    return " + ("saved_run" if alias else "subprocess.run") +
                 "([executable, '-c', 'pass'], capture_output=True, timeout=30)\n",
                 "<census-observation-probe>", "exec"), namespace)
    meter = observer()
    meter.role = PARENT
    meter.start()
    begin = time.perf_counter_ns()
    try:
        result = namespace["_process_snapshot"]()
        report = meter.snapshot(begin, time.perf_counter_ns())
    finally:
        meter.stop()
    assert result.returncode == 0
    assert len(report["census_invocations"]) == 1
    record = report["census_invocations"][0]
    assert record["producer"] == PRODUCER
    assert record["command_known"] is False  # Python is not the approved ps command.
    assert record["child_pid"] == report["process_starts"][0]["child_pid"]
    assert record["spawned_children"] == 1
    assert record["missing_wait_results"] == 0
    assert record["returncode"] == 0
    assert record["carry_in"] is record["carry_out"] is False
    assert report["coverage_gaps"] == []
