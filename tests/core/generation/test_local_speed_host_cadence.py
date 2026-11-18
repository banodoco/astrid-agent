"""A base-only host window allowance must retain exact polling cadence."""

import copy
import subprocess
import threading
import time
from collections import Counter

import pytest

from tests._local_speed_baseline import CALLERS, make_baseline
from tests._local_speed_calibration import PARENT, calibration, compare_calibrated
from tests.core.generation.test_local_speed_ambient_calibration import SETTINGS, proof
from tests.core.generation.test_local_speed_baseline import reports
from tests.core.generation.test_local_speed_process_observer import observer

PRODUCER = "astrid.core.execution.generic_host:GenericPackHost._run_command_definition"
SITE = "sleep_attempt|" + PRODUCER
METRIC = "waitsite." + PARENT + "." + SITE
PER_POLL = "per_poll." + PARENT + "." + SITE
CADENCE = "cadence." + PARENT + "." + PRODUCER
PROPAGATED = ("python.sleep_attempt", "process." + PARENT + ".sleep_attempt")


def host_observation(polls, *, period=0.001, extra=False, outside=False):
    # The real sleep/C-call and Popen.poll wrapper are observed. A deterministic
    # process double controls its results; the separate poll-window test uses
    # a real child. The one guard wait models work between polling boundaries.
    class Process:
        def __init__(self):
            self.pid = 123
            self.remaining = polls

        def _internal_poll(self, *args, **kwargs):
            self.remaining -= 1
            return None if self.remaining >= 0 else 0

    namespace = {"__name__": "astrid.core.execution.generic_host", "time": time,
                 "subprocess": subprocess, "guard": threading.Event()}
    exec(compile(
        "class GenericPackHost:\n"
        "    @staticmethod\n"
        "    def _run_command_definition(process, period, extra, outside):\n"
        "        first = True\n"
        "        while subprocess.Popen.poll(process) is None:\n"
        "            if first:\n"
        "                guard.wait(0.03)\n"
        "            time.sleep(period)\n"
        "            if first and extra:\n"
        "                time.sleep(period)\n"
        "            first = False\n"
        "        if outside:\n"
        "            time.sleep(period)\n", "<host-cadence-probe>", "exec"), namespace)
    meter = observer()
    meter.role = PARENT
    meter.start()
    begin = time.perf_counter_ns()
    try:
        namespace["GenericPackHost"]._run_command_definition(Process(), period, extra, outside)
        return meter.snapshot(begin, time.perf_counter_ns())
    finally:
        meter.stop()


def host_proof(polls=(2, 3, 2, 3, 2), **injections):
    result = proof((2, 2, 2, 2, 2))
    for row in result["counters"]:
        host = host_observation(polls[row["sample_index"]], **injections)
        row["process_observations"].append(host)
        row["python_counts"] = dict(Counter(row["python_counts"]) + Counter(host["counts"]))
    timing, _ = reports()
    for row in timing:
        row.update(gate_version=3, harness_settings=SETTINGS)
    result["baseline"] = make_baseline(timing, result["counters"], system="Linux",
                                       sources={"agent": "base", "runtime": "same-runtime"})
    result["baseline"]["pair_id"] = "same-runner-pair"
    return result


def test_base_window_budget_accepts_independent_base_repeat_without_changing_other_limits():
    base, holdout = host_proof(), host_proof((5, 5, 5, 5, 5))
    policy = calibration(base)
    for caller in CALLERS:
        item = policy["callers"][caller]
        cadence = item["host_poll_cadence"]
        metric = item["metrics"][METRIC]
        windows = [row["process_observations"][-1]["poll_windows"][0]
                   for row in base["counters"] if row["caller"] == caller]
        durations = [window["duration_ns"] for window in windows]
        assert cadence["model"] == "base_control_host_poll_window_cadence.v1"
        assert cadence["producer"] == PRODUCER
        assert cadence["requested_period_ns"] == 1_000_000
        assert cadence["base_window_duration_ns"] == durations
        assert cadence["base_nonterminal_polls"] == [2, 3, 2, 3, 2]
        assert cadence["nominal_poll_limit"] == max(durations) // 1_000_000
        assert metric["limit"] == max(4, cadence["nominal_poll_limit"])
        assert metric["margin"] == metric["limit"] - 3
        assert item["metrics"][PER_POLL]["limit"] == 1
        delta = metric["limit"] - 4
        for name in PROPAGATED:
            assert item["metrics"][name]["limit"] == 6 + delta
        for name, value in item["metrics"].items():
            if name not in {METRIC, *PROPAGATED}:
                assert value["margin"] == (0 if value["group"] == "scoped_or_deterministic" else value["spread"])
    assert compare_calibrated(holdout, base, policy) == []


