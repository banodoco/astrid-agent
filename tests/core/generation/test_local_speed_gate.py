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


def test_regressed_candidate_cannot_become_its_own_reference():
    from tests._local_speed_baseline import approve_reference
    old = baseline()
    offered = copy.deepcopy(old)
    offered["callers"]["resident_sdk"]["overhead_seconds"]["median"] *= 1.2
    with pytest.raises(ValueError, match="cannot replace"):
        approve_reference(offered, old)
    assert approve_reference(old, old) == old


def test_old_reference_cannot_hide_a_new_counter_regression_against_the_pair():
    old = baseline()
    paired = copy.deepcopy(old)
    paired["callers"]["resident_sdk"]["counter_maxima"]["python.event_wait_attempt"] = 3
    candidate = copy.deepcopy(paired)
    assert compare_baseline(candidate, old, coverage_reference=paired) == []
    candidate["callers"]["resident_sdk"]["counter_maxima"]["python.event_wait_attempt"] += 1
    assert [r["metric"] for r in compare_baseline(candidate, old, coverage_reference=paired)] == ["python.event_wait_attempt"]


def test_gate_workflow_covers_harness_inputs_and_pins_all_direct_dev_requirements():
    import fnmatch
    from pathlib import Path
    import re
    import tomllib
    import yaml
    root = Path(__file__).resolve().parents[3]
    workflow = yaml.safe_load((root / ".github/workflows/plan55-native-flush.yml").read_text())
    trigger = workflow.get("on", workflow.get(True))["pull_request"]
    for path in ("tests/integrations/test_runpod_slot_path_cpu.py", "tests/integrations/test_workflow_file_placements_cpu.py",
                 "tests/integrations/local_speed_sources.py", "pyproject.toml"):
        assert any(fnmatch.fnmatchcase(path, pattern) for pattern in trigger["paths"]), path
    constraints = (root / "tests/fixtures/local-speed-baselines/dev-constraints.txt").read_text()
    pins = {line.split("==")[0].lower() for line in constraints.splitlines() if re.fullmatch(r"[\w.-]+==[\w.]+", line)}
    project = tomllib.loads((root / "pyproject.toml").read_text())
    for dependency in project["project"]["optional-dependencies"]["dev"]:
        name = re.match(r"[\w.-]+", dependency)[0].lower()
        assert name in pins or "==" in dependency or re.search(r"@[0-9a-f]{40}(?:#|$)", dependency), dependency


def test_meter_explicitly_selects_product_defaults_without_modifying_other_journeys():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    module = ast.parse((root / "tests/integrations/test_local_speed_meter.py").read_text())
    drives = [node for node in ast.walk(module) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_drive"]
    assert len(drives) == 1
    assert any(keyword.arg == "product_defaults" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True for keyword in drives[0].keywords)
