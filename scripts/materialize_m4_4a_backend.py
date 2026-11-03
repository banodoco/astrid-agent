"""Prepare runner-local M4-4a bindings; refuse until the native seam is approved.

This is not an executor config producer yet.  Integrate the M-PIN owned pinned
engine and busy/unknown control provider here only after their contracts are
reviewed; preserve the existing composer and executor validation at consumption.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

if __package__:
    from . import ci_journey
else:
    import ci_journey


def _git(checkout: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(checkout), *args], text=True, stderr=subprocess.PIPE
    ).strip()


def prepare_bindings(candidate: dict, checkouts: dict[str, Path], old_ref: str,
                     os_label: str, runner: str, shell: str) -> dict:
    """Build only verifiable bindings, never a partial executor config on disk."""
    if not isinstance(candidate, dict):
        raise ValueError("candidate must be an object")
    if set(checkouts) != {"app", "agent", "runtime", "pinokio"}:
        raise ValueError("all four candidate checkouts are required")
    identity = ci_journey.runner_identity()
    ci_journey.assert_runner_identity(identity)
    if (os_label != ci_journey.journey_os_label()
            or runner != identity["runner_image"]):
        raise ValueError("matrix OS/runner must match the observed runner")
    expected_shell = "powershell.exe" if os_label == "windows" else "sh"
    if shell != expected_shell:
        raise ValueError("matrix shell does not match the OS")
    bound = {}
    for name, checkout in checkouts.items():
        ref = candidate.get(name)
        if not isinstance(ref, str) or not re.fullmatch(r"[0-9a-f]{40}", ref):
            raise ValueError(f"candidate {name} must be a full commit SHA")
        if checkout.is_symlink() or not checkout.is_dir():
            raise ValueError(f"candidate {name} must be a non-symlink checkout")
        checkout = checkout.resolve(strict=True)
        if Path(_git(checkout, "rev-parse", "--show-toplevel")).resolve() != checkout:
            raise ValueError(f"candidate {name} must be the checkout root")
        if _git(checkout, "rev-parse", "HEAD") != ref:
            raise ValueError(f"candidate {name} checkout differs from frozen candidate")
        if _git(checkout, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError(f"candidate {name} checkout is not clean")
        bound[name] = {"path": str(checkout), "ref": ref}
    pinokio = Path(bound["pinokio"]["path"])
    old = ci_journey.validate_old_launcher_provenance(pinokio, old_ref)
    # Both arrays are reconstructed from immutable Git objects, never caller input.
    first = ci_journey.build_pinokio_action(pinokio, old_ref, app_directory=pinokio)["run"]
    retry = ci_journey.build_pinokio_action(pinokio, bound["pinokio"]["ref"],
                                            app_directory=pinokio)["run"]
    pins = {}
    for relative in ("pinokio/update.js", "pinokio/stop.js"):
        source = pinokio / relative
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"candidate Pinokio {relative} must be a regular file")
        blob = subprocess.check_output(["git", "-C", str(pinokio), "show",
                                        f'{bound["pinokio"]["ref"]}:{relative}'])
        if source.read_bytes() != blob:
            raise ValueError(f"candidate Pinokio {relative} differs from Git blob")
        pins[relative] = hashlib.sha256(blob).hexdigest()
    bound["pinokio"]["files"] = pins
    return {"platform": {"os": os_label, "runner": runner, "shell": shell,
                         "hosted": runner != "local"},
            "candidate_checkouts": bound, "old_launcher": old,
            "actions": {"first": {"steps": first}, "retry": {"steps": retry}}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    for name in ("app", "agent", "runtime", "pinokio"):
        parser.add_argument(f"--{name}-checkout", type=Path, required=True)
    parser.add_argument("--old-launcher-ref", required=True)
    parser.add_argument("--os", required=True)
    parser.add_argument("--runner", required=True)
    parser.add_argument("--shell", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("output path already exists; refusing a stale config")
        candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
        checkouts = {name: getattr(args, f"{name}_checkout")
                     for name in ("app", "agent", "runtime", "pinokio")}
        prepare_bindings(candidate, checkouts, args.old_launcher_ref,
                         args.os, args.runner, args.shell)
        raise RuntimeError(
            "NEEDS M-PIN: pinned approved Pinokio action-engine runner and typed "
            "busy/unknown setup, independent observation and ownership-bound restoration "
            "seam; no executor config was emitted. Integrate that reviewed seam with "
            "JourneyDriver.compose_pinokio_backend_config and this materializer."
        )
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"M4-4a materializer: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
