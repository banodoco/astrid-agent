"""The reference gate must reject new work and never invent native coverage."""

import copy
import math

import pytest

from tests._local_speed_baseline import (
    CALLERS,
    TOLERANCE,
    compare_baseline,
    distribution,
    make_baseline,
)


def reports():
    latency, counters = [], []
    for caller in CALLERS:
        for seconds in (20.0, 21.0, 19.0, 22.0, 18.0):
            latency.append(
                {
                    "caller": caller,
                    "caller_seconds": seconds + 0.01,
                    "overhead_seconds": seconds,
                    "standin_queue_seconds": [0.0],
                    "standin_execution_seconds": [0.01],
                }
            )
        counters.append(
            {
                "caller": caller,
                "python_counts": {
                    "workflow_compile": 8,
                    "bundle_compile": 2,
                    "bundle_emission": 3,
                    "fsync_attempt": 6,
                    "fsync_success": 6,
                    "sleep_attempt": 20,
                },
                "process_start_counts": {
                    "pack_worker": 1,
                    "cli": int(caller == "fresh_python_cli"),
                },
                "attached_http_paths": {"POST /prompt": 1, "GET /history/job-one": 1},
                "output_hash_streams": 25,
                "runtime_main_connection_commit_statements": 10,
                "native_flushes": None,
            }
        )
    return latency, counters


def baseline():
    timing, counts = reports()
    return make_baseline(timing, counts, system="Windows", sources={"agent": "reference"})


def test_repeated_distribution_keeps_median_slowest_and_sample_count():
    value = baseline()
    assert value["callers"]["resident_sdk"]["overhead_seconds"] == {
        "samples": 5,
        "median": 20.0,
        "slowest": 22.0,
    }
    assert value["callers"]["resident_sdk"]["counter_maxima"]["python_forced_write_requests"] == 6
    assert value["native_windows_flushes"].startswith("unqualified")
    assert value["callers"]["resident_sdk"]["native_flush_coverage"] == "unobserved"
    assert value["budget_certified"] is False
    assert compare_baseline(value, value) == []


@pytest.mark.parametrize(
    "metric",
    [
        "process_start.pack_worker",
        "process_start.cli",
        "process_start.external",
        "python.workflow_compile",
        "python.bundle_emission",
        "output_hash_streams",
        "python_forced_write_requests",
        "comfy_http.GET /history/<prompt>",
    ],
)
def test_one_extra_deterministic_operation_is_a_regression(metric):
    reference = baseline()
    candidate = copy.deepcopy(reference)
    counts = candidate["callers"]["resident_sdk"]["counter_maxima"]
    counts[metric] = counts.get(metric, 0) + 1
    failures = compare_baseline(candidate, reference)
    assert [row["metric"] for row in failures] == [metric]


def test_latency_tolerance_is_explicit_and_speedups_are_allowed():
    reference = baseline()
    candidate = copy.deepcopy(reference)
    candidate["callers"]["resident_sdk"]["overhead_seconds"]["median"] = 20 * 1.5 + 3
    assert compare_baseline(candidate, reference) == []
    candidate["callers"]["resident_sdk"]["overhead_seconds"]["median"] += 0.001
    assert compare_baseline(candidate, reference)[0]["metric"] == "overhead_seconds.median"
    candidate["callers"]["resident_sdk"]["overhead_seconds"].update(median=0.01, slowest=0.02)
    assert compare_baseline(candidate, reference) == []


def test_background_counts_have_a_separate_disclosed_allowance():
    reference = baseline()
    candidate = copy.deepcopy(reference)
    counts = candidate["callers"]["resident_sdk"]["counter_maxima"]
    limit = math.ceil(20 * (1 + TOLERANCE["background_relative"])) + TOLERANCE["background_count"]
    counts["python.sleep_attempt"] = limit
    assert compare_baseline(candidate, reference) == []
    counts["python.sleep_attempt"] += 1
    assert compare_baseline(candidate, reference)[0]["metric"] == "python.sleep_attempt"


def test_native_flushes_are_gated_with_the_background_allowance():
    latency, counts = reports()
    for row in counts:
        row["native_flushes"] = 10
    reference = make_baseline(latency, counts, system="Linux", sources={})
    assert reference["callers"]["resident_sdk"]["native_flush_coverage"] == "observed"
    candidate = copy.deepcopy(reference)
    maximum = candidate["callers"]["resident_sdk"]["counter_maxima"]
    maximum["native.completed_flushes"] = 25
    assert compare_baseline(candidate, reference) == []
    maximum["native.completed_flushes"] = 26
    assert compare_baseline(candidate, reference)[0]["metric"] == "native.completed_flushes"


def test_partial_native_coverage_cannot_produce_a_reference():
    latency, counts = reports()
    extra = copy.deepcopy(counts[0])
    extra["native_flushes"] = 10
    counts.append(extra)
    with pytest.raises(ValueError, match="native coverage differs"):
        make_baseline(latency, counts, system="Linux", sources={})


@pytest.mark.parametrize(
    "change",
    [
        "system",
        "tolerance",
        "fixture",
        "samples",
        "counter_samples",
        "caller",
        "dependency",
        "native_coverage",
    ],
)
def test_incomparable_baselines_are_refused(change):
    reference = baseline()
    candidate = copy.deepcopy(reference)
    if change == "system":
        candidate["system"] = "Linux"
    elif change == "tolerance":
        reference["tolerance"]["median_relative"] = 9
    elif change == "fixture":
        candidate["fixture"] = "different payload"
    elif change == "samples":
        candidate["callers"]["resident_sdk"]["overhead_seconds"]["samples"] = 1
    elif change == "counter_samples":
        candidate["callers"]["resident_sdk"]["counter_samples"] = 0
    elif change == "caller":
        del reference["callers"]["resident_sdk"]
    elif change == "dependency":
        candidate["sources"]["runtime"] = "different-pin"
    elif change == "native_coverage":
        reference["callers"]["resident_sdk"]["native_flush_coverage"] = "observed"
    with pytest.raises(ValueError):
        compare_baseline(candidate, reference)


def test_failed_forced_write_is_not_a_successful_counter_sample():
    latency, counts = reports()
    counts[0]["python_counts"]["fsync_error"] = 1
    with pytest.raises(ValueError, match="forced-write request failed"):
        make_baseline(latency, counts, system="Windows", sources={})


@pytest.mark.parametrize("values", [[], [float("nan")], [float("inf")], [-1]])
def test_invalid_samples_do_not_produce_statistics(values):
    with pytest.raises(ValueError):
        distribution(values)