def test_shortened_requested_period_fails_even_below_every_sleep_volume_limit():
    base, actual = host_proof(), host_proof((3, 3, 3, 3, 3), period=0.0005)
    policy = calibration(base)
    failures = compare_calibrated(actual, base, policy)
    assert any(item["metric"] == CADENCE + ".minimum_requested_sleep_ns"
               and item["actual"] == 500_000 and item["limit"] == 1_000_000 for item in failures)
    assert not any(item["metric"] in {METRIC, *PROPAGATED, PER_POLL} for item in failures)


def test_extra_sleep_per_poll_is_rejected_inside_the_host_window_allowance():
    base, actual = host_proof(), host_proof((3, 3, 3, 3, 3), extra=True)
    failures = compare_calibrated(actual, base, calibration(base))
    assert any(item["metric"] == PER_POLL and item["actual"] == 2 and item["limit"] == 1 for item in failures)
    assert any(item["metric"] == CADENCE + ".unbalanced_windows" for item in failures)
    assert not any(item["metric"] in {METRIC, *PROPAGATED} for item in failures)


def test_sleep_outside_the_completed_window_cannot_borrow_its_allowance():
    base, actual = host_proof(), host_proof((3, 3, 3, 3, 3), outside=True)
    with pytest.raises(ValueError, match="host polling.*do not cover"):
        compare_calibrated(actual, base, calibration(base))


@pytest.mark.parametrize("loss", ["poll_windows", "sleep_requests", "result", "period", "carry_out", "ambiguous"])
def test_incomplete_host_cadence_coverage_is_unknown_not_zero(loss):
    base = host_proof()
    actual = copy.deepcopy(base)
    host = actual["counters"][0]["process_observations"][-1]
    if loss in {"poll_windows", "sleep_requests"}:
        host.pop(loss)
    elif loss == "result":
        host["poll_windows"][0]["missing_results"] = 1
    elif loss == "period":
        host["sleep_requests"][SITE]["unknown_calls"] = 1
    elif loss == "carry_out":
        host["poll_windows"][0]["carry_out"] = True
    else:
        host["poll_windows"][0]["ambiguous_children"] = True
    with pytest.raises(ValueError, match="host polling.*coverage"):
        compare_calibrated(actual, base, calibration(base))


def test_larger_range_bound_is_retained_and_candidate_windows_never_set_it():
    base = host_proof((2, 3, 4, 5, 6))
    # Model an ideal observed window with no guard/scheduling delay. This is
    # calibration arithmetic, independent of the real-wait observations above.
    for row in base["counters"]:
        window = row["process_observations"][-1]["poll_windows"][0]
        window["duration_ns"] = window["sleep_calls"] * 1_000_000
        window["poll_end_ns"] = window["interval_end_ns"] = window["poll_start_ns"] + window["duration_ns"]
    policy = calibration(base)
    for caller in CALLERS:
        assert policy["callers"][caller]["host_poll_cadence"]["nominal_poll_limit"] == 6
        assert policy["callers"][caller]["metrics"][METRIC]["limit"] == 10
    enlarged = copy.deepcopy(policy)
    enlarged["callers"][CALLERS[0]]["metrics"][METRIC]["limit"] += 1
    with pytest.raises(ValueError, match="only from.*base"):
        compare_calibrated(base, base, enlarged)


def test_observed_elimination_of_the_host_loop_remains_an_improvement():
    base = host_proof()
    assert compare_calibrated(proof((2, 2, 2, 2, 2)), base, calibration(base)) == []
