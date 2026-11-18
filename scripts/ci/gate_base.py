"""Resolve the comparison merge base from an event's explicit destination."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def resolve(event_name, event):
    if event_name == "pull_request":
        comparison = event["pull_request"]["base"]["sha"]
    elif event_name == "push":
        comparison = event["before"]
    elif event_name == "workflow_dispatch":
        branch = event.get("inputs", {}).get("target_branch", "")
        if not re.fullmatch(r"(?:release/[0-9]+/(?:main|plan-[0-9]+)|main|release-1|int/[a-zA-Z0-9_-]+)", branch):
            raise ValueError("manual gate run requires an explicit destination target_branch")
        git("fetch", "--no-tags", "origin", f"refs/heads/{branch}")
        comparison = git("rev-parse", "FETCH_HEAD")
    else:
        raise ValueError(f"unsupported gate event: {event_name}")
    if not re.fullmatch(r"[0-9a-f]{40}", comparison) or comparison == "0" * 40:
        raise ValueError("event has no immutable comparison commit; dispatch with target_branch")
    return git("merge-base", "HEAD", comparison)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME"))
    parser.add_argument("--event-path", default=os.environ.get("GITHUB_EVENT_PATH"))
    args = parser.parse_args(argv)
    if not args.event_path:
        parser.error("--event-path or GITHUB_EVENT_PATH is required")
    base = resolve(args.event_name, json.loads(Path(args.event_path).read_text()))
    print(f"ASTRID_GATE_BASE={base}")
    if os.environ.get("GITHUB_ENV"):
        with Path(os.environ["GITHUB_ENV"]).open("a") as output:
            output.write(f"ASTRID_GATE_BASE={base}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
