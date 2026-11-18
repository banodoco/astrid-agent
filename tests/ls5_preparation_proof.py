"""Retain the new unsent-prompt probes against immutable review head 04539d38."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from tests.ls5_rework_proof import git

BASELINE = "04539d38c13bc33d08ba943e42278b59a7eec2f8"
PROBES = "tests/core/generation/backends/test_comfy_preparation_rework.py"
PACK_PROBES = "tests/packs/vibecomfy/test_ls5_preparation_run.py"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    for name in ("vibe", "runtime", "lifecycle"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    baseline = Path(tempfile.mkdtemp(prefix="ls5-preparation-probes-")) / "agent"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "--detach", str(baseline), BASELINE], check=True)
    for relative in (PROBES, PACK_PROBES):
        shutil.copy2(root / relative, baseline / relative)
    git(baseline, "add", "--", PROBES, PACK_PROBES)
    product_diff = git(baseline, "diff", "HEAD", "--", "astrid", "pyproject.toml")
    assert not product_diff, "baseline product bytes changed"
    patch = subprocess.check_output(["git", "-C", str(baseline), "diff", "--cached", "--binary"])
    (output / "baseline-probes.patch").write_bytes(patch)
    subprocess.run(["git", "-C", str(root), "archive", "--format=zip",
                    "--output=" + str(output / "baseline-source.zip"), BASELINE], check=True)
    environment = baseline.with_name(baseline.name + ".env")
    environment.mkdir()
    for name in ("home", "tmp", "cache", "basetemp"):
        (environment / name).mkdir()
    inputs = {name: getattr(args, name).resolve() for name in ("vibe", "runtime", "lifecycle")}
    for name, target in {"venv": Path(sys.prefix), "vibecomfy": inputs["vibe"],
                         "runtime": inputs["runtime"], "lifecycle": inputs["lifecycle"]}.items():
        (environment / name).symlink_to(target, target_is_directory=True)
    (environment / "uv-dir").write_text(str(Path(sys.executable).parent) + "\n")
    prefix = "tests.core.generation.backends.test_comfy_preparation_rework::"
    expected = {
        *(prefix + f"test_stale_preparation_is_not_started_and_reprepares[{operation}-{expiry}]"
          for operation in ("system-stats", "upload", "head-view", "folder-paths")
          for expiry in ("peer-eof", "idle-age")),
        *(prefix + f"test_websocket_drop_before_post_is_not_started_and_reprepares[{api}-{point}]"
          for api, point in (("runtime", "before-begin"), ("direct", "before-begin"),
                             ("runtime", "after-begin"), ("direct", "after-begin"),
                             ("runtime", "before-dispatch"), ("direct", "before-dispatch"),
                             ("runtime", "after-attempt"))),
    }
    expected_pack = {"tests.packs.vibecomfy.test_ls5_preparation_run::"
                     f"test_unsent_preparation_retries_actual_pack_once[{boundary}]"
                     for boundary in ("preparation", "websocket")}
    summary = {
        "baseline_sha": git(baseline, "rev-parse", "HEAD"),
        "baseline_tree": git(baseline, "rev-parse", "HEAD^{tree}"),
        "product_diff": product_diff, "probes_sha256": hashlib.sha256(patch).hexdigest(),
        "inputs": {name: git(path, "rev-parse", "HEAD") for name, path in inputs.items()},
        "python": sys.version, "runs": {},
    }
    for label, arguments, required, count in (
        ("unit", [PROBES], expected, 17),
        ("pack", ["-m", "integration", PACK_PROBES], expected_pack, 2),
    ):
        command = ["bash", "scripts/worker-env/agent-pytest.sh", "-q", "-rA", *arguments,
                   "--junitxml=" + str(output / f"{label}.xml")]
        with (output / f"{label}.log").open("w", encoding="utf-8") as log:
            run = subprocess.run(command, cwd=baseline, stdout=log, stderr=subprocess.STDOUT,
                                 env={**os.environ, "PYTEST_ADDOPTS": ""}, timeout=300, check=False)
        (output / f"{label}.exit").write_text(str(run.returncode) + "\n")
        cases = list(ET.parse(output / f"{label}.xml").iter("testcase"))
        failed = {case.get("classname") + "::" + case.get("name") for case in cases
                  if case.find("failure") is not None}
        errors = [case.get("name") for case in cases if case.find("error") is not None]
        skipped = [case.get("name") for case in cases if case.find("skipped") is not None]
        summary["runs"][label] = {
            "command": command, "pytest_exit": run.returncode, "cases": len(cases),
            "failed": sorted(failed), "expected_failed": sorted(required), "errors": errors, "skipped": skipped,
            "expected_result": run.returncode == 1 and len(cases) == count and failed == required
                               and not errors and not skipped,
        }
    summary["result"] = ("EXPECTED FAILURES REPRODUCED" if all(row["expected_result"]
                         for row in summary["runs"].values()) else "UNEXPECTED PROBE RESULT")
    (output / "baseline-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    assert summary["result"] == "EXPECTED FAILURES REPRODUCED"


if __name__ == "__main__":
    main()
