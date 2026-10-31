#!/usr/bin/env python3
"""Run authentic update operations admitted by the current M4-5 product surface.

The public install-line is currently the only complete operation exposed by the
frozen product interfaces.  Browser same-operation recovery and both Pinokio
routes fail before side effects: Runtime cannot reconcile a recovery-needed
operation ID, and Pinokio exposes action bytes but no stable owner command that
executes those bytes as one action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

PROTOCOL = "astrid-installed-browser-interface/v1"
JOURNAL_FORMAT = "astrid-update-operation-surface/v1"
OWNER = "haus.astrid.runtime"
PINOKIO_ACTION_SHA256 = "87d81aa58e1bc12f5f0c932d668b3e387c17e1754a066995c54eaf2972c1bd8b"
SHA = re.compile(r"[0-9a-f]{40}")
SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
AMBIENT_ENV_ALLOWLIST = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOCALAPPDATA",
        "LOGNAME",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "SHELL",
        "SystemRoot",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "WAYLAND_DISPLAY",
        "WINDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    }
)
SENSITIVE_ENV_PARTS = (
    "ACCESS_KEY",
    "API_KEY",
    "CREDENTIAL",
    "PASSWORD",
    "PASSWD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
)


class SurfaceError(RuntimeError):
    """The requested update route is absent or failed authentic execution."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SurfaceError(f"{label} must be an object")
    return dict(value)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _child_env(installed: Mapping[str, Any] | None = None) -> dict[str, str]:
    env = {
        key: os.environ[key]
        for key in AMBIENT_ENV_ALLOWLIST
        if key in os.environ and not any(part in key.upper() for part in SENSITIVE_ENV_PARTS)
    }
    env.setdefault("PATH", os.defpath)
    for key, value in (installed or {}).items():
        name = str(key)
        if not any(part in name.upper() for part in SENSITIVE_ENV_PARTS):
            env[name] = str(value)
    return env


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SurfaceError(f"{label} must be a non-symlink regular file: {path}")
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), label)
    except (OSError, json.JSONDecodeError) as exc:
        raise SurfaceError(f"{label} is unavailable or malformed: {path}") from exc


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise SurfaceError(f"refusing symlinked update journal: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _deadline(deadline: float, label: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SurfaceError(f"operation deadline expired {label}")
    return remaining


def _command(
    argv: Sequence[str],
    label: str,
    *,
    timeout: float = 30,
    deadline: float | None = None,
) -> str:
    if deadline is not None:
        timeout = min(timeout, _deadline(deadline, f"while running {label}"))
    try:
        result = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SurfaceError(f"{label} is unavailable: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[0]}" if detail else ""
        raise SurfaceError(f"{label} failed with exit {result.returncode}{suffix}")
    return result.stdout


def _git_head(path: Path, label: str, deadline: float) -> str:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise SurfaceError(f"{label} checkout must be an absolute non-symlink directory")
    head = _command(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        f"{label} checkout",
        deadline=deadline,
    ).strip()
    if SHA.fullmatch(head) is None:
        raise SurfaceError(f"{label} checkout did not resolve to a full commit")
    if _command(
        ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"],
        f"{label} checkout status",
        deadline=deadline,
    ).strip():
        raise SurfaceError(f"{label} checkout has tracked modifications")
    return head


def _checkouts(
    args: argparse.Namespace, request: Mapping[str, Any], deadline: float
) -> dict[str, str]:
    inputs = _mapping(request.get("inputs"), "inputs")
    candidate = _mapping(inputs.get("candidate"), "candidate")
    paths = {
        "app": args.app_checkout,
        "agent": args.agent_checkout,
        "runtime": args.runtime_checkout,
        "pinokio": args.pinokio_checkout,
    }
    if set(candidate) != set(paths):
        raise SurfaceError("candidate must contain app, agent, runtime, and pinokio")
    observed = {}
    for name, path in paths.items():
        observed[name] = _git_head(path, name, deadline)
        if observed[name] != candidate[name]:
            raise SurfaceError(f"{name} checkout differs from the requested candidate")
    return observed


def _actual_os(expected: str) -> str:
    actual = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    if actual != expected:
        raise SurfaceError(f"scenario OS {expected!r} does not match this host ({actual!r})")
    return actual


def _roots(request: Mapping[str, Any]) -> tuple[Path, Path]:
    inputs = _mapping(request.get("inputs"), "inputs")
    root = Path(str(inputs.get("root", "")))
    data = Path(str(inputs.get("data_root", "")))
    if not root.is_absolute() or not data.is_absolute() or root == data:
        raise SurfaceError("install and data roots must be distinct absolute paths")
    if root.is_symlink() or data.is_symlink():
        raise SurfaceError("install and data roots must not be symlinks")
    if not data.is_dir():
        raise SurfaceError("data root is unavailable")
    return root.resolve(strict=False), data.resolve(strict=True)


def _inside_checkout(path: Path, checkout: Path, label: str, deadline: float) -> Path:
    if path.is_symlink() or not path.is_file():
        raise SurfaceError(f"{label} must be a non-symlink regular file")
    resolved = path.resolve(strict=True)
    root = checkout.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise SurfaceError(f"{label} is outside its bound checkout")
    relative = resolved.relative_to(root).as_posix()
    tracked = _command(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", relative],
        f"{label} provenance",
        deadline=deadline,
    ).strip()
    if tracked != relative:
        raise SurfaceError(f"{label} is not the committed checkout artifact")
    return resolved


def _installed(root: Path, data: Path) -> dict[str, Any]:
    marker = _read_json(root / "install.json", "install marker")
    if marker.get("owner") != OWNER or Path(str(marker.get("data_root", ""))).resolve() != data:
        raise SurfaceError("install marker does not bind the requested data root")
    current = root / "current"
    if current.is_symlink() or not current.is_file():
        raise SurfaceError("installed current selection is unavailable")
    name = current.read_text(encoding="utf-8").strip()
    if SAFE_VERSION.fullmatch(name) is None:
        raise SurfaceError("installed current selection is invalid")
    version_root = (root / "versions" / name).resolve(strict=True)
    if not version_root.is_relative_to((root / "versions").resolve(strict=True)):
        raise SurfaceError("installed version escapes the program root")
    version = _read_json(version_root / "version.json", "installed version record")
    if Path(str(version.get("root", ""))).resolve() != version_root:
        raise SurfaceError("installed version record points outside the selected version")
    manifest = _read_json(version_root / "manifest.json", "installed manifest")
    python = Path(os.path.abspath(str(version.get("python", ""))))
    if (
        not python.is_absolute()
        or not python.is_relative_to(version_root)
        or not python.is_file()
        or not os.access(python, os.X_OK)
    ):
        raise SurfaceError("installed Python executable is unavailable")
    return {
        "marker": marker,
        "version": version,
        "manifest": manifest,
        "version_root": version_root,
        "python": python,
    }


def _validate_release1(installed: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    manifest = _mapping(installed.get("manifest"), "installed manifest")
    candidate = _mapping(_mapping(request.get("inputs"), "inputs").get("candidate"), "candidate")
    if manifest.get("release", 0) != 1:
        raise SurfaceError("install-line update requires an installed Release-1 baseline")
    for name in ("agent", "runtime"):
        if _mapping(manifest.get(name), f"installed {name}").get("sha") != candidate[name]:
            raise SurfaceError(f"installed Release-1 {name} differs from the candidate")


def _stand_in(args: argparse.Namespace, request: Mapping[str, Any]) -> dict[str, Any]:
    inputs = _mapping(request.get("inputs"), "inputs")
    stand_in = _mapping(inputs.get("stand_in"), "stand-in")
    if args.stand_in_manifest.is_symlink() or not args.stand_in_manifest.is_file():
        raise SurfaceError("stand-in manifest must be a non-symlink regular file")
    if _sha256(args.stand_in_manifest) != stand_in.get("manifest_sha256"):
        raise SurfaceError("stand-in manifest bytes differ from the request")
    manifest = _read_json(args.stand_in_manifest, "stand-in manifest")
    target = _mapping(stand_in.get("target"), "stand-in target")
    runtime = _mapping(manifest.get("runtime"), "stand-in Runtime")
    if (
        manifest.get("release") != 2
        or manifest.get("qualification_marker") != inputs.get("stand_in_marker")
        or _mapping(manifest.get("agent"), "stand-in Agent").get("sha") != target.get("agent")
        or runtime.get("sha") != target.get("runtime")
        or runtime.get("schema_digest") != target.get("schema_digest")
    ):
        raise SurfaceError("stand-in manifest identity differs from the request")
    return manifest


def _manifest_preservation_preflight(args: argparse.Namespace, manifest: Mapping[str, Any]) -> str:
    """Prove the current installer can retain the accepted manifest bytes exactly."""

    raw = args.stand_in_manifest.read_bytes()
    raw_sha = hashlib.sha256(raw).hexdigest()
    canonical = (json.dumps(dict(manifest), indent=2, sort_keys=True) + "\n").encode("utf-8")
    reasons = []
    if "vibecomfy" not in manifest:
        reasons.append("it omits vibecomfy, which the current Agent installer adds")
    if raw != canonical:
        reasons.append("the current Agent installer rewrites it as sorted canonical JSON")
    if reasons:
        raise SurfaceError(
            "NEEDS: Agent installer must preserve the accepted stand-in manifest bytes, including an "
            "explicit vibecomfy pin, or the boundary must bind the effective installed-manifest digest; "
            f"accepted SHA-256 {raw_sha} cannot remain installed because " + " and ".join(reasons)
        )
    return raw_sha


def _validate_release2(
    root: Path, data: Path, request: Mapping[str, Any], expected_manifest_sha: str
) -> dict[str, Any]:
    installed = _installed(root, data)
    manifest = _mapping(installed.get("manifest"), "installed manifest")
    inputs = _mapping(request.get("inputs"), "inputs")
    target = _mapping(_mapping(inputs.get("stand_in"), "stand-in").get("target"), "target")
    runtime = _mapping(manifest.get("runtime"), "installed Runtime")
    if (
        manifest.get("release") != 2
        or _sha256(Path(installed["version_root"]) / "manifest.json") != expected_manifest_sha
        or manifest.get("qualification_marker") != inputs.get("stand_in_marker")
        or _mapping(manifest.get("agent"), "installed Agent").get("sha") != target.get("agent")
        or runtime.get("sha") != target.get("runtime")
        or runtime.get("schema_digest") != target.get("schema_digest")
    ):
        raise SurfaceError("public bootstrap did not select the requested Release-2 stand-in")
    return installed


def _journal_path(data: Path, args: argparse.Namespace) -> Path:
    if Path(args.journal_name).name != args.journal_name or args.journal_name in {".", ".."}:
        raise SurfaceError("journal name must be one safe file name")
    return data / args.journal_name


def _new_journal(request: Mapping[str, Any], checkouts: Mapping[str, str]) -> dict[str, Any]:
    scenario = _mapping(request.get("scenario"), "scenario")
    return {
        "format": JOURNAL_FORMAT,
        "scenario": scenario,
        "candidate": dict(checkouts),
        "operation": None,
        "cleanup": "pending",
    }


def _load_journal(path: Path) -> dict[str, Any]:
    value = _read_json(path, "update surface journal")
    if value.get("format") != JOURNAL_FORMAT:
        raise SurfaceError("update surface journal format differs")
    return value


def _fail_stop_for_adapter_custody() -> None:
    """Remain killable until the adapter's absolute deadline owns cleanup."""
    while True:
        time.sleep(3600)


def _terminate_product_tree_or_fail_stop(process: subprocess.Popen[bytes]) -> None:
    """End the inherited product tree without detaching it from the adapter."""
    if os.name != "nt":
        # installed_browser_adapter starts every admitted surface as its own
        # process-group leader.  Killing that group is the only race-free way
        # to include grandchildren.  Never target an unrelated group when a
        # developer invokes this command directly.
        if os.getpid() == os.getpgrp():
            try:
                os.killpg(os.getpgrp(), signal.SIGKILL)
            except OSError:
                pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait()
        except (OSError, subprocess.SubprocessError):
            pass
        _fail_stop_for_adapter_custody()

    child_env = _child_env()
    system_root = child_env.get("SystemRoot") or child_env.get("WINDIR")
    taskkill = Path(system_root, "System32", "taskkill.exe") if system_root else None
    if taskkill is not None and taskkill.is_file():
        try:
            result = subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                check=False,
                env=child_env,
            )
            if result.returncode == 0:
                process.wait(timeout=5)
                return
        except (OSError, subprocess.SubprocessError):
            pass
    # Returning would make the adapter accept a normal nonzero completion and
    # skip its /T fallback. Stay alive so its deadline remains authoritative.
    _fail_stop_for_adapter_custody()


def _run_logged(
    argv: Sequence[str],
    *,
    log: Path,
    deadline: float,
    env: Mapping[str, str] | None = None,
    label: str,
) -> str:
    _deadline(deadline, f"before starting {label}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(log, flags, 0o600)
    except OSError as exc:
        raise SurfaceError(f"refusing unavailable or pre-existing {label} log") from exc
    fchmod = getattr(os, "fchmod", None)
    if fchmod is not None:
        try:
            fchmod(descriptor, 0o600)
        except OSError as exc:
            os.close(descriptor)
            raise SurfaceError(f"could not make the exclusive {label} log private") from exc
    # Windows has no os.fchmod.  Its os.open mode still requests an owner-only
    # file without introducing a pathname reopen or ACL-inheriting copy step.
    with os.fdopen(descriptor, "wb") as handle:
        try:
            process = subprocess.Popen(
                list(argv),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=None if env is None else dict(env),
            )
        except OSError as exc:
            raise SurfaceError(f"{label} was unavailable: {exc}") from exc
        try:
            # The accepted adapter creates the one owned process group and
            # enforces its absolute deadline. Keep the product child in that
            # group so adapter killpg/taskkill /T retains the whole tree.
            stdout, stderr = process.communicate()
        except BaseException as exc:
            # Never turn a local pipe failure into a quick normal exit: the
            # adapter only tree-kills on its deadline, while product
            # grandchildren may otherwise outlive their direct parent.
            _terminate_product_tree_or_fail_stop(process)
            raise AssertionError("product-tree termination unexpectedly returned") from exc
        if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
            raise SurfaceError(f"{label} returned non-binary process output")
        content = stdout + (b"\n" if stdout and stderr else b"") + stderr
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    if process.returncode:
        raise SurfaceError(f"{label} exited {process.returncode}; see {log}")
    return hashlib.sha256(content).hexdigest()


def _install_line(
    args: argparse.Namespace,
    envelope: Mapping[str, Any],
    checkouts: Mapping[str, str],
    deadline: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    request = _mapping(envelope.get("request"), "request")
    scenario = _mapping(request.get("scenario"), "scenario")
    payload = _mapping(request.get("payload"), "payload")
    if scenario.get("channel") != "install-line" or scenario.get("mode") != "direct":
        raise SurfaceError("install-line command received a non-direct route")
    if payload.get("update_flag") is not True or not all(
        payload.get(name) is True for name in ("root_bound", "data_root_bound")
    ):
        raise SurfaceError("install-line binding flags are incomplete")
    root, data = _roots(request)
    _validate_release1(_installed(root, data), request)
    manifest = _stand_in(args, request)
    expected_manifest_sha = _manifest_preservation_preflight(args, manifest)
    bootstrap = _inside_checkout(args.bootstrap, args.app_checkout, "public bootstrap", deadline)
    if args.shell:
        selected = shutil.which(args.shell) if os.sep not in args.shell else args.shell
        if not selected or not Path(selected).is_file():
            raise SurfaceError("public bootstrap shell is unavailable")
        shell = str(Path(selected).resolve())
    else:
        shell = "powershell.exe" if sys.platform == "win32" else "/bin/sh"
    if sys.platform == "win32":
        argv = [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(bootstrap),
        ]
    else:
        argv = [shell, str(bootstrap)]
    argv.extend(
        [
            "--manifest",
            str(args.stand_in_manifest.resolve()),
            "--root",
            str(root),
            "--data-root",
            str(data),
            "--update",
        ]
    )
    journal_path = _journal_path(data, args)
    if journal_path.exists():
        raise SurfaceError("update surface journal already exists; refusing a second operation")
    journal = _new_journal(request, checkouts)
    journal["operation"] = {
        "handoff_id": envelope["handoff_id"],
        "action": "install-line-update",
        "state": "inflight",
        "argv": argv,
        "bootstrap_sha256": _sha256(bootstrap),
        "manifest_sha256": _sha256(args.stand_in_manifest),
    }
    _write_json(journal_path, journal)
    log = data / ".astrid-m4-5-update-operation.log"
    env = _child_env()
    env["BROWSER"] = "true"
    try:
        log_sha = _run_logged(
            argv,
            log=log,
            deadline=deadline,
            env=env,
            label="public install-line update",
        )
        installed = _validate_release2(root, data, request, expected_manifest_sha)
    except BaseException as exc:
        journal["operation"]["state"] = "failed"
        journal["operation"]["error"] = f"{type(exc).__name__}: {exc}"
        _write_json(journal_path, journal)
        raise
    journal["operation"].update(
        {
            "state": "complete",
            "log": str(log),
            "log_sha256": log_sha,
            "installed_python": str(installed["python"]),
            "installed_python_sha256": _sha256(Path(installed["python"])),
        }
    )
    _write_json(journal_path, journal)
    observation = {
        "channel": "install-line",
        "entrypoint": "public-bootstrap",
        **payload,
    }
    evidence = {
        "operation_journal": str(journal_path),
        "operation_id": envelope["handoff_id"],
        "bootstrap": {"path": str(bootstrap), "sha256": _sha256(bootstrap)},
        "log_sha256": log_sha,
    }
    return observation, evidence


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, PermissionError):
        return False
    return True


def _remove_owned_program_residue(root: Path, data: Path, prior_marker: Mapping[str, Any]) -> None:
    if not os.path.lexists(root):
        return
    runner = Path(__file__).resolve()
    home = Path.home().resolve()
    if (
        root.parent == root
        or root == home
        or root.is_symlink()
        or not root.is_dir()
        or runner == root
        or runner.is_relative_to(root)
        or data.is_relative_to(root)
        or root.is_relative_to(data)
    ):
        raise SurfaceError("refusing unsafe owned program-root residue removal")
    retained = _read_json(root / "install.json", "retained install marker")
    if (
        retained.get("owner") != OWNER
        or prior_marker.get("owner") != OWNER
        or Path(str(retained.get("data_root", ""))).resolve() != data
        or Path(str(prior_marker.get("data_root", ""))).resolve() != data
        or _canonical(retained) != _canonical(prior_marker)
    ):
        raise SurfaceError(
            "refusing to remove program residue without the exact retained owner/data-root marker"
        )
    shutil.rmtree(root)


def _cleanup(
    args: argparse.Namespace,
    envelope: Mapping[str, Any],
    checkouts: Mapping[str, str],
    deadline: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    request = _mapping(envelope.get("request"), "request")
    scenario = _mapping(request.get("scenario"), "scenario")
    if scenario.get("channel") != "install-line" or scenario.get("mode") != "direct":
        raise SurfaceError("no owned update cleanup exists for the blocked product route")
    root, data = _roots(request)
    journal_path = _journal_path(data, args)
    journal = _load_journal(journal_path)
    if journal.get("scenario") != scenario or journal.get("candidate") != dict(checkouts):
        raise SurfaceError("update cleanup journal belongs to another scenario or candidate")
    operation = _mapping(journal.get("operation"), "update operation")
    if operation.get("action") != "install-line-update" or operation.get("state") != "complete":
        raise SurfaceError("update cleanup has no completed owned install-line operation")
    installed = _installed(root, data)
    prior_marker = _mapping(installed["marker"], "installed marker")
    python = Path(installed["python"])
    if _sha256(python) != operation.get("installed_python_sha256"):
        raise SurfaceError("installed cleanup executable changed after the update operation")
    raw_env = _mapping(_mapping(installed["version"], "version").get("env"), "installed env")
    env = _child_env(raw_env)
    argv = [
        str(python),
        "-I",
        "-m",
        "astrid",
        "install",
        "--uninstall",
        "--root",
        str(root),
        "--data-root",
        str(data),
    ]
    log = data / ".astrid-m4-5-update-cleanup.log"
    try:
        log_sha = _run_logged(
            argv,
            log=log,
            deadline=deadline,
            env=env,
            label="installed product cleanup",
        )
        _remove_owned_program_residue(root, data, prior_marker)
        if os.path.lexists(root):
            raise SurfaceError("installed product cleanup retained the owned program root")
        discovery = data / "runtime" / "discovery.json"
        if discovery.exists() and _pid_alive(_read_json(discovery, "Runtime discovery").get("pid")):
            raise SurfaceError("installed product cleanup retained the Runtime owner")
    except BaseException as exc:
        journal["cleanup"] = "failed"
        journal["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        _write_json(journal_path, journal)
        raise
    journal.update(
        {
            "cleanup": "complete",
            "cleanup_handoff_id": envelope["handoff_id"],
            "cleanup_log": str(log),
            "cleanup_log_sha256": log_sha,
        }
    )
    _write_json(journal_path, journal)
    return {"cleanup": "pass"}, {
        "operation_journal": str(journal_path),
        "cleanup_executable": {"path": str(python), "sha256": operation["installed_python_sha256"]},
        "cleanup_log_sha256": log_sha,
    }


def _pinokio_artifact(args: argparse.Namespace, deadline: float) -> None:
    if args.pinokio_action is None:
        raise SurfaceError(
            "NEEDS: Pinokio owner must register the exact pinokio/update.js artifact and a stable owner command"
        )
    action = _inside_checkout(
        args.pinokio_action, args.pinokio_checkout, "Pinokio update action", deadline
    )
    if _sha256(action) != PINOKIO_ACTION_SHA256:
        raise SurfaceError("Pinokio update action bytes differ from the accepted owner digest")


def _validate_envelope(value: object) -> dict[str, Any]:
    envelope = _mapping(value, "interface envelope")
    if envelope.get("protocol") != PROTOCOL or envelope.get("surface") != "update":
        raise SurfaceError("update interface protocol or surface differs")
    request = _mapping(envelope.get("request"), "request")
    action = envelope.get("action")
    if request.get("action") != action:
        raise SurfaceError("request and interface action differ")
    if action not in {
        "button-update-lost-ack",
        "button-update-explicit",
        "install-line-update",
        "pinokio-update",
        "cleanup",
    }:
        raise SurfaceError(f"update surface does not own action {action!r}")
    scenario = _mapping(request.get("scenario"), "scenario")
    _actual_os(str(scenario.get("os")))
    handoff = envelope.get("handoff_id")
    if not isinstance(handoff, str) or len(handoff) != 64:
        raise SurfaceError("interface handoff identity is invalid")
    return envelope


def execute(
    args: argparse.Namespace,
    envelope: Mapping[str, Any],
    *,
    deadline: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if deadline is None:
        deadline = time.monotonic() + args.timeout
    request = _mapping(envelope.get("request"), "request")
    checkouts = _checkouts(args, request, deadline)
    action = envelope.get("action")
    if action in {"button-update-lost-ack", "button-update-explicit"}:
        raise SurfaceError(
            "NEEDS: Runtime must reconcile the same recovery-needed operation under lock; "
            "the current zero-body POST mints a new operation and is not invoked"
        )
    if action == "pinokio-update":
        _pinokio_artifact(args, deadline)
        raise SurfaceError(
            "NEEDS: Pinokio must expose one stable owner command for pinokio/update.js; "
            "child shell fragments are not an admitted replacement"
        )
    if action == "install-line-update":
        return _install_line(args, envelope, checkouts, deadline)
    return _cleanup(args, envelope, checkouts, deadline)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-checkout", type=Path, required=True)
    parser.add_argument("--agent-checkout", type=Path, required=True)
    parser.add_argument("--runtime-checkout", type=Path, required=True)
    parser.add_argument("--pinokio-checkout", type=Path, required=True)
    parser.add_argument("--stand-in-manifest", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--shell")
    parser.add_argument("--pinokio-action", type=Path)
    parser.add_argument("--journal-name", default=".astrid-m4-5-update-surface.json")
    parser.add_argument("--timeout", type=float, default=90.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not math.isfinite(args.timeout) or args.timeout <= 0 or args.timeout > 90:
            raise SurfaceError("timeout must be positive and at most 90 seconds")
        deadline = time.monotonic() + args.timeout
        envelope = _validate_envelope(json.load(sys.stdin))
        observation, extra_evidence = execute(args, envelope, deadline=deadline)
        response = {
            "protocol": PROTOCOL,
            "handoff_id": envelope["handoff_id"],
            "surface": "update",
            "action": envelope["action"],
            "status": "pass",
            "evidence": {
                "kind": "update-process",
                "pid": os.getpid(),
                "synthetic": False,
                "source_only": False,
                **extra_evidence,
            },
            "observation": observation,
        }
    except (SurfaceError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"update_operation_surface: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(response, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
