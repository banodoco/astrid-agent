"""Retain an exact-base comparison for the unchanged Windows target tests.

The candidate pytest command remains a failing gate when it has errors. This
additional step only records whether its failures also occur at 0a49ce48.
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
TARGET_CLASS = "tests.core.generation.backends.test_comfy_target"


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    candidate = outcomes(output / "agent-transport.xml")
    candidate_target = {key: value for key, value in candidate.items() if key.startswith(TARGET_CLASS + "::")}
    packet = {key: value for key, value in candidate.items() if key not in candidate_target}
    baseline = Path(tempfile.mkdtemp(prefix="ls5-windows-base-")) / "agent"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "--detach", str(baseline), BASELINE], check=True)
    unchanged = {}
    for relative in (TARGET, FIXTURE, "tests/conftest.py"):
        assert (root / relative).read_bytes() == (baseline / relative).read_bytes()
        unchanged[relative] = git(root, "hash-object", relative)
    assert not git(baseline, "status", "--porcelain"), "baseline checkout must remain immutable"
    environment = dict(os.environ)
    environment["PYTHONSAFEPATH"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join((str(baseline), environment["ASTRID_VIBECOMFY_CHECKOUT"]))
    imported = subprocess.check_output(
        [sys.executable, "-c", "import astrid.core.generation.backends.comfy_target as m; print(m.__file__)"],
        cwd=baseline, env=environment, text=True).strip()
    assert Path(imported).resolve() == (baseline / "astrid/core/generation/backends/comfy_target.py").resolve()
    command = [sys.executable, "-m", "pytest", "-q", "-rA", TARGET,
               "--junitxml=" + str(output / "baseline-target.xml")]
    with (output / "baseline-target.log").open("w", encoding="utf-8") as log:
        run = subprocess.run(command, cwd=baseline, env=environment, stdout=log,
                             stderr=subprocess.STDOUT, timeout=300, check=False)
    (output / "baseline-target.exit").write_text(str(run.returncode) + "\n")
    base_target = outcomes(output / "baseline-target.xml")
    summary = {
        "candidate_sha": git(root, "rev-parse", "HEAD"),
        "candidate_tree": git(root, "rev-parse", "HEAD^{tree}"),
        "baseline_sha": git(baseline, "rev-parse", "HEAD"),
        "baseline_tree": git(baseline, "rev-parse", "HEAD^{tree}"),
        "baseline_import": imported, "unchanged_test_blobs": unchanged,
        "command": command, "baseline_pytest_exit": run.returncode,
        "baseline_target": base_target, "candidate_target": candidate_target,
        "packet_outcomes": packet,
        "all_target_outcomes_equal": base_target == candidate_target,
        "result": "comparison only; candidate pytest gate remains authoritative",
    }
    (output / "windows-baseline-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    assert len(base_target) == len(candidate_target) == 52
    assert base_target == candidate_target, "new or removed Windows target outcomes require disposition"
    assert "SKIPPED" not in base_target.values()
    assert run.returncode == (1 if any(value != "PASS" for value in base_target.values()) else 0)
    assert len(packet) == 61 and set(packet.values()) == {"PASS"}


if __name__ == "__main__":
    main()
