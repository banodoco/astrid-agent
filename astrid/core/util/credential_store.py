"""Securely set credentials in Astrid's shared user-level ``astrid.env``."""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
import tempfile
import warnings
from contextlib import suppress
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values

from astrid.core.util.credentials_scope import _PROVIDER_ENV
from astrid.core.util.secrets import astrid_env_file_path


def _credential_variable(provider: str) -> str:
    variable = _PROVIDER_ENV.get(provider, provider)
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", variable):
        available = ", ".join(sorted(_PROVIDER_ENV))
        raise ValueError(
            f"use a supported provider ({available}) or an uppercase environment-variable name"
        )
    return variable


def _serialize_value(value: str) -> str:
    """Quote a literal single-line value using python-dotenv's syntax."""

    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def store_credential(provider: str, value: str, *, env_file: Path | None = None) -> Path:
    """Atomically store one provider value, preserving other file contents."""

    variable = _credential_variable(provider)

    value = value.strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("credential must be a non-empty single-line value")

    target = (env_file or astrid_env_file_path()).expanduser()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError("refusing to write a credential file through a symbolic link")

    try:
        original = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = ""
    except OSError as exc:
        raise ValueError(f"could not read the shared Astrid environment file: {exc}") from exc

    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(variable)}\s*=")
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines(keepends=True)
    updated: list[str] = []
    replaced = False
    for line in lines:
        if pattern.match(line):
            if replaced:
                continue
            updated.append(f"{variable}={_serialize_value(value)}{newline}")
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        if updated and not updated[-1].endswith(("\n", "\r")):
            updated[-1] += newline
        updated.append(f"{variable}={_serialize_value(value)}{newline}")

    # Refuse to claim success if the same dotenv parser used by the resolver
    # would change the credential (for example, through ${NAME} expansion).
    parsed = dotenv_values(stream=StringIO("".join(updated)), interpolate=False)
    if parsed.get(variable) != value:
        raise ValueError("credential cannot be represented safely in astrid.env")

    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".astrid-env-", dir=target.parent)
    temp_path = Path(temp_name)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.writelines(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        if os.name != "nt":
            target.chmod(0o600)
    finally:
        with suppress(OSError):
            temp_path.unlink(missing_ok=True)
    return target


def list_credential_names(*, env_file: Path | None = None) -> list[str]:
    """Return sorted names from the shared store without exposing values."""

    target = (env_file or astrid_env_file_path()).expanduser()
    if target.is_symlink():
        raise ValueError("refusing to read a credential file through a symbolic link")
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ValueError(f"could not read the shared Astrid environment file: {exc}") from exc
    parsed = dotenv_values(stream=StringIO(text), interpolate=False)
    return sorted(name for name, value in parsed.items() if value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="astrid-credential",
        description="Store an API credential in Astrid's shared user-level astrid.env file.",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("list", help="list stored credential names (values are never shown)")
    set_parser = subparsers.add_parser("set", help="prompt for and store a provider credential")
    supported = ", ".join(sorted(_PROVIDER_ENV))
    set_parser.add_argument(
        "provider",
        help=f"provider ({supported}) or uppercase environment-variable name",
    )
    args = parser.parse_args(argv)

    if args.action == "list":
        try:
            names = list_credential_names()
            path = astrid_env_file_path()
        except ValueError as exc:
            print(f"astrid-credential: {exc}", file=sys.stderr)
            return 1
        if not names:
            print(f"no credentials stored in {path}")
            return 0
        for name in names:
            print(name)
        return 0

    try:
        _credential_variable(args.provider)
    except ValueError as exc:
        parser.error(str(exc))

    try:
        # getpass otherwise warns and falls back to echoed input when it cannot
        # disable terminal echo. Never accept a credential in that case.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass.getpass(f"Credential for {args.provider}: ")
    except getpass.GetPassWarning:
        print(
            "astrid-credential: hidden input requires an interactive terminal",
            file=sys.stderr,
        )
        return 1
    try:
        path = store_credential(args.provider, value)
    except (OSError, ValueError) as exc:
        print(f"astrid-credential: {exc}", file=sys.stderr)
        return 1
    print(f"Stored {args.provider} credential in {path} (value not displayed).")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through entry point
    raise SystemExit(main())
