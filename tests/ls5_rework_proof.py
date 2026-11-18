"""Reproduce the Lead's LS-5 probes on immutable 0a49ce48 product bytes.

This functional proof doesn't change or interpret the shared latency meter.
The expected failing pytest result is retained, with an exact failure-ID set.
"""
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

BASELINE = "0a49ce4823d9acd90360d3e1e91e54ad98d76f93"
PROBES = "tests/core/generation/backends/test_comfy_held_rework.py"
PACK_PROBE = "tests/packs/vibecomfy/test_ls5_rework_run.py"
CHILD_PROBE = "tests/core/generation/backends/test_comfy_held_transport.py::test_blocked_progress_consumer_cannot_hold_worker_exit"


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vibe", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--lifecycle", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    baseline = Path(tempfile.mkdtemp(prefix="ls5-review-probes-")) / "agent"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "--detach", str(baseline), BASELINE], check=True)
    copied = (PROBES, PACK_PROBE, "tests/helpers/ls5_comfy_peer.py")
    for relative in copied:
        shutil.copy2(root / relative, baseline / relative)
    git(baseline, "add", "--", *copied)
    product_diff = git(baseline, "diff", "HEAD", "--", "astrid", "pyproject.toml")
    assert not product_diff, "baseline product bytes changed"
    patch = subprocess.check_output(["git", "-C", str(baseline), "diff", "--cached", "--binary"])
    (output / "baseline-fixtures.patch").write_bytes(patch)
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
    command = ["bash", "scripts/worker-env/agent-pytest.sh", "-q", "-rA", PROBES, CHILD_PROBE,
               "--junitxml=" + str(output / "baseline.xml")]
    with (output / "baseline.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(command, cwd=baseline, stdout=log, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTEST_ADDOPTS": ""}, timeout=600, check=False)
    (output / "baseline.exit").write_text(str(result.returncode) + "\n")
    cases = list(ET.parse(output / "baseline.xml").iter("testcase"))
    failed = {case.get("classname") + "::" + case.get("name")
              for case in cases if case.find("failure") is not None}
    errors = [case.get("name") for case in cases if case.find("error") is not None]
    skipped = [case.get("name") for case in cases if case.find("skipped") is not None]
    prefix = "tests.core.generation.backends.test_comfy_held_rework::"
    expected = {
        prefix + "test_idle_post_is_typed_not_started_and_new_client_posts_once[runtime]",
        prefix + "test_idle_post_is_typed_not_started_and_new_client_posts_once[direct]",
        prefix + "test_idle_http_read_recovers_running_prompt_without_reposting",
        prefix + "test_live_http_connection_older_than_thirty_seconds_is_not_used",
        *(prefix + f"test_websocket_rejection_before_post_uses_bounded_fallback[{status}]"
          for status in (400, 403, 426, 500)),
        *(prefix + f"test_missing_or_invalid_initial_status_uses_bounded_fallback[{status}]"
          for status in ("None", "unexpected")),
        prefix + "test_http_connect_failure_keeps_existing_object_info_error",
        *(prefix + f"test_early_ready_history_falls_back_once_and_keeps_terminal_validation[{status}]"
          for status in ("empty", "pending", "queued", "running")),
        prefix + "test_stale_post_reprepares_with_fresh_target_facts_before_sending",
        prefix + "test_repreparation_is_bounded_and_never_repeats_a_started_prompt[always-stale]",
        prefix + "test_agent_declares_bounded_websockets_dependency",
        "tests.core.generation.backends.test_comfy_held_transport::test_blocked_progress_consumer_cannot_hold_worker_exit",
    }
    pack_command = ["bash", "scripts/worker-env/agent-pytest.sh", "-q", "-rA", "-m", "integration", PACK_PROBE,
                    "--junitxml=" + str(output / "baseline-pack.xml")]
    with (output / "baseline-pack.log").open("w", encoding="utf-8") as log:
        pack_result = subprocess.run(pack_command, cwd=baseline, stdout=log, stderr=subprocess.STDOUT,
                                    env={**os.environ, "PYTEST_ADDOPTS": ""}, timeout=180, check=False)
    (output / "baseline-pack.exit").write_text(str(pack_result.returncode) + "\n")
    pack_cases = list(ET.parse(output / "baseline-pack.xml").iter("testcase"))
    pack_failed = [case.get("name") for case in pack_cases if case.find("failure") is not None]
    pack_errors = [case.get("name") for case in pack_cases if case.find("error") is not None]
    pack_skipped = [case.get("name") for case in pack_cases if case.find("skipped") is not None]
    summary = {
        "baseline_sha": git(baseline, "rev-parse", "HEAD"),
        "baseline_tree": git(baseline, "rev-parse", "HEAD^{tree}"),
        "product_diff": product_diff,
        "fixture_patch_sha256": hashlib.sha256(patch).hexdigest(),
        "command": command, "cwd": str(baseline), "python": sys.version,
        "inputs": {name: git(path, "rev-parse", "HEAD") for name, path in inputs.items()},
        "pytest_exit": result.returncode, "cases": len(cases), "failed": sorted(failed),
        "expected_failed": sorted(expected), "errors": errors, "skipped": skipped,
        "pack_command": pack_command, "pack_pytest_exit": pack_result.returncode,
        "pack_failed": pack_failed, "pack_errors": pack_errors, "pack_skipped": pack_skipped,
        "result": "EXPECTED FAILURES REPRODUCED" if result.returncode == 1 and failed == expected
                  and not errors and not skipped and len(cases) == 25 and pack_result.returncode == 1
                  and pack_failed == ["test_stale_post_reprepares_actual_pack_and_preserves_one_acceptance"]
                  and len(pack_cases) == 1 and not pack_errors and not pack_skipped
                  else "UNEXPECTED PROBE RESULT",
    }
    (output / "baseline-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    assert summary["result"] == "EXPECTED FAILURES REPRODUCED"


if __name__ == "__main__":
    main()
