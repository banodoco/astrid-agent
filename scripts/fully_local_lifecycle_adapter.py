#!/usr/bin/env python3
"""Authenticate and invoke the pinned Pinokio fully-local Update route.

The adapter never executes ``update.js`` through Node and never substitutes a
backend or foreground service. A separately pinned owner wrapper invokes the
authenticated PTerm CLI as ``pterm start pinokio/update.js --ref <local app
ref>`` and collects terminal task evidence from the pinned pinokiod engine.
Raw PTerm exit status is never treated as settlement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

PINOKIO_REF = "c5b42701641889ecb916c60cde09a717f83d62ff"
PINOKIO_UPDATE_SHA256 = "87d81aa58e1bc12f5f0c932d668b3e387c17e1754a066995c54eaf2972c1bd8b"
RUNTIME_REF = "b70f9c258fbc324b311acaa279c0c4343024ddc1"
APP_REF = "7302b2992976ce7ed016738f7fe1086937f99384"
PTERM_REF = "5be43718701c448b43daf0ce51d8935f170065bb"
PINOKIOD_REF = "add4a674ad1ba95bb62ec427fce519fe5bf0e9bf"
PINOKIOD_VERSION = "8.1.1"
ROUTE_ID = "pinokio.update.local-editor"
DISPOSABLE_MARKER = ".m4-4b-disposable"
MARKER_BYTES = b"m4-4b-disposable\n"
EXTERNAL_EDITOR_MARKER_SUFFIX = ".m4-4b-editor-custody.json"
EXTERNAL_PINOKIO_MARKER_SUFFIX = ".m4-4b-pinokio-custody.json"
ENGINE_PROTOCOL = "astrid.pterm.settled-task.v1"
ENGINE_TIMEOUT_SECONDS = 1800
HOSTED_NEED = "hosted_linux_receipt_required"
EVENT_ORDER = (
    "runtime_preflight_stop",
    "installer_lock_release",
    "pinokio_launcher_fast_forward",
    "local_port_preflight",
    "editor_checkout_validation",
    "editor_fast_forward_pull",
    "installer_update_manifest",
    "ref_build_boundary",
    "temporary_runtime_healthy",
    "editor_build",
    "temporary_runtime_down",
    "restore_editor_checkout",
    "restore_runtime_state",
    "post_restore_start",
    "post_restore_status",
    "post_restore_workspace",
    "post_restore_http",
    "post_restore_ready",
    "controlled_stop",
)
BUILD_TAIL = ("scripts/dev-local-workspace.mjs", "--build")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REF_RE = re.compile(r"^pinokio://127\.0\.0\.1:42000/api/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class AdapterError(RuntimeError):
    """The local route cannot safely be executed or evidenced."""


def _reject_symlink_components(path: Path, label: str) -> Path:
    absolute = path.expanduser().absolute()
    for component in reversed((absolute, *absolute.parents)):
        if component.exists() and component.is_symlink():
            raise AdapterError(f"{label} path contains a symlink: {component}")
    return absolute


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(checkout: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *args], text=True, capture_output=True, check=False, timeout=30
    )
    if result.returncode:
        raise AdapterError(
            f"git {args[0]} could not authenticate {checkout}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _checkout(
    path: Path, name: str, expected_ref: str, *, require_clean: bool = True
) -> tuple[Path, dict[str, str]]:
    _reject_symlink_components(path, f"{name} checkout")
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise AdapterError(f"{name} checkout does not exist") from exc
    if not resolved.is_dir():
        raise AdapterError(f"{name} checkout must be a directory")
    head = _git(resolved, "rev-parse", "HEAD")
    if head != expected_ref:
        raise AdapterError(f"{name} checkout ref mismatch: expected {expected_ref}, got {head}")
    if require_clean and _git(resolved, "status", "--porcelain=v1", "--untracked-files=all"):
        raise AdapterError(f"{name} checkout must be clean")
    return resolved, {"head": head, "path": str(resolved)}


def _real_root(path: Path, name: str) -> Path:
    _reject_symlink_components(path, name)
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise AdapterError(f"{name} does not exist") from exc
    if not resolved.is_dir():
        raise AdapterError(f"{name} must be a directory")
    return resolved


def editor_marker_path(editor: Path) -> Path:
    """Return the adjacent custody marker path for a disposable editor clone."""
    return editor.with_name(editor.name + EXTERNAL_EDITOR_MARKER_SUFFIX)


def editor_marker_bytes(editor: Path) -> bytes:
    resolved = editor.expanduser().resolve(strict=True)
    value = {
        "schema_version": 1,
        "kind": "m4-4b-editor-checkout",
        "resolved_path": str(resolved),
        "source_ref": APP_REF,
    }
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def pinokio_marker_path(checkout: Path) -> Path:
    return checkout.with_name(checkout.name + EXTERNAL_PINOKIO_MARKER_SUFFIX)


def pinokio_marker_bytes(checkout: Path) -> bytes:
    resolved = checkout.expanduser().resolve(strict=True)
    value = {
        "schema_version": 1,
        "kind": "m4-4b-pinokio-checkout",
        "resolved_path": str(resolved),
        "source_ref": PINOKIO_REF,
    }
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _validate_pinokio_marker(checkout_input: Path, checkout: Path) -> None:
    marker = pinokio_marker_path(checkout_input.expanduser().absolute())
    _reject_symlink_components(marker, "Pinokio custody marker")
    if marker.is_symlink() or not marker.is_file():
        raise AdapterError(
            f"disposable Pinokio checkout requires an adjacent non-symlink marker: {marker}"
        )
    if marker.read_bytes() != pinokio_marker_bytes(checkout):
        raise AdapterError("Pinokio custody marker does not bind this path and source ref")


def _validate_editor_marker(editor_input: Path, editor: Path) -> None:
    marker = editor_marker_path(editor_input.expanduser().absolute())
    _reject_symlink_components(marker, "editor custody marker")
    if marker.is_symlink() or not marker.is_file():
        raise AdapterError(
            f"editor checkout requires an adjacent non-symlink custody marker: {marker}"
        )
    try:
        actual = marker.read_bytes()
    except OSError as exc:
        raise AdapterError("editor custody marker cannot be read") from exc
    if actual != editor_marker_bytes(editor):
        raise AdapterError(
            "editor custody marker does not bind this resolved path and App source ref"
        )


def _state_root(path: Path) -> tuple[Path, dict[str, Any]]:
    root = _real_root(path, "Runtime state root")
    marker = root / DISPOSABLE_MARKER
    if marker.is_symlink() or not marker.is_file() or marker.read_bytes() != MARKER_BYTES:
        raise AdapterError(
            f"Runtime state root lacks its exact internal disposable marker: {marker}"
        )
    for ancestor in root.parents:
        parent_marker = ancestor / DISPOSABLE_MARKER
        if parent_marker.is_file() and not parent_marker.is_symlink():
            raise AdapterError(
                "Runtime state root is nested inside another explicitly disposable root"
            )
    install_path = root / "install.json"
    mode_path = root.parent / "install-mode.json"
    for required, label in (
        (install_path, "Runtime install record"),
        (mode_path, "Pinokio install-mode record"),
    ):
        _reject_symlink_components(required, label)
        if not required.is_file():
            raise AdapterError(f"{label} is missing from disposable custody")
    try:
        install = json.loads(install_path.read_text(encoding="utf-8"))
        mode = json.loads(mode_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError("disposable Runtime install/mode records are invalid") from exc
    if (
        not isinstance(install, Mapping)
        or install.get("managed_by") != "pinokio"
        or install.get("owner") != "haus.astrid.runtime"
    ):
        raise AdapterError("Runtime install record lacks Pinokio/Astrid ownership")
    if mode != {"schema_version": 1, "mode": "local"}:
        raise AdapterError("Pinokio mode record must select exactly fully-local mode")
    data_value = install.get("data_root")
    if not isinstance(data_value, str) or not Path(data_value).is_absolute():
        raise AdapterError("Runtime install record must name an absolute disposable data root")
    data_root = _real_root(Path(data_value), "Runtime data root")
    if data_root != root / "data":
        raise AdapterError(
            "Runtime data root must be the marked program root's isolated data directory"
        )
    return root, {
        "install_path": str(install_path),
        "install_sha256": f"sha256:{_sha256(install_path)}",
        "mode_path": str(mode_path),
        "mode_sha256": f"sha256:{_sha256(mode_path)}",
        "mode": dict(mode),
        "data_root": str(data_root),
    }


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _require_local_origin(checkout: Path, expected_source: Path, label: str) -> str:
    origin = _git(checkout, "remote", "get-url", "origin")
    origin_path = Path(origin).expanduser()
    if not origin_path.is_absolute():
        raise AdapterError(
            f"{label} origin must be an absolute local path; network pulls are forbidden"
        )
    _reject_symlink_components(origin_path, f"{label} origin")
    if origin_path.resolve(strict=True) != expected_source:
        raise AdapterError(f"{label} origin does not point to its authenticated local source")
    return str(origin_path.resolve())


def _regular_digest(path: Path, label: str) -> str:
    _reject_symlink_components(path, label)
    if not path.is_file():
        raise AdapterError(f"{label} must be a regular non-symlink file: {path}")
    return _sha256(path)


def _authenticate_pterm(checkout_input: Path, executable_input: Path) -> dict[str, str]:
    pterm, identity = _checkout(checkout_input, "PTerm engine", PTERM_REF)
    package_path = pterm / "package.json"
    entry_path = pterm / "index.js"
    package_digest = _regular_digest(package_path, "PTerm package manifest")
    lock_digest = _regular_digest(pterm / "package-lock.json", "PTerm dependency lock")
    entry_digest = _regular_digest(entry_path, "PTerm CLI entrypoint")
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError("pinned PTerm package manifest is invalid") from exc
    version = package.get("version") if isinstance(package, dict) else None
    bins = package.get("bin") if isinstance(package, dict) else None
    if not isinstance(version, str) or not version or not isinstance(bins, dict):
        raise AdapterError("pinned PTerm source lacks a versioned CLI package contract")
    bin_entry = bins.get("pterm")
    if not isinstance(bin_entry, str) or (pterm / bin_entry).resolve() != entry_path.resolve():
        raise AdapterError("pinned PTerm package does not bind its pterm command to index.js")
    executable = executable_input.expanduser()
    _reject_symlink_components(executable, "PTerm executable")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise AdapterError("PTerm executable must be an absolute executable regular file")
    executable_digest = _regular_digest(executable, "PTerm executable")
    if executable_digest != entry_digest:
        raise AdapterError(
            "PTerm executable bytes differ from the authenticated pinned CLI entrypoint"
        )
    if not executable.is_absolute():
        raise AdapterError("PTerm executable path must be absolute")
    return {
        "head": identity["head"],
        "path": identity["path"],
        "version": version,
        "protocol": ENGINE_PROTOCOL,
        "package_sha256": f"sha256:{package_digest}",
        "lock_sha256": f"sha256:{lock_digest}",
        "entry_sha256": f"sha256:{entry_digest}",
        "executable": str(executable),
        "executable_sha256": f"sha256:{executable_digest}",
    }


def _authenticate_pinokiod(checkout_input: Path) -> dict[str, str]:
    checkout, identity = _checkout(checkout_input, "Pinokio action engine", PINOKIOD_REF)
    package_path = checkout / "package.json"
    package_digest = _regular_digest(package_path, "pinokiod package manifest")
    lock_digest = _regular_digest(checkout / "package-lock.json", "pinokiod dependency lock")
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError("pinned pinokiod package manifest is invalid") from exc
    if not isinstance(package, Mapping) or package.get("version") != PINOKIOD_VERSION:
        raise AdapterError(f"pinned pinokiod source must declare version {PINOKIOD_VERSION}")
    return {
        **identity,
        "version": PINOKIOD_VERSION,
        "package_sha256": f"sha256:{package_digest}",
        "lock_sha256": f"sha256:{lock_digest}",
    }


def _authenticate_owner(
    checkout_input: Path,
    executable_input: Path,
    *,
    expected_ref: str,
    expected_sha256: str,
) -> dict[str, str]:
    if not _SHA_RE.fullmatch(expected_ref):
        raise AdapterError("owner wrapper ref must be a full 40-character Git SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise AdapterError("owner wrapper digest must be a lowercase SHA-256")
    checkout, identity = _checkout(checkout_input, "owner settlement wrapper", expected_ref)
    executable = executable_input.expanduser()
    _reject_symlink_components(executable, "owner settlement wrapper")
    if (
        not executable.is_absolute()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        raise AdapterError("owner settlement wrapper must be an absolute executable regular file")
    resolved = executable.resolve()
    if not resolved.is_relative_to(checkout):
        raise AdapterError("owner settlement wrapper must be inside its pinned checkout")
    observed = _regular_digest(resolved, "owner settlement wrapper")
    if observed != expected_sha256:
        raise AdapterError(
            "owner settlement wrapper digest differs from the supplied immutable authority"
        )
    return {
        **identity,
        "protocol": ENGINE_PROTOCOL,
        "executable": str(resolved),
        "executable_sha256": f"sha256:{observed}",
    }


def authenticate_inputs(
    *,
    pinokio_source: Path,
    pinokio_checkout: Path,
    runtime_source: Path,
    app_source: Path,
    editor_checkout: Path,
    state_root: Path,
    pterm_checkout: Path,
    pterm_executable: Path,
    pinokiod_checkout: Path,
    owner_checkout: Path,
    owner_executable: Path,
    owner_ref: str,
    owner_sha256: str,
    pinokio_app_ref: str,
) -> dict[str, Any]:
    """Authenticate Linux support, all source identities, custody, and engine before mutation."""
    if platform.system().lower() != "linux":
        raise AdapterError(
            "real fully-local lifecycle requires Linux-native Pinokio; refusing before mutation"
        )
    if not isinstance(pinokio_app_ref, str) or not _REF_RE.fullmatch(pinokio_app_ref):
        raise AdapterError(
            "Pinokio app ref must be a full local pinokio://127.0.0.1:42000/api/<id> ref"
        )
    pinokio_source_root, pinokio_source_identity = _checkout(
        pinokio_source, "read-only Pinokio source", PINOKIO_REF
    )
    pinokio, pinokio_identity = _checkout(
        pinokio_checkout, "disposable Pinokio candidate", PINOKIO_REF, require_clean=False
    )
    _validate_pinokio_marker(pinokio_checkout, pinokio)
    runtime, runtime_identity = _checkout(runtime_source, "Runtime source", RUNTIME_REF)
    app, app_identity = _checkout(app_source, "App source", APP_REF)
    editor, editor_identity = _checkout(editor_checkout, "disposable editor", APP_REF)
    _validate_editor_marker(editor_checkout, editor)
    state, state_identity = _state_root(state_root)
    pterm = _authenticate_pterm(pterm_checkout, pterm_executable)
    pinokiod = _authenticate_pinokiod(pinokiod_checkout)
    owner = _authenticate_owner(
        owner_checkout, owner_executable, expected_ref=owner_ref, expected_sha256=owner_sha256
    )
    expected_editor = pinokio / "app" / "editor"
    expected_state = pinokio / "app" / "astrid"
    if editor != expected_editor or state != expected_state:
        raise AdapterError(
            "editor and Runtime state roots must be the actual Pinokio app/editor and app/astrid paths"
        )
    pinokio_origin = _require_local_origin(pinokio, pinokio_source_root, "disposable Pinokio")
    editor_origin = _require_local_origin(editor, app, "disposable editor")
    editor_branch = _git(editor, "symbolic-ref", "--short", "-q", "HEAD")
    if not editor_branch:
        raise AdapterError(
            "disposable editor must have a named branch for the fully-local Update route"
        )
    environment_path = pinokio / "ENVIRONMENT"
    if environment_path.is_symlink() or not environment_path.is_file():
        raise AdapterError("disposable Pinokio checkout must pin ASTRID_APP_REF in ENVIRONMENT")
    app_refs = [
        line.split("=", 1)[1].strip()
        for line in environment_path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("ASTRID_APP_REF=")
    ]
    if app_refs != [editor_branch]:
        raise AdapterError("ASTRID_APP_REF must exactly match the disposable editor branch")
    independent_roots = (
        pinokio_source_root,
        runtime,
        app,
        pinokio,
        Path(pterm["path"]),
        Path(pinokiod["path"]),
        Path(owner["path"]),
    )
    if any(
        _overlap(independent_roots[i], independent_roots[j])
        for i in range(len(independent_roots))
        for j in range(i + 1, len(independent_roots))
    ):
        raise AdapterError(
            "source, disposable Pinokio, Runtime, App, and PTerm roots must be pairwise disjoint"
        )
    update_action = pinokio / "pinokio" / "update.js"
    action_digest = _regular_digest(update_action, "Pinokio Update action")
    if action_digest != PINOKIO_UPDATE_SHA256:
        raise AdapterError(f"Pinokio Update action digest mismatch: sha256:{action_digest}")
    if _git(pinokio, "status", "--porcelain=v1", "--untracked-files=all"):
        raise AdapterError("Pinokio candidate checkout must be clean")
    build_script = app / BUILD_TAIL[0]
    if build_script.is_symlink() or not build_script.is_file():
        raise AdapterError("authenticated App source is missing its local workspace build script")
    return {
        "pinokio": {
            **pinokio_identity,
            "origin": pinokio_origin,
            "update_action_sha256": f"sha256:{action_digest}",
        },
        "pinokio_source": {
            **pinokio_source_identity,
            "update_action_sha256": f"sha256:{PINOKIO_UPDATE_SHA256}",
        },
        "runtime": runtime_identity,
        "app": app_identity,
        "editor": {
            **editor_identity,
            "branch": editor_branch,
            "origin": editor_origin,
        },
        "state_root": str(state),
        "state": state_identity,
        "pterm": pterm,
        "pinokiod": pinokiod,
        "owner": owner,
        "route_id": ROUTE_ID,
    }


def _exact_argv(provenance: Mapping[str, Any], pinokio_app_ref: str) -> list[str]:
    return [
        provenance["pterm"]["executable"],
        "start",
        "pinokio/update.js",
        "--ref",
        pinokio_app_ref,
    ]


def _owner_argv(provenance: Mapping[str, Any], downstream: Sequence[str]) -> list[str]:
    return [
        provenance["owner"]["executable"],
        "--protocol",
        ENGINE_PROTOCOL,
        "--downstream-argv-json",
        json.dumps(list(downstream), separators=(",", ":")),
        "--pterm-ref",
        provenance["pterm"]["head"],
        "--pinokiod-ref",
        provenance["pinokiod"]["head"],
        "--pinokiod-version",
        provenance["pinokiod"]["version"],
        "--candidate-ref",
        provenance["pinokio"]["head"],
        "--action-sha256",
        provenance["pinokio"]["update_action_sha256"],
        "--inject-failure-step",
        "editor_build",
        "--injection-mechanism",
        "real-build-process-fault",
    ]


def _authenticate_pterm_home(provenance: Mapping[str, Any], pinokio_app_ref: str) -> Path:
    app_id = pinokio_app_ref.rsplit("/", 1)[1]
    pinokio = Path(provenance["pinokio"]["path"])
    expected_home = pinokio.parent.parent.resolve()
    if pinokio.parent.name != "api" or pinokio.name != app_id:
        raise AdapterError(
            "Pinokio app ref does not resolve to the authenticated disposable API checkout"
        )
    argv = [provenance["pterm"]["executable"], "home", "--json"]
    try:
        result = subprocess.run(
            argv,
            cwd=provenance["pterm"]["path"],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdapterError("PTerm home authority probe timed out") from exc
    if result.returncode:
        raise AdapterError(
            f"PTerm could not authenticate its Pinokio home: {result.stderr.strip()}"
        )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AdapterError("PTerm home probe did not return JSON") from exc
    reported = (
        value
        if isinstance(value, str)
        else (
            value.get("home") or value.get("path") or value.get("PINOKIO_HOME")
            if isinstance(value, Mapping)
            else None
        )
    )
    if not isinstance(reported, str) or Path(reported).expanduser().resolve() != expected_home:
        raise AdapterError(
            "PTerm server home does not contain the authenticated disposable Pinokio app checkout"
        )
    return expected_home


def _output_path(output: Path, provenance: Mapping[str, Any]) -> Path:
    path = output.expanduser().absolute()
    _reject_symlink_components(path, "evidence output")
    resolved = path.resolve()
    roots = [
        Path(provenance[key]["path"])
        for key in (
            "pinokio_source",
            "pinokio",
            "runtime",
            "app",
            "editor",
            "pterm",
            "pinokiod",
            "owner",
        )
    ]
    roots.extend((Path(provenance["state_root"]),))
    if any(_overlap(resolved, root) for root in roots):
        raise AdapterError("evidence output must be outside all authenticated custody roots")
    if path.exists():
        raise AdapterError("evidence output must be a new path")
    return path


def _atomic_exclusive_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def run_route(
    *,
    pinokio_source: Path,
    pinokio_checkout: Path,
    runtime_source: Path,
    app_source: Path,
    editor_checkout: Path,
    state_root: Path,
    pterm_checkout: Path,
    pterm_executable: Path,
    pinokiod_checkout: Path,
    owner_checkout: Path,
    owner_executable: Path,
    owner_ref: str,
    owner_sha256: str,
    pinokio_app_ref: str,
    output: Path,
) -> dict[str, Any]:
    """Run only the authenticated PTerm action route and require settled evidence."""
    provenance = authenticate_inputs(
        pinokio_source=pinokio_source,
        pinokio_checkout=pinokio_checkout,
        runtime_source=runtime_source,
        app_source=app_source,
        editor_checkout=editor_checkout,
        state_root=state_root,
        pterm_checkout=pterm_checkout,
        pterm_executable=pterm_executable,
        pinokiod_checkout=pinokiod_checkout,
        owner_checkout=owner_checkout,
        owner_executable=owner_executable,
        owner_ref=owner_ref,
        owner_sha256=owner_sha256,
        pinokio_app_ref=pinokio_app_ref,
    )
    out = _output_path(output, provenance)
    _authenticate_pterm_home(provenance, pinokio_app_ref)
    downstream = _exact_argv(provenance, pinokio_app_ref)
    argv = _owner_argv(provenance, downstream)
    process = subprocess.Popen(
        argv,
        cwd=provenance["owner"]["path"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    try:
        stdout, stderr = process.communicate(timeout=ENGINE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise AdapterError(
            "owner-wrapped PTerm action timed out; task settlement is unknown and receipt is refused"
        ) from exc
    if process.returncode:
        raise AdapterError(
            f"owner settlement wrapper exited {process.returncode}; settlement evidence is required: {stderr.strip()}"
        )
    try:
        evidence = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AdapterError(
            "owner wrapper returned no machine-readable accepted-and-settled evidence"
        ) from exc
    evidence["owner_process"] = {
        "pid": process.pid,
        "argv": argv,
        "exit_status": process.returncode,
    }
    _validate_engine_evidence(
        evidence,
        provenance,
        expected_argv=downstream,
        expected_owner_argv=argv,
        pinokio_app_ref=pinokio_app_ref,
    )
    evidence[HOSTED_NEED] = True
    evidence["hosted_prerequisite"] = (
        "Run the hosted Linux M4-4b qualification on the approved runner; this local run is not that receipt."
    )
    evidence["candidate_provenance"] = provenance
    _atomic_exclusive_json(out, evidence)
    return evidence


def _snapshot_pair(snapshots: Any, name: str) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if not isinstance(snapshots, Mapping) or not isinstance(snapshots.get(name), Mapping):
        raise AdapterError(f"missing before/after {name} snapshots")
    value = snapshots[name]
    before, after = value.get("before"), value.get("after")
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        raise AdapterError(f"{name} snapshots require structured before and after values")
    return before, after


def _validate_engine_evidence(
    evidence: Any,
    provenance: Mapping[str, Any],
    *,
    expected_argv: Sequence[str],
    expected_owner_argv: Sequence[str],
    pinokio_app_ref: str,
) -> None:
    if not isinstance(evidence, dict):
        raise AdapterError("action engine evidence must be a JSON object")
    evidence_class = evidence.get("evidence_class")
    if evidence_class == "source_fake":
        raise AdapterError("source_fake evidence cannot make real-boundary claims or be written")
    if evidence_class != "real_subprocess":
        raise AdapterError("evidence_class must be real_subprocess")
    expected_engine = {
        "pterm": provenance["pterm"],
        "pinokiod": provenance["pinokiod"],
        "owner": provenance["owner"],
    }
    if evidence.get("protocol") != ENGINE_PROTOCOL or evidence.get("engine") != expected_engine:
        raise AdapterError("engine protocol or pinned pterm/pinokiod/owner provenance differs")
    if evidence.get("route_id") != ROUTE_ID or evidence.get("mode") != "local":
        raise AdapterError("evidence does not attest the fully-local editor Update route")
    if evidence.get("backend") is not False or evidence.get("foreground_service") is not False:
        raise AdapterError("backend and foreground-service routes are forbidden")
    if evidence.get("provenance") != provenance:
        raise AdapterError(
            "evidence provenance does not match authenticated Pinokio/Runtime/App/engine sources"
        )
    request = evidence.get("request")
    if (
        not isinstance(request, Mapping)
        or not isinstance(request.get("request_id"), str)
        or not request["request_id"].strip()
        or request.get("argv") != list(expected_argv)
        or request.get("operation") != "start"
        or request.get("action") != "pinokio/update.js"
        or request.get("pinokio_app_ref") != pinokio_app_ref
    ):
        raise AdapterError(
            "request evidence does not attest the exact pterm start action and app ref"
        )
    owner_process = evidence.get("owner_process")
    if (
        not isinstance(owner_process, Mapping)
        or type(owner_process.get("pid")) is not int
        or owner_process["pid"] <= 0
        or owner_process.get("argv") != list(expected_owner_argv)
        or owner_process.get("exit_status") != 0
    ):
        raise AdapterError(
            "adapter-observed owner wrapper pid, argv, and successful exit are required"
        )
    pterm_process = evidence.get("pterm_process")
    if (
        not isinstance(pterm_process, Mapping)
        or type(pterm_process.get("pid")) is not int
        or pterm_process["pid"] <= 0
        or pterm_process.get("argv") != list(expected_argv)
        or pterm_process.get("exit_status") != 0
    ):
        raise AdapterError(
            "owner evidence must bind a real PTerm child pid to the exact downstream argv"
        )
    task = evidence.get("task")
    if (
        not isinstance(task, Mapping)
        or task.get("request_id") != request.get("request_id")
        or not isinstance(task.get("task_id"), str)
        or not task["task_id"].strip()
        or task.get("accepted") is not True
        or task.get("settled") is not True
        or task.get("terminal") != "failed"
    ):
        raise AdapterError(
            "Pinokio task was not both accepted and settled with the expected failed terminal state"
        )
    action = evidence.get("action")
    if (
        not isinstance(action, Mapping)
        or action.get("route_id") != ROUTE_ID
        or action.get("ref") != provenance["pinokio"]["head"]
        or action.get("sha256") != provenance["pinokio"]["update_action_sha256"]
    ):
        raise AdapterError(
            "evidence action ref/digest does not match the authenticated Update descriptor"
        )
    events = evidence.get("events")
    if not isinstance(events, list) or [
        e.get("name") if isinstance(e, Mapping) else None for e in events
    ] != list(EVENT_ORDER):
        raise AdapterError("evidence must contain the ordered local Update lifecycle events")
    if any(e.get("sequence") != index for index, e in enumerate(events, start=1)):
        raise AdapterError("lifecycle event sequence numbers must be contiguous and ordered")
    if (
        events[9].get("status") != "failed"
        or events[17].get("ready") is not True
        or events[17].get("local_set") is not True
    ):
        raise AdapterError(
            "event stream must show build failure then only post-restore ready/local.set"
        )
    if any(e.get("ready") is True or e.get("local_set") is True for e in events[:17]):
        raise AdapterError("ready/local.set cannot be published before all post-restore checks")
    failure = evidence.get("failure")
    build_argv = failure.get("argv") if isinstance(failure, Mapping) else None
    if (
        not isinstance(failure, Mapping)
        or failure.get("step") != "editor_build"
        or failure.get("injected") is not True
        or failure.get("exit_status", 0) == 0
        or type(failure.get("exit_status")) is not int
        or failure.get("pinokio_failure_marker") != "ASTRID_PINOKIO_FAILURE"
        or failure.get("pinokio_failure_marker_seen") is not True
        or type(failure.get("pid")) is not int
        or failure["pid"] <= 0
        or failure.get("injection_authority") != provenance["owner"]
        or failure.get("injection_mechanism") != "real-build-process-fault"
        or not isinstance(build_argv, list)
        or tuple(build_argv[-2:]) != BUILD_TAIL
    ):
        raise AdapterError(
            "evidence must prove injected nonzero real build argv and Pinokio failure marker"
        )
    processes = evidence.get("processes")
    if not isinstance(processes, list) or not processes:
        raise AdapterError("real subprocess evidence requires process ids and argv")
    for process in processes:
        if (
            not isinstance(process, Mapping)
            or type(process.get("pid")) is not int
            or process["pid"] <= 0
            or not isinstance(process.get("argv"), list)
            or not process["argv"]
        ):
            raise AdapterError("real process record requires a positive pid and exact argv")
    if not any(p["pid"] == failure["pid"] and p["argv"] == build_argv for p in processes):
        raise AdapterError("build failure pid/argv is absent from the real process records")
    snapshots = evidence.get("snapshots")
    checkout_before, _checkout_after = _snapshot_pair(snapshots, "checkout")
    if checkout_before.get("head") != provenance["editor"]["head"]:
        raise AdapterError("checkout snapshot does not bind the disposable editor source ref")
    runtime_before, _runtime_after = _snapshot_pair(snapshots, "runtime")
    if runtime_before.get("root") != provenance["state_root"]:
        raise AdapterError("Runtime snapshot does not bind the marked state root")
    lock_before, _lock_after = _snapshot_pair(snapshots, "lock")
    if lock_before.get("held") is not False or lock_before.get("released") is not True:
        raise AdapterError("final installer lock snapshot must prove released custody")
    mode_before, _mode_after = _snapshot_pair(snapshots, "mode")
    if (
        mode_before.get("value") != provenance["state"]["mode"]
        or mode_before.get("sha256") != provenance["state"]["mode_sha256"]
    ):
        raise AdapterError("mode snapshot does not bind the authenticated local mode record")
    data_before, _data_after = _snapshot_pair(snapshots, "data")
    expected_data_root = provenance["state"]["data_root"]
    if data_before.get("root") != expected_data_root:
        raise AdapterError("data snapshot does not bind the Runtime data root")
    for name in ("checkout", "runtime", "lock", "mode", "data"):
        before, after = _snapshot_pair(snapshots, name)
        if dict(before) != dict(after):
            raise AdapterError(f"{name} before/after snapshot changed across rollback")
    ready_before, ready_after = _snapshot_pair(snapshots, "readiness")
    if not ready_before or ready_after.get("status") != 200 or ready_after.get("ready") is not True:
        raise AdapterError("readiness snapshots must include the post-restore local ready state")
    if (
        ready_after.get("route_id") != ROUTE_ID
        or ready_after.get("mode") != "local"
        or ready_after.get("local_set") is not True
        or ready_after.get("hosted") is not False
    ):
        raise AdapterError("post-restore readiness is not local, route-bound, and complete")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pinokio", type=Path, required=True)
    parser.add_argument("--pinokio-source", type=Path, required=True)
    parser.add_argument("--runtime-source", type=Path, required=True)
    parser.add_argument("--app-source", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--pterm-checkout", type=Path, required=True)
    parser.add_argument("--pterm-executable", type=Path, required=True)
    parser.add_argument("--pinokiod-checkout", type=Path, required=True)
    parser.add_argument("--owner-checkout", type=Path, required=True)
    parser.add_argument("--owner-executable", type=Path, required=True)
    parser.add_argument("--owner-ref", required=True)
    parser.add_argument("--owner-sha256", required=True)
    parser.add_argument("--pinokio-app-ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        run_route(
            pinokio_checkout=args.pinokio,
            runtime_source=args.runtime_source,
            pinokio_source=args.pinokio_source,
            app_source=args.app_source,
            editor_checkout=args.checkout,
            state_root=args.state_root,
            pterm_checkout=args.pterm_checkout,
            pterm_executable=args.pterm_executable,
            pinokiod_checkout=args.pinokiod_checkout,
            owner_checkout=args.owner_checkout,
            owner_executable=args.owner_executable,
            owner_ref=args.owner_ref,
            owner_sha256=args.owner_sha256,
            pinokio_app_ref=args.pinokio_app_ref,
            output=args.output,
        )
    except (AdapterError, OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        print(f"fully-local lifecycle adapter rejected: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
