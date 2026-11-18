"""Require a named owner's approval of the exact head for gate policy changes.

Run this stdlib-only program from the trusted default branch, with python -I.
Candidate trees are read as Git blobs; they are never checked out or imported.
The repository variable ASTRID_GATE_OWNERS supplies the authorized GitHub logins.
The CLI without --pull-request is a local, unapproved policy check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.request

PREFIXES = ("scripts/reshape/", "scripts/ci/", ".github/workflows/", "requirements/")
FILES = {"scripts/__init__.py", "scripts/check_collection.py", "scripts/known_collection_errors.txt",
         "tests/lanes.py", "Makefile", "uv.lock", "poetry.lock", "Pipfile", "Pipfile.lock",
         "requirements.txt", ".python-version", "CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"}
TOOLS = {"ruff", "mypy", "pytest", "coverage", "types-pyyaml", "numpy"}


def git(*args):
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def tool_pins(revision):
    config = tomllib.loads(git("show", f"{revision}:pyproject.toml"))
    project = config.get("project", {})
    requirements = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        requirements.extend(group)
    pins = []
    for requirement in requirements:
        name = re.split(r"[\s\[<>=!~;@]", requirement, maxsplit=1)[0].lower().replace("_", "-")
        if name in TOOLS or name.startswith("pytest-"):
            pins.append(requirement)
    return sorted(pins)


def protected_changes(base, head):
    names = git("diff", "--no-renames", "--name-only", "-z", base, head).split("\0")
    protected = [name for name in names if name in FILES or name.startswith(PREFIXES)]
    if "pyproject.toml" in names and tool_pins(base) != tool_pins(head):
        protected.append("pyproject.toml (gate tool version pins)")
    return sorted(protected)


def approving_owner(head, owners, reviews):
    # A later change request/dismissal revokes an earlier approval. Comments do
    # not replace a formal review. An approval for an older head cannot unlock
    # any new bytes, even when GitHub has not auto-dismissed that review.
    latest = {}
    for review in sorted(reviews, key=lambda row: row["id"]):
        login = review.get("user", {}).get("login", "").lower()
        if login in owners and review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[login] = review
    return next((login for login, review in latest.items()
                 if review["state"] == "APPROVED" and review.get("commit_id") == head), None)


def api(repository, endpoint):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid GITHUB_REPOSITORY")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/{endpoint}",
        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                 "X-GitHub-Api-Version": "2022-11-28"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def pull_request_inputs(number):
    repository = os.environ["GITHUB_REPOSITORY"]
    pull = api(repository, f"pulls/{number}")
    if pull["base"]["repo"]["full_name"].lower() != repository.lower():
        raise ValueError("pull request does not target this repository")
    base, head = pull["base"]["sha"], pull["head"]["sha"]
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (base, head)):
        raise ValueError("pull request does not name immutable commits")
    git("fetch", "--no-tags", "origin", base)
    git("fetch", "--no-tags", "origin", f"refs/pull/{number}/head")
    # Resolve the fetched commit, not a moving worktree. A synchronize racing
    # this request must fail rather than check a different head than reviews.
    if git("rev-parse", "FETCH_HEAD") != head:
        raise ValueError("pull request head changed while fetching; rerun the check")
    reviews = []
    page = 1
    while True:
        batch = api(repository, f"pulls/{number}/reviews?per_page=100&page={page}")
        reviews.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    # Review policy must be configured outside the candidate branch.
    owners = {name.lower() for name in re.split(r"[\s,]+", os.environ.get("ASTRID_GATE_OWNERS", "")) if name}
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) for name in owners):
        raise ValueError("ASTRID_GATE_OWNERS must contain explicit GitHub logins")
    return base, head, owners, reviews


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=["controls"], default="controls")
    parser.add_argument("--base-ref")
    parser.add_argument("--head-ref", default="HEAD")
    parser.add_argument("--pull-request", type=int)
    args = parser.parse_args(argv)
    try:
        owners, reviews = set(), []
        if args.pull_request:
            if args.pull_request < 1 or args.base_ref or args.head_ref != "HEAD":
                raise ValueError("--pull-request must be positive and cannot override its GitHub base/head")
            base_ref, head_ref, owners, reviews = pull_request_inputs(args.pull_request)
        else:
            if not args.base_ref:
                raise ValueError("--base-ref or --pull-request is required")
            base_ref, head_ref = args.base_ref, args.head_ref
        head = git("rev-parse", "--verify", f"{head_ref}^{{commit}}")
        base = git("merge-base", base_ref, head)
        paths = protected_changes(base, head)
        print(f"Trusted gate policy: base={base} head={head}")
        for path in paths:
            print(f"Protected change: {path}")
        owner = approving_owner(head, owners, reviews)
        if paths and not owner:
            print("ERROR: protected gate changes require a configured named owner's approval of this exact head.")
            return 1
        print(f"Gate integrity passed; owner={owner or 'no protected changes'}")
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"Gate integrity failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
