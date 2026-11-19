"""Retain the six-file reference and verify the accepted merged-plan source."""
from pathlib import Path
import json
import subprocess

BASE = "493616c1cfc7a83f18894dc81773571e977f26ef"
PIN = "9a3a9554294a7d6511f93e2111e7a96fd8564d6a"
REFERENCE = "666a52af46d3f5ad8efb4cf467f20085c913008b"
TREE = "92d39b0e78bfe49a0b569da4bf3c302732c0b491"
PLAN_REFERENCE = "e379565641414bc183cd02edebab1cbf77617931"
PLAN_TREE = "5ff15dcae73d78eaebb1e7b26929d85f241d6200"
FILES = (
    "astrid/packs/runpod/pins.json",
    "astrid/packs/runpod/recipe.linux-cu130.json",
    "astrid/packs/vibecomfy/environment.windows-nvidia.json",
    "astrid/packs/vibecomfy/extensions/generation.engine/comfyui.yaml",
    "astrid/packs/vibecomfy/pins.json",
    "astrid/packs/vibecomfy/recipe.windows-cu130.json",
)


def verify_historical_pin_reference(root):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()

    if git("show", "-s", "--format=%P", REFERENCE) != BASE:
        raise ValueError("reference must have exactly base493 as its parent")
    if git("rev-parse", REFERENCE + "^{tree}") != TREE:
        raise ValueError("reference tree differs from the approved six-file overlay")
    changed = git("diff", "--name-only", BASE, REFERENCE).splitlines()
    if changed != list(FILES):
        raise ValueError("reference changes more than the six approved pin files")
    blobs = {}
    for path in FILES:
        actual = git("rev-parse", REFERENCE + ":" + path)
        if actual != git("rev-parse", PIN + ":" + path):
            raise ValueError("reference pin differs from accepted 9a3a9554: " + path)
        blobs[path] = actual
    return {"decision": "6997218519", "base": BASE, "accepted_pin": PIN,
            "reference": REFERENCE, "reference_tree": TREE, "pin_blobs": blobs,
            "historical_493_d8_comparison": "NO-RESULT; dependency guard retained",
            "stored_reference_replacement": False}


def verify(root):
    historical = verify_historical_pin_reference(root)

    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()

    if git("rev-parse", PLAN_REFERENCE + "^{tree}") != PLAN_TREE:
        raise ValueError("plan reference differs from accepted destination e3795656")
    git("merge-base", "--is-ancestor", PLAN_REFERENCE, "HEAD")
    authorities = {}
    for path in (*FILES, "runtime.lock.json", "astrid/resources/runtime.lock.json"):
        expected = git("rev-parse", PLAN_REFERENCE + ":" + path)
        if git("rev-parse", "HEAD:" + path) != expected:
            raise ValueError("candidate dependency authority differs from accepted plan: " + path)
        authorities[path] = expected
    return {"decision": "6998870417", "count_reference_decision": "6999678949", "reference": PLAN_REFERENCE,
            "reference_tree": PLAN_TREE, "dependency_authority_blobs": authorities,
            "historical_pin_reference": historical,
            "historical_cross_pin_comparisons": "NO-RESULT; dependency guards retained",
            "stored_reference_replacement": False}


if __name__ == "__main__":
    print(json.dumps(verify(Path.cwd()), indent=2, sort_keys=True))
