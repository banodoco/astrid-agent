"""The named SDK total is observed; complete, unchanged cadence still gates."""

import copy
import time
from collections import Counter
from types import SimpleNamespace

import pytest

from astrid.sdk.invocation import _wait_for_kernel_task_loop
from tests._local_speed_calibration import calibration, compare_calibrated
from tests._local_speed_sdk_poll import CADENCE, METRIC, PARENT, SITE, sdk_sample
from tests.core.generation.test_local_speed_ambient_calibration import proof
from tests.core.generation.test_local_speed_host_cadence import host_proof
from tests.core.generation.test_local_speed_process_observer import observer


def sdk_observation(polls=2, *, period=0.001, clipped=False, error=False):
    # Use the actual SDK loop, an ordinary task-status double and real sleeps.
    # Capture the function alias before installing the observer.
    class Tasks:
        calls = 0

        def show(self, task_id):
            self.calls += 1
            if error and self.calls == 2:
                raise RuntimeError("task read failed")
            return {"id": task_id, "state": "running" if self.calls <= polls else "succeeded",
                    "result": {"outputs": []}}

    tasks = Tasks()
    meter = observer()
    meter.role = PARENT
    meter.start()
    begin = time.perf_counter_ns()
    try:
        result = _wait_for_kernel_task_loop(SimpleNamespace(tasks=tasks), task_id="probe-task", run_id="probe-run",
                                            timeout_seconds=10, poll_seconds=period)
        report = meter.snapshot(begin, time.perf_counter_ns())
        if clipped:
            item, = report["sdk_wait_invocations"]
            report = meter.snapshot(item["start_ns"] + 1, item["end_ns"] - 1)
    finally:
        meter.stop()
    return report, result, tasks.calls


def replace_sdk(result, *, polls=3, period=0.001, caller="resident_sdk"):
    report, _, _ = sdk_observation(polls, period=period)
    for row in result["counters"]:
        if row["caller"] != caller:
            continue
        parent = row["process_observations"][0]
        replacement = copy.deepcopy(report)
        # Keep the independent SQLite/request observations from the existing
        # fixture; only substitute the separately observed SDK wait component.
        old_counts = {key: count for key, count in parent["counts"].items()
                      if not key.startswith(("sdk_", "sleep_")) and key != "poll_progress"}
        replacement["counts"] = dict(Counter(old_counts) + Counter(replacement["counts"]))
        combined = Counter(row["python_counts"])
        combined.subtract(parent["counts"])
        combined.update(replacement["counts"])
        row["python_counts"] = dict(+combined)
        row["process_observations"][0] = replacement
    return result


def test_actual_sdk_loop_retains_every_poll_result_and_sleep():
    report, result, calls = sdk_observation(11)
    item, = report["sdk_wait_invocations"]
    assert result[1] is True and calls == 12
    assert item["task_id"] == "probe-task" and item["sleep_calls"] == 11
    assert len(item["polls"]) == 12
    assert [poll["sleep_calls"] for poll in item["polls"]] == [1] * 11 + [0]
    assert all(poll["result_known"] and poll["missing_sleep_results"] == 0 for poll in item["polls"])
    sample = sdk_sample({"process_observations": [report], "measured_task_id": "probe-task"})
    assert sample == {"sleep_calls": 11, "unbalanced_polls": [],
                      "minimum_requested_sleep_ns": 1_000_000, "maximum_requested_sleep_ns": 1_000_000}


def test_larger_sdk_total_is_an_observation_and_does_not_set_a_limit():
    base = host_proof()
    actual = replace_sdk(copy.deepcopy(base))
    policy = calibration(base)
    observation = policy["callers"]["resident_sdk"]["metrics"][METRIC]
    assert observation["samples"] == [2] * 5 and observation["maximum"] == 2
    assert observation["policy"] == "observation_only" and observation["decision"] == "6981129059"
    assert "limit" not in observation
    assert compare_calibrated(actual, base, policy) == []
    assert [row["process_observations"][0]["wait_sites"][SITE]
            for row in actual["counters"] if row["caller"] == "resident_sdk"] == [3] * 5
    assert policy["callers"]["fresh_python_cli"]["metrics"][METRIC]["limit"] == 2
    cli_changed = replace_sdk(copy.deepcopy(base), caller="fresh_python_cli")
    assert any(failure["caller"] == "fresh_python_cli" and failure["metric"] == METRIC
               for failure in compare_calibrated(cli_changed, base, policy))


def test_containing_counters_keep_their_existing_caps():
    base = proof((2, 2, 2, 2, 2))
    policy = calibration(base)
    failures = compare_calibrated(replace_sdk(copy.deepcopy(base)), base, policy)
    for name in ("python.sleep_attempt", "process." + PARENT + ".sleep_attempt"):
        assert policy["callers"]["resident_sdk"]["metrics"][name]["limit"] == 2
        assert any(item["metric"] == name and item["actual"] == 3 and item["limit"] == 2 for item in failures)
    assert not any(item["metric"] == METRIC for item in failures)


