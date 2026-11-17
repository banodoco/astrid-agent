"""Compare two exact products using one harness and one hosted runner.

Historical references come from the PR base, never the proposed replacement.
Both products retain normal warmup/output/task proof and separate counter runs.
The temporary base has a named test-only overlay; its product bytes are checked.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from tests._local_speed_baseline import approve_reference, compare_baseline


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def harness_files(root):
    patterns = ("tests/_local_speed*.py", "tests/local_speed*.py", "tests/local_speed_native.wprp",
                "tests/core/generation/test_local_speed*.py", "tests/integrations/*.py", "tests/helpers/*.py")
    files = {path.relative_to(root) for pattern in patterns for path in root.glob(pattern)}
    files.update(Path(name) for name in ("tests/conftest.py", "tests/lanes.py", "tests/runpod_guard.py", "tests/process_groups.py"))
    return sorted(files)


def immutable_reference(root, base, system):
    path = "tests/fixtures/local-speed-baselines/" + system.lower() + ".json"
    return json.loads(git(root, "show", base + ":" + path))


def measure(root, output, native, vocabulary):
    command = [sys.executable, "-m", "tests.local_speed_meter_run", "--output", str(output),
               "--samples", "5", "--counter-samples", "2", "--timeout", "1800", "--native", native,
               "--counter-vocabulary", str(vocabulary)]
    # The current directory, not an installed editable package, must supply the
    # product. This is checked again in the child before collecting any samples.
    check = "import pathlib,astrid,astrid_runtime_client; r=pathlib.Path.cwd().resolve(); assert pathlib.Path(astrid.__file__).resolve().is_relative_to(r); assert pathlib.Path(astrid_runtime_client.__file__).resolve().is_relative_to(r)"
    subprocess.run([sys.executable, "-c", check], cwd=root, check=True)
    completed = subprocess.run(command, cwd=root, check=False)
    if completed.returncode:
        raise RuntimeError(f"measurement failed ({completed.returncode}): {output}")
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    if summary.get("status") != "PASS":
        raise ValueError("incomplete paired measurement")
    return summary["baseline"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root, output = Path.cwd().resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    summary = {"schema": "astrid.local-speed-pair.v1", "status": "NO-RESULT", "budget_certified": False,
               "python": sys.version, "platform": platform.platform(), "base": args.base,
               "candidate": git(root, "rev-parse", "HEAD"),
               "runner": {name: os.environ.get(name) for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "RUNNER_NAME", "ImageOS", "ImageVersion")}}
    base_parent = Path(tempfile.mkdtemp(prefix="p55-pair-", dir=os.environ.get("RUNNER_TEMP")))
    base_root = base_parent / "base"
    worktree_created = False
    previous_pair = os.environ.get("ASTRID_LS2_PAIR_ID")
    os.environ["ASTRID_LS2_PAIR_ID"] = summary["pair_id"] = str(uuid.uuid4())
    try:
        if git(root, "status", "--porcelain", "--untracked-files=no"):
            raise ValueError("candidate must be the clean exact source being tested")
        base_sha = git(root, "rev-parse", args.base + "^{commit}")
        summary["base"] = base_sha
        old = immutable_reference(root, base_sha, platform.system())
        (output / "old-reference.json").write_text(json.dumps(old, indent=2) + "\n")
        summary["old_reference_sha256"] = hashlib.sha256((output / "old-reference.json").read_bytes()).hexdigest()
        subprocess.run(["git", "worktree", "add", "--detach", str(base_root), base_sha], cwd=root, check=True)
        worktree_created = True
        overlay = {}
        for relative in harness_files(root):
            source, destination = root / relative, base_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            overlay[relative.as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
            if hashlib.sha256(destination.read_bytes()).hexdigest() != overlay[relative.as_posix()]:
                raise ValueError("paired harness bytes differ")
        changed = git(base_root, "diff", "--name-only").splitlines()
        if any(path not in overlay for path in changed):
            raise ValueError("paired reference has a non-harness modification")
        summary["harness_sha256"] = overlay
        summary["base_tree"] = git(base_root, "rev-parse", "HEAD^{tree}")
        summary["candidate_tree"] = git(root, "rev-parse", "HEAD^{tree}")
        freeze = subprocess.check_output([sys.executable, "-m", "pip", "freeze", "--all"], text=True)
        (output / "pip-freeze.txt").write_text(freeze)
        native = "linux"
        paired = measure(base_root, output / "reference", native, output / "old-reference.json")
        actual = measure(root, output / "candidate", native, output / "reference/baseline.json")
        if subprocess.check_output([sys.executable, "-m", "pip", "freeze", "--all"], text=True) != freeze:
            raise ValueError("dependency environment changed between paired measurements")
        summary["historical_regressions"] = compare_baseline(actual, old, coverage_reference=paired)
        summary["paired_regressions"] = compare_baseline(actual, paired)
        summary["reference_product"] = paired["sources"]
        summary["candidate_product"] = actual["sources"]
        if paired["sources"]["agent"] != base_sha or actual["sources"]["agent"] != summary["candidate"]:
            raise ValueError("measured product source differs from the requested pair")
        # A proposed replacement can never become the reference for its own run.
        # Historical comparison has already happened against immutable base Git.
        approved = approve_reference(actual, old, coverage_reference=paired)
        (output / "eligible-reference.json").write_text(json.dumps(approved, indent=2) + "\n")
        summary["status"] = "PASS"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        summary["error"] = f"{type(error).__name__}: {error}"
    finally:
        if previous_pair is None:
            os.environ.pop("ASTRID_LS2_PAIR_ID", None)
        else:
            os.environ["ASTRID_LS2_PAIR_ID"] = previous_pair
        summary["finished_epoch_ns"] = time.time_ns()
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        if worktree_created:
            # This is the temporary worktree created above, containing only the
            # checked test overlay. No caller-owned checkout is reset/removed.
            subprocess.run(["git", "worktree", "remove", "--force", str(base_root)], cwd=root, check=False)
        base_parent.rmdir()
    print(json.dumps(summary, indent=2))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
