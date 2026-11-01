"""Fail-closed execution checks for the Ruff and mypy baseline wrappers."""

from __future__ import annotations

import importlib
import subprocess

import pytest


@pytest.mark.parametrize(
    ("module_name", "stdout", "stderr", "returncode"),
    [
        ("scripts.reshape.compare_ruff_baseline", "", "No module named ruff", 1),
        ("scripts.reshape.compare_mypy_baseline", "", "No module named mypy", 1),
    ],
)
def test_missing_tool_is_not_treated_as_zero_findings(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    stdout: str,
    stderr: str,
    returncode: int,
) -> None:
    module = importlib.import_module(module_name)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], returncode, stdout=stdout, stderr=stderr
        ),
    )

    with pytest.raises(RuntimeError, match="failed to execute"):
        module._run()


def test_ruff_findings_exit_one_remains_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("scripts.reshape.compare_ruff_baseline")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout='[{"code": "E501"}]', stderr=""
        ),
    )

    result = module._run()

    assert result["finding_count"] == 1


def test_mypy_findings_exit_one_remains_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("scripts.reshape.compare_mypy_baseline")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            1,
            stdout="astrid/example.py:7: error: Bad value  [assignment]\n",
            stderr="",
        ),
    )

    result = module._run()

    assert result["finding_count"] == 1
    assert result["code_counts"] == {"assignment": 1}
    assert "--no-pretty" in result["command"]


@pytest.mark.parametrize(
    ("current", "baseline", "code"),
    [(10, 10, 0), (11, 10, 1), (9, 10, 1)],
)
def test_count_baselines_are_exact_so_deleted_code_cannot_leave_slack(current: int, baseline: int, code: int) -> None:
    from scripts.reshape.count_baseline import verdict

    result, message = verdict("Ruff", current, baseline)

    assert result == code
    assert f"current={current} baseline={baseline}" in message
    if current < baseline:
        assert "--write-baseline" in message


def test_structure_violations_are_normalised_to_survive_unrelated_edits(tmp_path) -> None:
    from scripts.reshape.compare_structure_baseline import normalise

    assert normalise(f"{tmp_path}/astrid/core/x.py:1176 imports forbidden module 'm'", tmp_path) == (
        "astrid/core/x.py imports forbidden module 'm'"
    )
    traceback = f"invalid executor folder a/b: failed to extract folder executor metadata from {tmp_path}/a/b/executor.py: Traceback\n  File ..."
    assert normalise(traceback, tmp_path) == "invalid executor folder a/b: failed to extract folder executor metadata"


def test_structure_gate_fails_on_a_new_violation_and_on_a_fixed_one() -> None:
    from scripts.reshape.compare_structure_baseline import compare

    assert compare(["a", "b"], ["a", "b"]) == ([], [])
    assert compare(["a", "b", "c"], ["a", "b"]) == (["c"], [])
    assert compare(["a"], ["a", "b"]) == ([], ["b"])


def test_structure_baseline_script_runs_directly_from_outside_the_checkout(tmp_path) -> None:
    import os
    import sys

    from scripts.reshape.compare_structure_baseline import REPO_ROOT

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/reshape/compare_structure_baseline.py")],
        cwd=tmp_path,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Structure violations:" in result.stdout
    assert "No module named 'scripts'" not in result.stderr


def test_the_tree_has_exactly_the_baselined_structure_violations() -> None:
    import json

    from scripts.reshape.compare_structure_baseline import BASELINE_PATH, compare, current

    new, fixed = compare(current(), json.loads(BASELINE_PATH.read_text())["violations"])
    assert new == [], f"new structure violations: {new}"
    assert fixed == [], f"fixed violations still in {BASELINE_PATH.name}; regenerate with --write-baseline: {fixed}"
