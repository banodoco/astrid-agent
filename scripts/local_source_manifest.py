#!/usr/bin/env python3
"""Build the official installer manifest for this checkout and a selected ref.

The generated manifest pins source commits and tool assets locally; it never
downloads the published manifest from astrid.haus.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def git_commit(repository: Path, ref: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-ref", default="HEAD", help="agent Git ref to install (default: HEAD)")
    parser.add_argument("--agent-repo", type=Path, default=ROOT, help="local Astrid agent repository")
    parser.add_argument("--runtime-repo", type=Path, help="optional local Runtime repository override")
    parser.add_argument("--output", type=Path, help="write JSON to this file instead of stdout")
    args = parser.parse_args()

    agent_repo = args.agent_repo.expanduser().resolve()
    runtime_lock = json.loads((ROOT / "runtime.lock.json").read_text(encoding="utf-8"))
    manifest = {
        "schema_version": 1,
        "release": 0,
        "agent": {"sha": git_commit(agent_repo, args.agent_ref), "repo": str(agent_repo)},
        "runtime": {"sha": runtime_lock["source_commit"]},
        "tools": json.loads((ROOT / "scripts/install-tools.json").read_text(encoding="utf-8")),
    }
    if args.runtime_repo is not None:
        runtime_repo = args.runtime_repo.expanduser().resolve()
        manifest["runtime"]["repo"] = str(runtime_repo)
        manifest["runtime"]["sha"] = git_commit(runtime_repo, runtime_lock["source_commit"])
    payload = json.dumps(manifest, indent=2) + "\n"
    if args.output:
        args.output.expanduser().resolve().write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
