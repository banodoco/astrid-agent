"""Check meter-policy changes with the accepted base's copy of this script.

This is a path and source check, not a hostile-code sandbox. It never imports
or executes the proposed product, test configuration or proposed gate code.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
from pathlib import Path, PurePosixPath


PATTERNS = (
    "tests/_local_speed*.py", "tests/local_speed*.py",
    "tests/core/generation/test_local_speed*.py",
    "tests/integrations/*.py", "tests/helpers/*.py",
    "tests/fixtures/local-speed-baselines/*",
)
EXACT = {
    ".github/workflows/plan55-native-flush.yml",
    "tests/lanes.py", "tests/runpod_guard.py", "tests/process_groups.py",
    "tests/local_speed_native.wprp",
}
CONFIG_NAMES = {"pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg", "conftest.py"}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def protected(path):
    return (path in EXACT or PurePosixPath(path).name in CONFIG_NAMES
            or any(fnmatch.fnmatchcase(path, pattern) for pattern in PATTERNS))


def check_policy(root, base, head):
    base = git(root, "rev-parse", base + "^{commit}")
    head = git(root, "rev-parse", head + "^{commit}")
    if (git(root, "rev-parse", "HEAD") != base
            or git(root, "status", "--porcelain", "--untracked-files=no")):
        raise ValueError("meter policy must run from the clean accepted base checkout")
    changed = git(root, "diff", "--name-only", "--no-renames", base, head).splitlines()
    changes = sorted(path for path in changed if protected(path))
    if changes:
        raise ValueError("proposed meter policy/configuration changes require Lead review: "
                         + ", ".join(changes))
    return {"status": "PASS", "policy_source": base, "candidate": head,
            "protected_changes": [], "decision": "6979024059"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    try:
        result = check_policy(Path.cwd(), args.base, args.head)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
