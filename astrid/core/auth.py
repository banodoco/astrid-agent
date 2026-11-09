"""Astrid's user-facing Hivemind contributor authentication surface.

The Hivemind repository owns the browser/OAuth protocol. Astrid owns the
entrypoint users see and forwards the auth lifecycle to the managed source.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Sequence

AUTH_COMMANDS = ("login", "status", "logout", "revoke")
_AUTH_HELP_MARKER = "Manage contributor authentication"


class AuthCommandError(RuntimeError):
    """Raised when the managed Hivemind auth CLI is not available."""


def contributor_key_present() -> bool:
    """Return whether the host injected a contributor credential."""
    return bool(os.environ.get("HIVEMIND_CONTRIBUTOR_KEY", "").strip())


def contributor_auth_system_notice() -> str:
    """Return the dynamic Hivemind auth block for Astrid's system prompt."""

    state = "logged in" if contributor_key_present() else "not logged in"
    return (
        "Hivemind contributor login status: "
        f"{state}. Public Hivemind search and ordinary Astrid work remain available without login. "
        "Only contribution and ingestion actions require Discord contributor authentication. "
        "If the user asks to contribute while not logged in, say they are not logged in and offer "
        "`astrid login`; do not block unrelated work."
    )


def _auth_cli_candidates() -> list[list[str]]:
    candidates: list[list[str]] = []
    try:
        from astrid.core.pack.source_setup import active_sources

        for source in active_sources(verify=True):
            if source.pack_id != "hivemind":
                continue
            cli = source.pack_root / "cli.py"
            if cli.is_file():
                candidates.append([sys.executable, str(cli)])
    except Exception:
        # Optional source failures become an actionable auth error below.
        pass

    installed = shutil.which("hivemind")
    if installed:
        candidates.append([installed])
    return candidates


def _supports_auth(candidate: Sequence[str]) -> bool:
    try:
        result = subprocess.run(
            [*candidate, "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and _AUTH_HELP_MARKER in (result.stdout + result.stderr)


def _resolve_auth_cli(*, provision_if_missing: bool) -> list[str]:
    for candidate in _auth_cli_candidates():
        if _supports_auth(candidate):
            return candidate

    if provision_if_missing:
        try:
            from astrid.core.pack.source_setup import declarations_from_json, provision

            provision(declarations_from_json())
        except Exception as exc:
            raise AuthCommandError(
                "Astrid could not install the Hivemind authentication helper. "
                "Run the Astrid install line again, then retry."
            ) from exc
        for candidate in _auth_cli_candidates():
            if _supports_auth(candidate):
                return candidate

    from astrid.installer.service import default_program_root

    root = Path(os.environ.get("ASTRID_INSTALL_PROGRAM_ROOT") or default_program_root(Path.home(), sys.platform))
    if sys.platform == "win32":
        launcher = root / "run-astrid.cmd"
        login = f"& '{str(launcher).replace(chr(39), chr(39) * 2)}' login"
    else:
        login = f"{shlex.quote(str(root / 'run-astrid'))} login"
    raise AuthCommandError(
        "Astrid's Hivemind authentication helper is unavailable. "
        f"Run the Astrid install line again, then retry `{login}`."
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="astrid auth",
        description="Manage Astrid's Hivemind contributor authentication.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Open Discord approval and save a device key.")
    login.add_argument("--no-browser", action="store_true")
    login.add_argument("--machine", help="Machine label shown during approval.")
    login.add_argument("--ttl", type=int, default=600)
    login.add_argument("--timeout", type=float, default=600)
    login.add_argument("--interval", type=float, default=2.0)
    commands.add_parser("status", help="Show redacted local/server login status.")
    commands.add_parser("logout", help="Delete only the local contributor key.")
    commands.add_parser("revoke", help="Revoke the configured contributor key.")
    return parser


def run_auth(argv: list[str] | None = None) -> int:
    """Run ``astrid auth ...`` through the managed Hivemind CLI."""

    args = _parser().parse_args(argv)
    try:
        command = _resolve_auth_cli(provision_if_missing=args.command == "login")
    except AuthCommandError as exc:
        print(f"astrid auth: {exc}", file=sys.stderr)
        return 1

    forwarded = ["auth", args.command]
    if args.command == "login":
        if args.no_browser:
            forwarded.append("--no-browser")
        machine = args.machine or f"astrid-{socket.gethostname()}"
        forwarded.extend(["--machine", machine, "--ttl", str(args.ttl)])
        forwarded.extend(["--timeout", str(args.timeout), "--interval", str(args.interval)])

    try:
        return int(subprocess.run([*command, *forwarded], check=False).returncode)
    except OSError as exc:
        print(f"astrid auth: could not start Hivemind authentication: {exc}", file=sys.stderr)
        return 1


__all__ = [
    "AUTH_COMMANDS",
    "AuthCommandError",
    "contributor_auth_system_notice",
    "contributor_key_present",
    "run_auth",
]
