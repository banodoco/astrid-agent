#!/usr/bin/env python3
"""Fail-closed receipt for the clean installed-product checkpoint smoke.

The runner deliberately starts outside the product checkout.  It renders an
App-shaped manifest, invokes the App bootstrap, and then uses only the console
script from the selected installation.  The receipt is intentionally small:
the command output lives in named files and the receipt records their paths,
hashes and *inner* exit statuses.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any, Mapping


FORMAT = "astrid.installed-smoke/v1"
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_IDENTITIES = ("runtime", "agent", "vibecomfy", "app", "pinokio")
REQUIRED_COMMANDS = (
    "installer",
    "version",
    "generate_list",
    "generate_describe",
    "readiness_dry_run",
    "skills_sync_check",
)
DEFAULTS = {
    "runtime": "47e5949465cb6f3afdfd1cb8a0e90009fd9d4020",
    "agent": "af5ae8afdb9829beb2bba4f811dbe97765bd72b2",
    "vibecomfy": "3bebae39655eb460454885040be4d3aefaf2ef31",
    "app": "a55bec9ff384b5bbfee5535962880084ab2adb1b",
    "pinokio": "89ed7aacae72bb48eb97ff9ff95c222bdd7e71e4",
}


class SmokeError(ValueError):
    """A stable fail-closed error suitable for the compact receipt."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeError(f"{label} is unavailable or invalid") from exc
    if not isinstance(raw, dict):
        raise SmokeError(f"{label} must be a JSON object")
    return raw


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n")


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        raise SmokeError(f"{label} must be a full lowercase Git SHA")
    return value


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise SmokeError("receipt artifact escapes its output directory") from exc


