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
    import re
    import tomllib
    from pathlib import Path

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


def test_windows_cannot_promote_a_reference_from_another_measurement_pair():
    from tests._local_speed_baseline import approve_reference
    old, candidate = baseline(), baseline()
    candidate["measurement"] = {"gate_version": 3}
    candidate["pair_id"] = "runner-pair-one"
    paired = copy.deepcopy(candidate)
    paired["pair_id"] = "runner-pair-two"
    with pytest.raises(ValueError, match="same runner pair"):
        approve_reference(candidate, old, coverage_reference=paired)


def test_edited_checkout_reference_cannot_grade_itself(tmp_path):
    import json
    import subprocess

    from tests.local_speed_paired_run import immutable_reference
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / "tests/fixtures/local-speed-baselines/linux.json"
    path.parent.mkdir(parents=True)
    prior = baseline()
    path.write_text(json.dumps(prior))
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Meter test", "-c", "user.email=meter@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "-qm", "reference"], check=True)
    commit = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    forged = copy.deepcopy(prior)
    forged["callers"]["resident_sdk"]["overhead_seconds"]["median"] *= 10
    path.write_text(json.dumps(forged))
    assert immutable_reference(tmp_path, commit, "Linux") == prior


def test_removed_request_is_reported_as_observed_zero_without_losing_its_counter():
    from tests._local_speed_baseline import make_baseline
    from tests.core.generation.test_local_speed_baseline import reports
    timing, counters = reports()
    reference = baseline()
    for row in counters:
        del row["attached_http_paths"]["GET /history/job-one"]
    candidate = make_baseline(timing, counters, system="Windows", sources={"agent": "reference"}, counter_vocabulary=reference)
    assert candidate["callers"]["resident_sdk"]["counter_maxima"]["comfy_http.GET /history/<prompt>"] == 0
    assert compare_baseline(candidate, reference) == []


def transition_cells():
    old, control, actual = baseline(), baseline(), baseline()
    old["tolerance"] = dict(LEGACY_TOLERANCE)
    actual["sources"]["agent"] = control["sources"]["agent"] = "candidate"
    actual["pair_id"] = control["pair_id"] = "same-runner-pair"
    actual["measurement"] = {"gate_version": 3, "harness_settings": {
        "host_poll_seconds": "product_default", "machine_poll_seconds": "product_default",
        "sdk_poll_seconds": "product_default",
    }}
    # Different configured cells are not one timing distribution. The paired
    # base uses the same defaults/observer as this candidate default cell.
    for value in actual["callers"].values():
        value["overhead_seconds"]["median"] = 30
        value["overhead_seconds"]["slowest"] = 32
    paired = copy.deepcopy(actual)
    paired["sources"]["agent"] = "base"
    return old, control, actual, paired


def test_measurement_transition_retains_the_old_gate_and_the_default_pair():
    from tests._local_speed_baseline import approve_reference
    old, control, actual, paired = transition_cells()
    assert compare_baseline(actual, old, coverage_reference=paired)
    assert approve_reference(actual, old, coverage_reference=paired, historical_control=control) is actual
    for field in ("median", "slowest"):
        regressed = copy.deepcopy(control)
        regressed["callers"]["resident_sdk"]["overhead_seconds"][field] *= 1.2
        with pytest.raises(ValueError, match="cannot replace"):
            approve_reference(actual, old, coverage_reference=paired, historical_control=regressed)
    slower_default = copy.deepcopy(actual)
    slower_default["callers"]["resident_sdk"]["overhead_seconds"]["median"] *= 1.2
    with pytest.raises(ValueError, match="contemporaneous"):
        approve_reference(slower_default, old, coverage_reference=paired, historical_control=control)


@pytest.mark.parametrize("field", ["sources", "pair_id", "system", "fixture"])
def test_historical_control_cannot_qualify_another_candidate_or_measurement(field):
    from tests._local_speed_baseline import approve_reference
    old, control, actual, paired = transition_cells()
    control[field] = {"agent": "base"} if field == "sources" else "different"
    with pytest.raises(ValueError, match="exact candidate"):
        approve_reference(actual, old, coverage_reference=paired, historical_control=control)


def test_historical_control_cannot_bypass_an_established_default_reference():
    from tests._local_speed_baseline import approve_reference
    _, control, actual, paired = transition_cells()
    with pytest.raises(ValueError, match="original measurement transition"):
        approve_reference(actual, paired, coverage_reference=paired, historical_control=control)


def test_historical_overlay_uses_immutable_harness_and_preserves_candidate_product(tmp_path):
    import hashlib
    import subprocess

    from tests.local_speed_paired_run import overlay_harness
    root, worktree = tmp_path / "repo", tmp_path / "control"
    root.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
    git("init", "-q")
    product, harness = root / "astrid/__init__.py", root / "tests/_local_speed_process.py"
    product.parent.mkdir()
    harness.parent.mkdir()
    product.write_text("base product\n", newline="\n")
    harness.write_text("original observer\n", newline="\n")
    git("add", ".")
    identity = ("-c", "user.name=Meter test", "-c", "user.email=meter@example.invalid", "-c", "commit.gpgsign=false")
    git(*identity, "commit", "-qm", "old")
    old = git("rev-parse", "HEAD")
    product.write_text("candidate product\n", newline="\n")
    harness.write_text("new observer\n", newline="\n")
    git("add", ".")
    git(*identity, "commit", "-qm", "candidate")
    git("worktree", "add", "--detach", str(worktree), "HEAD")
    try:
        hashes = overlay_harness(root, worktree, historical_ref=old)
        assert (worktree / "astrid/__init__.py").read_text() == "candidate product\n"
        assert (worktree / "tests/_local_speed_process.py").read_text() == "original observer\n"
        assert hashes == {"tests/_local_speed_process.py": hashlib.sha256(b"original observer\n").hexdigest()}
        (worktree / "astrid/__init__.py").write_text("tampered\n", newline="\n")
        with pytest.raises(ValueError, match="non-harness"):
            overlay_harness(root, worktree, historical_ref=old)
    finally:
        git("worktree", "remove", "--force", str(worktree))
