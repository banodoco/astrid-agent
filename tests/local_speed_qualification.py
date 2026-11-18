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
PLAN_BASE = "91cb4cb499306b2f09686de0fb67b803bcb89077"
PREVIOUS_PLAN_BASE = "d0217776fa05c6d55b953d0265ddafd918aafd2f"
PLAN_PREDECESSORS = {
    PLAN_BASE: PREVIOUS_PLAN_BASE,
    PREVIOUS_PLAN_BASE: "24c68a2702339583b7be1693d101146bed31cfa6",
}
REQUIRED_ANCESTOR = "6451821e18bd300484a921a425cafd856095b1a4"
PREVIOUS_CONTROLLER = "d0834eb62c9343a54a6f1731ae5e0213a4503644"
WORKFLOW = ".github/workflows/plan55-native-flush.yml"
PACKET_PATHS = {WORKFLOW, "tests/_local_speed_process.py", "tests/_local_speed_calibration.py",
                "tests/_local_speed_census.py", "tests/core/generation/test_local_speed_census.py",
                "tests/_local_speed_sdk_poll.py", "tests/core/generation/test_local_speed_sdk_poll.py",
                "tests/core/generation/test_local_speed_ambient_calibration.py",
                "tests/core/generation/test_local_speed_qualification.py",
                "tests/core/generation/test_local_speed_policy.py", "tests/local_speed_policy.py",
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


def _published_source(root, controller, plan_base, previous_plan_base=None):
    # Lead 6985581776 adds another accepted native plan boundary. Preserve
    # each recorded predecessor while following the original-tuple source.
    if previous_plan_base is None:
        previous_plan_base = PLAN_PREDECESSORS.get(plan_base)
    parents = git(root, "show", "-s", "--format=%P", controller).split()
    if len(parents) != 2:
        raise ValueError("published controller does not retain its ordered plan merge")
    if parents[1] == plan_base:
        earlier_parents = git(root, "show", "-s", "--format=%P", parents[0]).split()
        if (previous_plan_base is not None and len(earlier_parents) == 2
                and subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor",
                                    previous_plan_base, parents[0]], capture_output=True, check=False).returncode == 0):
            return _published_source(root, parents[0], previous_plan_base)
        return parents[0]
    previous_source = _published_source(root, parents[0], plan_base, previous_plan_base)
    if subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor",
                       previous_source, parents[1]], check=False).returncode:
        raise ValueError("published controller's original-tuple source did not extend its predecessor")
    return parents[1]


def _native_merge(root, controller, parents):
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
    return native_tree, conflicts


def validate_sources(root, controller, *, original_head=ORIGINAL_HEAD, plan_base=PLAN_BASE,
                     required_ancestor=REQUIRED_ANCESTOR, previous_controller=PREVIOUS_CONTROLLER,
                     previous_plan_base=PREVIOUS_PLAN_BASE):
    """Prove the merge and harness bytes before touching the worktree."""
    if git(root, "rev-parse", "HEAD") != controller or git(root, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("qualification controller must be the clean exact PR head")
    parents = git(root, "show", "-s", "--format=%P", controller).split()
    if len(parents) != 2:
        raise ValueError("qualification requires the ordered current-plan merge parents")
    retained_controller = None
    retained_followup = None
    if parents[1] == plan_base:
        source = parents[0]
        followup_parents = git(root, "show", "-s", "--format=%P", parents[0]).split()
        if len(followup_parents) == 2 and followup_parents[0] == previous_controller:
            # Lead 6983043853: retain the published controller, merge its
            # original-tuple fix, then merge the newly accepted plan head.
            # Both ordinary merges must reproduce; neither tuple is replaced.
            source = followup_parents[1]
            retained_controller = previous_controller
            previous_source = _published_source(root, previous_controller, previous_plan_base)
            for earlier, later in ((previous_source, source), (previous_plan_base, plan_base)):
                if subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor",
                                   earlier, later], check=False).returncode:
                    raise ValueError("qualification plan/source does not extend its published predecessor")
            tree, merge_conflicts = _native_merge(root, parents[0], followup_parents)
            retained_followup = {"controller": parents[0], "native_merge_tree": tree,
                                 "native_merge_conflicts": sorted(merge_conflicts),
                                 "previous_plan_base": previous_plan_base}
    elif parents[0] == previous_controller:
        # Merge an original-tuple follow-up into the published integrated
        # controller, preserving ordinary first-parent history without rebase.
        source = parents[1]
        retained_controller = previous_controller
        previous_source = _published_source(root, previous_controller, plan_base, previous_plan_base)
        if subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor",
                           previous_source, source], check=False).returncode:
            raise ValueError("qualification source does not extend the published controller's source")
    else:
        raise ValueError("qualification requires ordered current-plan parents or the exact published controller")
    for earlier, later in ((original_head, source), (required_ancestor, plan_base), (plan_base, controller)):
        if subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", earlier, later], check=False).returncode:
            raise ValueError("qualification is missing a required source ancestor")
    changed = set(git(root, "diff", "--name-only", original_head, source).splitlines())
    if not changed <= PACKET_PATHS:
        raise ValueError("qualification source changes protected product, dependency, reference or test bytes: "
                         + repr(sorted(changed - PACKET_PATHS)))

    native_tree, conflicts = _native_merge(root, controller, parents)

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
            "retained_followup_merge": retained_followup,
            "required_plan_ancestor": required_ancestor, "native_merge_tree": native_tree,
            "native_merge_conflicts": sorted(conflicts), "qualification_changed_paths": sorted(changed),
            "measurement_harness_sha256": hashes,
            "plan_added_harness_paths_not_in_original_tuple": sorted(PLAN_HARNESS_ADDITIONS),
            "new_plan_tuple_qualified": False,
            "decisions": {"census": "6970432614", "sdk_poll": "6981129059",
                          "original_tuple": "6967421223", "plan_merge": "6968915902",
                          "plan_advance": "6985581776", "previous_plan_advance": "6983043853"}}


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
