"""Validate the integrated packet before selecting its original-tuple parent.

This handoff is only for R14-P55-10. It does not change the paired runner or
any dependency, saved-reference, product-source or timing guard.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import subprocess
from pathlib import Path

from tests.local_speed_paired_run import HARNESS_EXACT, HARNESS_PATTERNS

ORIGINAL_HEAD = "b18080bce998b62656cf9f908afd3d0560c9d18f"
PLAN_BASE = "24c68a2702339583b7be1693d101146bed31cfa6"
REQUIRED_ANCESTOR = "6451821e18bd300484a921a425cafd856095b1a4"
PREVIOUS_CONTROLLER = "d84e72760086c60f51a2874d5e46ddea3be8e37b"
WORKFLOW = ".github/workflows/plan55-native-flush.yml"
PACKET_PATHS = {WORKFLOW, "tests/_local_speed_process.py", "tests/_local_speed_calibration.py",
                "tests/_local_speed_census.py", "tests/core/generation/test_local_speed_census.py",
                "tests/core/generation/test_local_speed_qualification.py",
                "tests/local_speed_qualification.py", "tests/fixtures/local-speed-baselines/README.md"}
PLAN_HARNESS_ADDITIONS = {"tests/helpers/ls5_comfy_peer.py", "tests/integrations/test_ls5_meter.py"}
PIN_READER = '''      - name: Read the pinned Runtime commit
        id: runtime-pin
        shell: python
        run: |
          import json, os, pathlib
          commit = json.loads(pathlib.Path("runtime.lock.json").read_text())["source_commit"]
          with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
              print("commit=" + commit, file=output)
'''
PIN_REFERENCE = "          ref: ${{ steps.runtime-pin.outputs.commit }}\n"


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def blob(root, revision, path):
    return subprocess.check_output(["git", "-C", str(root), "show", revision + ":" + path])


def _harness_paths(root, revision):
    return {path for path in git(root, "ls-tree", "-r", "--name-only", revision, "--", "tests").splitlines()
            if path in HARNESS_EXACT or any(fnmatch.fnmatchcase(path, pattern) for pattern in HARNESS_PATTERNS)}


def validate_sources(root, controller, *, original_head=ORIGINAL_HEAD, plan_base=PLAN_BASE,
                     required_ancestor=REQUIRED_ANCESTOR, previous_controller=PREVIOUS_CONTROLLER):
    """Prove the merge and harness bytes before touching the worktree."""
    if git(root, "rev-parse", "HEAD") != controller or git(root, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("qualification controller must be the clean exact PR head")
    parents = git(root, "show", "-s", "--format=%P", controller).split()
    if len(parents) != 2:
        raise ValueError("qualification requires the ordered current-plan merge parents")
    retained_controller = None
    if parents[1] == plan_base:
        source = parents[0]
    elif parents[0] == previous_controller:
        # Merge an original-tuple follow-up into the published integrated
        # controller, preserving ordinary first-parent history without rebase.
        source = parents[1]
        retained_controller = previous_controller
        previous = git(root, "show", "-s", "--format=%P", previous_controller).split()
        if (len(previous) != 2 or previous[1] != plan_base
                or subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor",
                                   previous[0], source], check=False).returncode):
            raise ValueError("qualification source does not extend the published controller's source")
    else:
        raise ValueError("qualification requires ordered current-plan parents or the exact published controller")
    for earlier, later in ((original_head, source), (required_ancestor, plan_base)):
        if subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", earlier, later], check=False).returncode:
            raise ValueError("qualification is missing a required source ancestor")
    changed = set(git(root, "diff", "--name-only", original_head, source).splitlines())
    if not changed <= PACKET_PATHS:
        raise ValueError("qualification source changes protected product, dependency, reference or test bytes: "
                         + repr(sorted(changed - PACKET_PATHS)))

    native = subprocess.run(["git", "-C", str(root), "merge-tree", "--write-tree", "--name-only",
                             "--no-messages", *parents], capture_output=True, text=True, check=False)
    lines = native.stdout.splitlines()
    if native.returncode not in (0, 1) or not lines:
        raise ValueError("native merge cannot be reproduced: " + native.stderr)
    native_tree, conflicts = lines[0], {line for line in lines[1:] if line}
    if conflicts - {WORKFLOW}:
        raise ValueError("qualification has a conflict outside the named workflow")
    differences = set(git(root, "diff", "--name-only", native_tree, controller).splitlines())
    if differences - {WORKFLOW}:
        raise ValueError("integrated product differs from the accepted native merge")

    workflow = blob(root, source, WORKFLOW)
    accepted = blob(root, plan_base, WORKFLOW)
    if workflow != blob(root, controller, WORKFLOW):
        raise ValueError("integrated workflow differs from the qualified workflow")
    for marker in (PIN_READER.encode(), PIN_REFERENCE.encode()):
        if accepted.count(marker) != 1 or workflow.count(marker) != 1:
            raise ValueError("accepted Runtime-pin workflow behavior was not retained")
    selector = b"-m tests.local_speed_qualification"
    if workflow.count(selector) != 1 or workflow.index(selector) > workflow.index(PIN_READER.encode()):
        raise ValueError("original-tuple source must be selected before reading its Runtime pin")

    source_paths, integrated_paths = _harness_paths(root, source), _harness_paths(root, controller)
    if source_paths - integrated_paths or integrated_paths - source_paths != PLAN_HARNESS_ADDITIONS:
        raise ValueError("qualification harness path coverage differs")
    hashes = {}
    for path in sorted(source_paths):
        content = blob(root, source, path)
        if content != blob(root, controller, path):
            raise ValueError("measurement harness differs between integrated and qualified sources: " + path)
        hashes[path] = hashlib.sha256(content).hexdigest()
    for path in PLAN_HARNESS_ADDITIONS:
        if blob(root, controller, path) != blob(root, plan_base, path):
            raise ValueError("accepted added plan harness differs: " + path)
    return {"schema": "astrid.local-speed-qualification.v1", "controller": controller,
            "controller_tree": git(root, "rev-parse", controller + "^{tree}"),
            "qualification": source, "qualification_tree": git(root, "rev-parse", source + "^{tree}"),
            "original_head": original_head, "plan_base": plan_base,
            "previous_controller_preserved": retained_controller,
            "required_plan_ancestor": required_ancestor, "native_merge_tree": native_tree,
            "native_merge_conflicts": sorted(conflicts), "qualification_changed_paths": sorted(changed),
            "measurement_harness_sha256": hashes,
            "plan_added_harness_paths_not_in_original_tuple": sorted(PLAN_HARNESS_ADDITIONS),
            "new_plan_tuple_qualified": False,
            "decisions": {"census": "6970432614", "original_tuple": "6967421223", "plan_merge": "6968915902"}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--controller", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--checkout", action="store_true")
    args = parser.parse_args()
    root = Path.cwd().resolve()
    receipt = validate_sources(root, args.controller)
    args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.checkout:
        subprocess.run(["git", "checkout", "--detach", receipt["qualification"]], cwd=root, check=True)
        if git(root, "rev-parse", "HEAD") != receipt["qualification"] or git(root, "status", "--porcelain", "--untracked-files=no"):
            raise ValueError("qualification checkout differs from its verified clean source")
    print(json.dumps(receipt, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
