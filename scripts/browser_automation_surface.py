#!/usr/bin/env python3
"""Fail closed at the absent installed App browser-automation boundary.

The current App has source-only Playwright coverage, but no installed command
implementing ``astrid-installed-browser-interface/v1``.  More importantly, its
Update action bypasses draft flush and excludes ``recovery-needed`` from update
availability.  Running the current button would therefore create an operation
which the accepted route cannot recover.  This command authenticates the App
candidate and refuses before opening a browser or sending an update request.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

PROTOCOL = "astrid-installed-browser-interface/v1"
SHA = re.compile(r"[0-9a-f]{40}")


class SurfaceError(RuntimeError):
    """The installed browser route is not safely executable."""


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SurfaceError(f"{label} must be an object")
    return dict(value)


def _command(argv: Sequence[str], label: str) -> str:
    try:
        result = subprocess.run(list(argv), capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SurfaceError(f"{label} is unavailable: {exc}") from exc
    if result.returncode:
        raise SurfaceError(f"{label} failed with exit {result.returncode}")
    return result.stdout


def _authenticate_app(path: Path, expected: object) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise SurfaceError("App checkout must be an absolute non-symlink directory")
    head = _command(["git", "-C", str(path), "rev-parse", "HEAD"], "App checkout").strip()
    if SHA.fullmatch(head) is None or head != expected:
        raise SurfaceError("App checkout differs from the requested candidate")
    if _command(
        ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"],
        "App checkout status",
    ).strip():
        raise SurfaceError("App checkout has tracked modifications")


def _validate(value: object) -> dict[str, Any]:
    envelope = _mapping(value, "interface envelope")
    if envelope.get("protocol") != PROTOCOL or envelope.get("surface") != "browser":
        raise SurfaceError("browser interface protocol or surface differs")
    request = _mapping(envelope.get("request"), "request")
    action = envelope.get("action")
    if request.get("action") != action:
        raise SurfaceError("request and interface action differ")
    if action not in {
        "button-unsaved-draft",
        "button-update-lost-ack",
        "button-update-explicit",
        "cleanup",
    }:
        raise SurfaceError(f"browser surface does not own action {action!r}")
    scenario = _mapping(request.get("scenario"), "scenario")
    actual = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    if scenario.get("os") != actual:
        raise SurfaceError("scenario OS does not match this host")
    handoff = envelope.get("handoff_id")
    if not isinstance(handoff, str) or len(handoff) != 64:
        raise SurfaceError("interface handoff identity is invalid")
    return envelope


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-checkout", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        envelope = _validate(json.load(sys.stdin))
        request = _mapping(envelope["request"], "request")
        candidate = _mapping(
            _mapping(request.get("inputs"), "inputs").get("candidate"), "candidate"
        )
        _authenticate_app(args.app_checkout, candidate.get("app"))
        action = envelope["action"]
        if action == "button-unsaved-draft":
            raise SurfaceError(
                "NEEDS: App must expose an installed draft-flush guard and browser command; "
                "the current Update action bypasses draft flush, so zero-update evidence cannot be observed"
            )
        if action in {"button-update-lost-ack", "button-update-explicit"}:
            raise SurfaceError(
                "NEEDS: App and Runtime must expose installed same-operation recovery before browser automation; "
                "the current recovery-needed state is unavailable to Update and no operation ID can be supplied"
            )
        raise SurfaceError("no owned browser session exists because every button route is blocked")
    except (SurfaceError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"browser_automation_surface: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
