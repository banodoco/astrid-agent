"""Compare all 52 unchanged Windows target cases by ID and outcome.

The packet's 61 held/rework cases have their own pytest gate. This comparison
retains both raw target results and fails on drift, not on base-matching errors.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from tests.ls5_rework_proof import BASELINE

TARGET = "tests/core/generation/backends/test_comfy_target.py"
FIXTURE = "tests/core/generation/test_engine_endpoint_transport.py"


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def outcomes(path):
    result = {}
    for case in ET.parse(path).iter("testcase"):
        identity = case.get("classname") + "::" + case.get("name")
        assert identity not in result, "duplicate test identity"
        result[identity] = next((state.upper() for state in ("failure", "error", "skipped")
                                 if case.find(state) is not None), "PASS")
    return result


def run_target(root, output, label):
    environment = dict(os.environ)
    environment["PYTHONSAFEPATH"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join((str(root), environment["ASTRID_VIBECOMFY_CHECKOUT"]))
    imported = subprocess.check_output(
        [sys.executable, "-c", "import astrid.core.generation.backends.comfy_target as m; print(m.__file__)"],
        cwd=root, env=environment, text=True).strip()
    assert Path(imported).resolve() == (root / "astrid/core/generation/backends/comfy_target.py").resolve()
    command = [sys.executable, "-m", "pytest", "-q", "-rA", TARGET,
               "--junitxml=" + str(output / f"{label}-target.xml")]
    with (output / f"{label}-target.log").open("w", encoding="utf-8") as log:
        run = subprocess.run(command, cwd=root, env=environment, stdout=log,
                             stderr=subprocess.STDOUT, timeout=300, check=False)
    (output / f"{label}-target.exit").write_text(str(run.returncode) + "\n")
    return outcomes(output / f"{label}-target.xml"), run.returncode, command, imported


def require_parity(base_target, candidate_target):
    assert len(base_target) == len(candidate_target) == 52
    assert base_target == candidate_target, "new, removed or changed Windows target IDs require disposition"
    assert "SKIPPED" not in base_target.values()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    baseline = Path(tempfile.mkdtemp(prefix="ls5-windows-base-")) / "agent"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "--detach", str(baseline), BASELINE], check=True)
    unchanged = {}
    for relative in (TARGET, FIXTURE, "tests/conftest.py"):
        assert (root / relative).read_bytes() == (baseline / relative).read_bytes()
        unchanged[relative] = git(root, "hash-object", relative)
    assert not git(baseline, "status", "--porcelain"), "baseline checkout must remain immutable"
    candidate_target, candidate_exit, candidate_command, candidate_import = run_target(root, output, "candidate")
    base_target, base_exit, base_command, base_import = run_target(baseline, output, "baseline")
    summary = {
        "candidate_sha": git(root, "rev-parse", "HEAD"),
        "candidate_tree": git(root, "rev-parse", "HEAD^{tree}"),
        "baseline_sha": git(baseline, "rev-parse", "HEAD"),
        "baseline_tree": git(baseline, "rev-parse", "HEAD^{tree}"),
        "baseline_import": base_import, "candidate_import": candidate_import,
        "unchanged_test_blobs": unchanged,
        "baseline_command": base_command, "candidate_command": candidate_command,
        "baseline_pytest_exit": base_exit, "candidate_pytest_exit": candidate_exit,
        "baseline_target": base_target, "candidate_target": candidate_target,
        "all_target_outcomes_equal": base_target == candidate_target,
        "result": "target ID/outcome comparison; packet cases have a separate pytest gate",
    }
    (output / "windows-baseline-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    require_parity(base_target, candidate_target)
    for values, code in ((base_target, base_exit), (candidate_target, candidate_exit)):
        assert code == (1 if any(value != "PASS" for value in values.values()) else 0)


if __name__ == "__main__":
    main()