def _artifact(root: Path, path: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        raise SmokeError(f"required artifact is unavailable: {path.name}")
    return {"path": _relative(path, root), "sha256": _sha256_file(path)}


def _artifact_path(root: Path, record: object, label: str, errors: list[str]) -> Path | None:
    if not isinstance(record, Mapping):
        errors.append(f"{label} artifact record is missing")
        return None
    relative, digest = record.get("path"), record.get("sha256")
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        errors.append(f"{label} artifact path is unsafe or missing")
        return None
    if not isinstance(digest, str) or DIGEST.fullmatch(digest) is None:
        errors.append(f"{label} artifact digest is missing")
        return None
    path = root / relative
    if not path.is_file() or path.is_symlink():
        errors.append(f"{label} artifact is missing")
        return None
    if _sha256_file(path) != digest:
        errors.append(f"{label} artifact digest differs")
        return None
    return path


def _git_head(path: Path, label: str) -> str:
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise SmokeError(f"{label} checkout is unavailable")
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode:
        raise SmokeError(f"{label} checkout has no readable HEAD")
    return _identity(result.stdout.strip(), f"{label} checkout HEAD")


def _product_version(agent_checkout: Path) -> str:
    try:
        with (agent_checkout / "pyproject.toml").open("rb") as stream:
            value = tomllib.load(stream)["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise SmokeError("Agent project version is unavailable") from exc
    if not isinstance(value, str) or not value.strip():
        raise SmokeError("Agent project version is invalid")
    return value.strip()


def build_manifest(template: Mapping[str, Any], identities: Mapping[str, str], *, product_version: str) -> dict[str, Any]:
    """Copy the App manifest shape while replacing only published identities."""
    if template.get("schema_version") != 1 or not isinstance(template.get("tools"), Mapping):
        raise SmokeError("App install-manifest is not a supported v1 manifest")
    result = copy.deepcopy(dict(template))
    result["agent"] = {"sha": _identity(identities.get("agent"), "agent identity"), "version": product_version}
    runtime = result.get("runtime")
    if not isinstance(runtime, Mapping):
        raise SmokeError("App install-manifest has no runtime section")
    result["runtime"] = {**runtime, "sha": _identity(identities.get("runtime"), "runtime identity")}
    # Backend.source uses its reviewed VIBECOMFY_FORK_URL when no local repo is
    # supplied.  A local override would weaken this clean-runner proof.
    result["vibecomfy"] = {"sha": _identity(identities.get("vibecomfy"), "VibeComfy identity")}
    return result


def _clean_env(home: Path) -> dict[str, str]:
    # The installer has its own narrow child environment, but clear these here
    # too so the bootstrap and every recorded command prove the same boundary.
    environment = {
        key: value for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME"} and not key.startswith("ASTRID_")
    }
    environment.update({
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return environment


def _record_command(
    commands: dict[str, dict[str, Any]],
    root: Path,
    name: str,
    argv: list[str],
    *,
    environment: Mapping[str, str],
    cwd: Path,
    timeout: int,
    expected_stdout: str | None = None,
) -> subprocess.CompletedProcess[str]:
    log = root / "commands" / f"{name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=dict(environment),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        output, code = result.stdout, result.returncode
    except subprocess.TimeoutExpired as exc:
        output, code = (exc.stdout or "") + "\n[installed smoke command timed out]\n", 124
        result = subprocess.CompletedProcess(argv, code, output)
    except OSError as exc:
        output, code = f"[installed smoke command could not start: {exc}]\n", 127
        result = subprocess.CompletedProcess(argv, code, output)
    log.write_text(output, encoding="utf-8")
    record: dict[str, Any] = {
        "argv": argv,
        "exit_code": code,
        "output": _relative(log, root),
        "sha256": _sha256_file(log),
    }
    if expected_stdout is not None:
        record["expected_stdout"] = expected_stdout
    commands[name] = record
    return result


def _record_skipped(commands: dict[str, dict[str, Any]], root: Path, name: str, why: str) -> None:
    log = root / "commands" / f"{name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"not run: {why}\n", encoding="utf-8")
    commands[name] = {
        "argv": [],
        "exit_code": 125,
        "output": _relative(log, root),
        "sha256": _sha256_file(log),
    }


def _installed_console(root: Path) -> tuple[Path, Path]:
    current = root / "current"
    if not current.is_file() or current.is_symlink():
        raise SmokeError("installed current selection is missing")
    name = current.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise SmokeError("installed current selection is invalid")
    version_root = root / "versions" / name
    version = _json(version_root / "version.json", "installed version record")
    python = Path(str(version.get("python", "")))
    if not python.is_absolute() or not python.is_file() or not python.resolve().is_relative_to(version_root.resolve()):
        raise SmokeError("installed Python is missing or escapes its version")
    console = python.parent / ("astrid.exe" if os.name == "nt" else "astrid")
    if not console.is_file():
        raise SmokeError("installed astrid console entry point is missing")
    return console, version_root


def _pick_described_entry(list_log: Path) -> str:
    try:
        rows = json.loads(list_log.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeError("generate list output is not JSON") from exc
    if not isinstance(rows, list):
        raise SmokeError("generate list output is not a list")
    entries = sorted({str(row["id"]) for row in rows if isinstance(row, Mapping)
                      and row.get("source") in {"action-entry", "ready-template", "user-entry"}
                      and isinstance(row.get("id"), str) and row["id"]})
    if not entries:
        raise SmokeError("generate list returned no describable entry")
    return entries[0]


def _check_identity_mapping(value: object, expected: Mapping[str, str], errors: list[str], label: str) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{label} identities are missing")
        return
    if set(value) != set(REQUIRED_IDENTITIES):
        errors.append(f"{label} identities have missing or unexpected fields")
    for name in REQUIRED_IDENTITIES:
        observed = value.get(name)
        if not isinstance(observed, str) or SHA.fullmatch(observed) is None:
            errors.append(f"{label} {name} identity is missing")
        elif observed != expected.get(name):
            errors.append(f"{label} {name} identity is stale")


def evaluate_receipt(receipt: object, evidence_root: Path, expected: Mapping[str, str]) -> list[str]:
    """Return every reason the receipt is red; absence is never a pass."""
    errors: list[str] = []
    if not isinstance(receipt, Mapping):
        return ["receipt is not an object"]
    if receipt.get("format") != FORMAT:
        errors.append("receipt format is missing or wrong")
    _check_identity_mapping(receipt.get("identities"), expected, errors, "receipt")
    expected_tuple_digest = _sha256_bytes(_canonical(dict(expected)))
    if receipt.get("tuple_sha256") != expected_tuple_digest:
        errors.append("receipt tuple digest is missing or stale")
    environment = receipt.get("environment")
    if not isinstance(environment, Mapping) or environment.get("PYTHONPATH") != "unset":
        errors.append("receipt does not prove PYTHONPATH was unset")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, Mapping):
        errors.append("receipt artifacts are missing")
        requested = installed = None
    else:
        requested = _artifact_path(evidence_root, artifacts.get("requested_manifest"), "requested manifest", errors)
        installed = _artifact_path(evidence_root, artifacts.get("installed_manifest"), "installed manifest", errors)
    for path, label in ((requested, "requested manifest"), (installed, "installed manifest")):
        if path is None:
            continue
        try:
            manifest = _json(path, label)
        except SmokeError as exc:
            errors.append(str(exc))
            continue
        for name in ("agent", "runtime", "vibecomfy"):
            entry = manifest.get(name)
            observed = entry.get("sha") if isinstance(entry, Mapping) else None
            if observed != expected.get(name):
                errors.append(f"{label} has wrong {name} artifact identity")
        if label == "requested manifest":
            version = manifest.get("agent", {}).get("version") if isinstance(manifest.get("agent"), Mapping) else None
            if not isinstance(version, str) or not version:
                errors.append("requested manifest has no product version")
    commands = receipt.get("commands")
    if not isinstance(commands, Mapping):
        errors.append("receipt commands are missing")
    else:
        if set(commands) != set(REQUIRED_COMMANDS):
            errors.append("receipt commands have missing or unexpected fields")
        for name in REQUIRED_COMMANDS:
            command = commands.get(name)
            if not isinstance(command, Mapping):
                errors.append(f"{name} command record is missing")
                continue
            argv, exit_code = command.get("argv"), command.get("exit_code")
            if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item for item in argv):
                errors.append(f"{name} command argv is missing")
            if type(exit_code) is not int:
                errors.append(f"{name} command exit code is missing")
            elif exit_code != 0:
                errors.append(f"{name} inner command failed with exit {exit_code}")
            log = _artifact_path(evidence_root, command, f"{name} command output", errors)
            if name == "version":
                expected_stdout = command.get("expected_stdout")
                if not isinstance(expected_stdout, str) or not expected_stdout:
                    errors.append("version command expected output is missing")
                elif log is not None:
                    actual = log.read_text(encoding="utf-8")
                    if actual != expected_stdout:
                        errors.append("version command output differs from the manifest")
    computed = "PASS" if not errors else "FAIL"
    if receipt.get("VERDICT") != computed:
        errors.append("receipt VERDICT does not match evaluated evidence")
    return errors


def _run(args: argparse.Namespace) -> int:
    if args.installer_timeout <= 0 or args.deadline_seconds <= 0:
        raise SmokeError("installed-smoke timeouts must be positive")
    output = args.output.resolve()
    if output.exists():
        if not output.is_dir() or output.is_symlink() or any(output.iterdir()):
            raise SmokeError("installed-smoke output directory must be a new empty directory")
    output.mkdir(parents=True)
    # Remove it from this process too, not merely from children.
    os.environ.pop("PYTHONPATH", None)
    os.environ.pop("PYTHONHOME", None)
    identities = {name: _identity(getattr(args, name), f"{name} identity") for name in REQUIRED_IDENTITIES}
    checkouts = {"agent": args.agent_checkout.resolve(), "runtime": args.runtime_checkout.resolve(),
                 "app": args.app_checkout.resolve(), "pinokio": args.pinokio_checkout.resolve()}
    for name, checkout in checkouts.items():
        observed = _git_head(checkout, name)
        if observed != identities[name]:
            raise SmokeError(f"{name} checkout does not match the requested identity")
    template = _json(args.app_checkout / "public" / "install-manifest.json", "App install-manifest")
    manifest = build_manifest(template, identities, product_version=_product_version(args.agent_checkout))
    manifest_path = output / "install-manifest.json"
    _write_json(manifest_path, manifest)
    receipt: dict[str, Any] = {
        "format": FORMAT,
        "identities": identities,
        "tuple_sha256": _sha256_bytes(_canonical(identities)),
        "environment": {"PYTHONPATH": "unset"},
        "artifacts": {"requested_manifest": _artifact(output, manifest_path)},
        "commands": {},
        "VERDICT": "FAIL",
    }
    commands: dict[str, dict[str, Any]] = receipt["commands"]
    home, install_root, data_root, work = output / "home", output / "program", output / "data", output / "work"
    for directory in (home, data_root, work):
        directory.mkdir(parents=True, exist_ok=True)
    environment = _clean_env(home)
    deadline = time.monotonic() + args.deadline_seconds

    def remaining(limit: int) -> int:
        value = int(deadline - time.monotonic())
        return min(limit, value) if value > 0 else 0

    installer = args.app_checkout / "public" / "install"
    if not installer.is_file():
        raise SmokeError("App install bootstrap is missing")
    installer_timeout = remaining(args.installer_timeout)
    if installer_timeout:
        installed = _record_command(
            commands, output, "installer",
            [str(installer), "--manifest", str(manifest_path), "--root", str(install_root),
             "--data-root", str(data_root), "--managed-by", "pinokio"],
            environment=environment, cwd=work, timeout=installer_timeout,
        )
    else:
        _record_skipped(commands, output, "installer", "the 27-minute smoke deadline elapsed")
        installed = subprocess.CompletedProcess([], 124)
    console: Path | None = None
    if installed.returncode == 0:
        try:
            console, version_root = _installed_console(install_root)
            receipt["artifacts"]["installed_manifest"] = _artifact(output, version_root / "manifest.json")
        except SmokeError as exc:
            (output / "commands" / "installed-selection.log").write_text(str(exc) + "\n", encoding="utf-8")
    if console is None:
        for name in REQUIRED_COMMANDS[1:]:
            _record_skipped(commands, output, name, "the real installer did not produce an installed console")
    else:
        expected_version = f"astrid/{manifest['agent']['version']}\n"
        version_timeout = remaining(60)
        if version_timeout:
            _record_command(commands, output, "version", [str(console), "--version"], environment=environment,
                            cwd=work, timeout=version_timeout, expected_stdout=expected_version)
        else:
            _record_skipped(commands, output, "version", "the 27-minute smoke deadline elapsed")
        list_timeout = remaining(120)
        if list_timeout:
            listing = _record_command(commands, output, "generate_list", [str(console), "generate", "list", "--json"],
                                      environment=environment, cwd=work, timeout=list_timeout)
        else:
            _record_skipped(commands, output, "generate_list", "the 27-minute smoke deadline elapsed")
            listing = subprocess.CompletedProcess([], 124)
        if listing.returncode == 0:
            try:
                entry = _pick_described_entry(output / "commands" / "generate_list.log")
            except SmokeError as exc:
                _record_skipped(commands, output, "generate_describe", str(exc))
            else:
                describe_timeout = remaining(120)
                if describe_timeout:
                    _record_command(commands, output, "generate_describe", [str(console), "generate", "describe", entry, "--json"],
                                    environment=environment, cwd=work, timeout=describe_timeout)
                else:
                    _record_skipped(commands, output, "generate_describe", "the 27-minute smoke deadline elapsed")
        else:
            _record_skipped(commands, output, "generate_describe", "generate list failed")
        readiness_timeout = remaining(120)
        if readiness_timeout:
            _record_command(
                commands, output, "readiness_dry_run",
                [str(console), "engines", "install", "vibecomfy-h3", "--platform", "linux-x86_64-cu130",
                 "--placement", "local", "--root", str(data_root / "engine"), "--dry-run"],
                environment=environment, cwd=work, timeout=readiness_timeout,
            )
        else:
            _record_skipped(commands, output, "readiness_dry_run", "the 27-minute smoke deadline elapsed")
        skills_timeout = remaining(120)
        if skills_timeout:
            _record_command(commands, output, "skills_sync_check", [str(console), "skills", "sync", "--check"],
                            environment=environment, cwd=work, timeout=skills_timeout)
        else:
            _record_skipped(commands, output, "skills_sync_check", "the 27-minute smoke deadline elapsed")
    errors = evaluate_receipt(receipt, output, identities)
    receipt["VERDICT"] = "PASS" if not errors else "FAIL"
    # Check the final declared verdict too; this detects future accidental
    # changes to the receipt writer independently from the runner exit status.
    errors = evaluate_receipt(receipt, output, identities)
    receipt["errors"] = errors
    receipt_path = output / "receipt.json"
    _write_json(receipt_path, receipt)
    print(json.dumps({"receipt": str(receipt_path), "VERDICT": receipt["VERDICT"], "errors": errors}, sort_keys=True))
    return 0 if receipt["VERDICT"] == "PASS" and not errors else 1


def _check(args: argparse.Namespace) -> int:
    expected = {name: _identity(getattr(args, name), f"expected {name}") for name in REQUIRED_IDENTITIES}
    receipt = _json(args.receipt, "receipt")
    errors = evaluate_receipt(receipt, args.output.resolve(), expected)
    result = {"VERDICT": "PASS" if not errors else "FAIL", "errors": errors}
    print(json.dumps(result, sort_keys=True))
    return 0 if not errors else 1


def _identity_arguments(parser: argparse.ArgumentParser) -> None:
    for name in REQUIRED_IDENTITIES:
        parser.add_argument(f"--{name}", default=DEFAULTS[name])


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="perform the real clean installation and write its receipt")
    _identity_arguments(run)
    run.add_argument("--agent-checkout", type=Path, required=True)
    run.add_argument("--runtime-checkout", type=Path, required=True)
    run.add_argument("--app-checkout", type=Path, required=True)
    run.add_argument("--pinokio-checkout", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--installer-timeout", type=int, default=1200)
    run.add_argument("--deadline-seconds", type=int, default=1620)
    run.set_defaults(handler=_run)
    check = commands.add_parser("check", help="evaluate an existing receipt and its compact artifacts")
    _identity_arguments(check)
    check.add_argument("--receipt", type=Path, required=True)
    check.add_argument("--output", type=Path, required=True)
    check.set_defaults(handler=_check)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except SmokeError as exc:
        print(json.dumps({"VERDICT": "FAIL", "errors": [str(exc)]}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