@pytest.mark.parametrize("count", [0, 2])
def test_each_nonterminal_poll_requires_exactly_one_sleep(count):
    base = host_proof()
    actual = copy.deepcopy(base)
    row = actual["counters"][0]
    process = row["process_observations"][0]
    item = process["sdk_wait_invocations"][0]
    poll = item["polls"][0]
    delta = count - 1
    poll["sleep_calls"] = count
    poll["sleep_requests"] = ({SITE: {"calls": count, "observed_calls": count, "unknown_calls": 0,
                                    "minimum_ns": 1_000_000, "maximum_ns": 1_000_000}} if count else {})
    item["sleep_calls"] += delta
    process["wait_sites"][SITE] += delta
    process["wait_invocation_maxima"][SITE] += delta
    process["poll_wait_maxima"][SITE] = max(count, 1)
    for field in ("calls", "observed_calls"):
        process["sleep_requests"][SITE][field] += delta
    for field in ("sleep_attempt", "sleep_success"):
        process["counts"][field] += delta
        row["python_counts"][field] += delta
    failures = compare_calibrated(actual, base, calibration(base))
    assert any(failure["metric"] == CADENCE + ".sleeps_per_poll" and failure["actual"] == count
               and failure["limit"] == 1 and failure["comparison"] == "exact" for failure in failures)
    with pytest.raises(ValueError, match="base SDK controls"):
        calibration(actual)


@pytest.mark.parametrize("period,shortened", [(0.0005, True), (0.002, False)])
def test_requested_period_cannot_be_shortened(period, shortened):
    base = proof((2, 2, 2, 2, 2))
    actual = replace_sdk(copy.deepcopy(base), polls=2, period=period)
    failures = compare_calibrated(actual, base, calibration(base))
    assert bool(failures) is shortened
    if shortened:
        assert all(item["metric"] == CADENCE + ".minimum_requested_sleep_ns"
                   and item["actual"] == 500_000 and item["limit"] == 1_000_000 for item in failures)


@pytest.mark.parametrize("loss", ["map", "empty", "carry_in", "carry_out", "result", "poll",
                                  "terminal", "identity", "sleep_result", "period", "raw_count",
                                  "task", "gap", "duplicate"])
def test_missing_clipped_or_ambiguous_sdk_evidence_fails_closed(loss):
    base = proof((2, 2, 2, 2, 2))
    actual = copy.deepcopy(base)
    row = actual["counters"][0]
    process = row["process_observations"][0]
    item = process["sdk_wait_invocations"][0]
    poll = item["polls"][0]
    if loss == "map":
        process.pop("sdk_wait_invocations")
    elif loss == "empty":
        process["sdk_wait_invocations"] = []
    elif loss in {"carry_in", "carry_out"}:
        item[loss] = True
    elif loss == "result":
        poll["result_known"] = False
    elif loss == "poll":
        item["polls"].pop(0)
    elif loss == "terminal":
        item["polls"][-1]["state"] = "running"
    elif loss == "identity":
        item["polls"][1]["poll_id"] = poll["poll_id"]
    elif loss == "sleep_result":
        poll["missing_sleep_results"] = 1
    elif loss == "period":
        poll["sleep_requests"][SITE]["unknown_calls"] = 1
    elif loss == "raw_count":
        process["counts"]["sdk_poll_start"] += 1
    elif loss == "task":
        row["measured_task_id"] = "another-task"
    elif loss == "gap":
        process["coverage_gaps"] = ["Python observation event limit exceeded"]
    else:
        process["sdk_wait_invocations"].append(copy.deepcopy(item))
    with pytest.raises(ValueError, match="SDK polling"):
        compare_calibrated(actual, base, calibration(base))


def test_older_aggregate_only_sdk_evidence_does_not_become_complete():
    base = proof((2, 2, 2, 2, 2))
    for row in base["counters"]:
        process = row["process_observations"][0]
        process.pop("sdk_wait_invocations")
        process["counts"] = {key: value for key, value in process["counts"].items() if not key.startswith("sdk_")}
    with pytest.raises(ValueError, match="SDK polling invocation coverage"):
        calibration(base)


def test_observer_retains_clipped_boundaries_and_failed_status_reads():
    clipped, _, _ = sdk_observation(clipped=True)
    item, = clipped["sdk_wait_invocations"]
    assert item["carry_in"] is True and item["carry_out"] is True
    with pytest.raises(ValueError, match="SDK polling"):
        sdk_sample({"process_observations": [clipped]})
    failed, result, calls = sdk_observation(error=True)
    assert result[1] is False and calls == 2
    assert result[0]["error"]["code"] == "task_status_unavailable"
    assert failed["sdk_wait_invocations"][0]["polls"][-1]["result_known"] is False
    with pytest.raises(ValueError, match="SDK polling"):
        sdk_sample({"process_observations": [failed]})
