"""Adversarial LS-2.2 gate probes, independent of product optimization."""

import copy

import pytest

from tests._local_speed_baseline import LEGACY_TOLERANCE, compare_baseline
from tests.core.generation.test_local_speed_baseline import baseline


@pytest.mark.parametrize("seconds", [20.0, 0.02])
@pytest.mark.parametrize("statistic", ["median", "slowest"])
def test_twenty_percent_slowdown_fails_without_an_absolute_floor(seconds, statistic):
    reference = baseline()
    reference["callers"]["resident_sdk"]["overhead_seconds"][statistic] = seconds
    candidate = copy.deepcopy(reference)
    candidate["callers"]["resident_sdk"]["overhead_seconds"][statistic] *= 1.20
    assert [row["metric"] for row in compare_baseline(candidate, reference)] == [
        "overhead_seconds." + statistic
    ]


@pytest.mark.parametrize("counter", [
    "python.sleep_attempt", "runtime_commit_statements", "native.completed_flushes",
])
def test_one_extra_wait_commit_or_native_flush_fails(counter):
    reference = baseline()
    reference["callers"]["resident_sdk"]["counter_maxima"][counter] = 100
    candidate = copy.deepcopy(reference)
    candidate["callers"]["resident_sdk"]["counter_maxima"][counter] += 1
    assert [row["metric"] for row in compare_baseline(candidate, reference)] == [counter]


@pytest.mark.parametrize("counter", ["python.bundle_emission", "process_start.pack_worker"])
def test_vanished_reference_counter_is_unknown_and_fails(counter):
    reference = baseline()
    candidate = copy.deepcopy(reference)
    del candidate["callers"]["resident_sdk"]["counter_maxima"][counter]
    failures = compare_baseline(candidate, reference)
    assert [row["metric"] for row in failures] == [counter]
    assert failures[0]["actual"] is None


def test_recorded_legacy_tolerance_cannot_relax_the_current_gate():
    reference = baseline()
    reference["tolerance"] = dict(LEGACY_TOLERANCE)
    candidate = copy.deepcopy(reference)
    candidate["callers"]["resident_sdk"]["overhead_seconds"]["median"] *= 1.2
    assert compare_baseline(candidate, reference)


def test_resident_worker_with_observed_zero_starts_remains_an_improvement():
    reference = baseline()
    candidate = copy.deepcopy(reference)
    candidate["callers"]["resident_sdk"]["counter_maxima"]["process_start.pack_worker"] = 0
    candidate["callers"]["resident_sdk"]["overhead_seconds"].update(median=0.01, slowest=0.02)
    assert compare_baseline(candidate, reference) == []
