#!/usr/bin/env python3
"""Bridge the M4-5 journey protocol to installed, browser, and update processes.

The shared Release-2 executor deliberately knows nothing about a product UI or
an installed product.  This executable is the other half of that boundary.  It
accepts one ``astrid-release2-boundary/v1`` request on stdin, invokes concrete
surface commands as argv arrays (never through a shell), and returns only
observations made by those processes.

The three commands implement ``astrid-installed-browser-interface/v1``.  They
receive an envelope on stdin and return an envelope containing their observed
result.  The adapter validates route ordering and records a durable journal in
the supplied data root.  In particular, the operation learned from a lost
button acknowledgement is the only operation accepted by the later explicit
Update action.

This module does not install Astrid, drive a particular browser, or invent a
receipt.  Product owners must supply commands which do those things on the
host.  ``--evidence-kind disposable-executable`` keeps focused process tests
visibly distinct from ``installed-executable`` hosted observations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

BOUNDARY_PROTOCOL = "astrid-release2-boundary/v1"
INTERFACE_PROTOCOL = "astrid-installed-browser-interface/v1"
JOURNAL_FORMAT = "astrid-installed-browser-adapter/v1"
EVIDENCE_KINDS = ("disposable-executable", "installed-executable")
SURFACES = ("installed", "browser", "update")
SHA256_LENGTH = 64
COMMIT_LENGTH = 40
OUTER_ACTION_TIMEOUT_SECONDS = 120.0
OUTER_TIMEOUT_SAFETY_SECONDS = 10.0
DEFAULT_ACTION_TIMEOUT_SECONDS = OUTER_ACTION_TIMEOUT_SECONDS - OUTER_TIMEOUT_SAFETY_SECONDS
PROCESS_TREE_KILL_TIMEOUT_SECONDS = 2.0
PROCESS_PIPE_DRAIN_TIMEOUT_SECONDS = 2.0
PROCESS_FINAL_WAIT_TIMEOUT_SECONDS = 1.0


class AdapterError(RuntimeError):
    """The configured interface or one of its observations failed closed."""


def _deadline_remaining(deadline: float, operation: str) -> float:
    if not math.isfinite(deadline):
        raise AdapterError("action deadline must be finite")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AdapterError(f"action deadline expired {operation}")
    return remaining


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AdapterError(f"{label} must be an object")
    return dict(value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AdapterError(f"{label} must be a non-empty string")
    return value


def _expect(value: object, expected: object, label: str) -> None:
    if value != expected:
        raise AdapterError(f"{label} was {value!r}; expected {expected!r}")


def _digest(value: object, label: str) -> str:
    text = _text(value, label)
    if len(text) != SHA256_LENGTH or any(character not in "0123456789abcdef" for character in text):
        raise AdapterError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _commit(value: object, label: str) -> str:
    text = _text(value, label)
    if len(text) != COMMIT_LENGTH or any(character not in "0123456789abcdef" for character in text):
        raise AdapterError(f"{label} must be a lowercase full commit SHA")
    return text


def _validate_scenario(scenario: Mapping[str, Any]) -> None:
    _expect(
        set(scenario),
        {"scenario_id", "channel", "mode", "os", "job_id"},
        "request scenario fields",
    )
    channel = scenario.get("channel")
    mode = scenario.get("mode")
    os_name = scenario.get("os")
    if os_name not in {"macos", "linux", "windows"}:
        raise AdapterError("request scenario OS is not an admitted M4-5 host")
    admitted_modes = {
        "button": {"direct"},
        "install-line": {"direct"},
        "pinokio-update": {"pinokio-backend", "pinokio-fully-local"},
    }
    if channel not in admitted_modes or mode not in admitted_modes[channel]:
        raise AdapterError("request scenario channel/mode is not one of the twelve M4-5 rows")
    _expect(
        scenario.get("scenario_id"),
        f"release2-{channel}-{mode}-{os_name}",
        "request scenario identity",
    )
    _expect(scenario.get("job_id"), f"{mode}-{os_name}", "request job identity")


def _validate_request_envelope(request: Mapping[str, Any]) -> None:
    _expect(
        set(request),
        {"protocol", "sequence", "action", "scenario", "inputs", "payload"},
        "request fields",
    )
    _expect(request.get("protocol"), BOUNDARY_PROTOCOL, "request protocol")
    sequence = request.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise AdapterError("request sequence must be a positive integer")
    action = _text(request.get("action"), "request action")
    scenario = _mapping(request.get("scenario"), "request scenario")
    _validate_scenario(scenario)
    inputs = _mapping(request.get("inputs"), "request inputs")
    _expect(
        set(inputs),
        {"candidate", "stand_in", "stand_in_marker", "root", "data_root"},
        "request input fields",
    )
    candidate = _mapping(inputs.get("candidate"), "request candidate")
    _expect(set(candidate), {"app", "agent", "runtime", "pinokio"}, "candidate fields")
    for name, value in candidate.items():
        _commit(value, f"candidate {name}")
    stand_in = _mapping(inputs.get("stand_in"), "request stand-in")
    _expect(
        set(stand_in),
        {
            "fixture_sums_sha256",
            "descriptor_sha256",
            "manifest_sha256",
            "release",
            "qualification_marker",
            "target",
        },
        "stand-in fields",
    )
    for name in ("fixture_sums_sha256", "descriptor_sha256", "manifest_sha256"):
        _digest(stand_in.get(name), f"stand-in {name}")
    _expect(stand_in.get("release"), 2, "stand-in release")
    marker = _text(inputs.get("stand_in_marker"), "stand-in marker")
    _expect(stand_in.get("qualification_marker"), marker, "stand-in marker identity")
    target = _mapping(stand_in.get("target"), "stand-in target")
    _expect(set(target), {"agent", "runtime", "schema_digest"}, "stand-in target fields")
    _commit(target.get("agent"), "stand-in agent")
    _commit(target.get("runtime"), "stand-in runtime")
    schema_digest = _text(target.get("schema_digest"), "stand-in schema digest")
    if not schema_digest.startswith("sha256:"):
        raise AdapterError("stand-in schema digest must use the sha256 namespace")
    _digest(schema_digest.removeprefix("sha256:"), "stand-in schema digest")
    root = Path(_text(inputs.get("root"), "request install root"))
    data_root = Path(_text(inputs.get("data_root"), "request data root"))
    if (
        not root.is_absolute()
        or not data_root.is_absolute()
        or root.resolve(strict=False) == data_root.resolve(strict=False)
    ):
        raise AdapterError("request install and data roots must be distinct absolute paths")
    payload = _mapping(request.get("payload"), "request payload")
    expected_payloads = {
        "inspect-release1": set(),
        "button-unsaved-draft": {"surface", "save", "request"},
        "button-update-lost-ack": {"request", "draft_flush", "simulate_lost_ack"},
        "button-update-explicit": {"request", "operation_id"},
        "install-line-update": {
            "update_flag",
            "manifest_sha256",
            "root_bound",
            "data_root_bound",
        },
        "pinokio-update": {"mode", "route", "foreground_fallback"},
        "inspect-release2": set(),
        "cleanup": {"owned_roots_only"},
    }
    if action not in expected_payloads:
        raise AdapterError(f"unsupported boundary action: {action!r}")
    _expect(set(payload), expected_payloads[action], f"{action} payload fields")
    exact_payloads = {
        "inspect-release1": {},
        "button-unsaved-draft": {
            "surface": "browser",
            "save": "failed",
            "request": "explicit-update",
        },
        "button-update-lost-ack": {
            "request": "explicit-update",
            "draft_flush": "pass",
            "simulate_lost_ack": True,
        },
        "install-line-update": {
            "update_flag": True,
            "manifest_sha256": stand_in["manifest_sha256"],
            "root_bound": True,
            "data_root_bound": True,
        },
        "pinokio-update": {
            "mode": scenario["mode"],
            "route": (
                "backend-adapter"
                if scenario["mode"] == "pinokio-backend"
                else "existing-fully-local"
            ),
            "foreground_fallback": False,
        },
        "inspect-release2": {},
        "cleanup": {"owned_roots_only": True},
    }
    if action == "button-update-explicit":
        _expect(payload.get("request"), "explicit-update", "explicit Update request")
        _text(payload.get("operation_id"), "explicit Update operation ID")
    else:
        _expect(payload, exact_payloads[action], f"{action} payload")


def _read_request() -> dict[str, Any]:
    try:
        value = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        raise AdapterError("stdin must contain one JSON request object") from exc
    return _mapping(value, "boundary request")


def _read_request_before(deadline: float) -> dict[str, Any]:
    """Read stdin without permitting an unavailable writer to exceed the action budget."""

    result: list[dict[str, Any] | Exception] = []

    def read() -> None:
        try:
            result.append(_read_request())
        except (AdapterError, OSError) as exc:  # transported back to the main thread
            result.append(exc)

    reader = threading.Thread(target=read, name="installed-browser-request", daemon=True)
    reader.start()
    reader.join(_deadline_remaining(deadline, "before reading the boundary request"))
    if reader.is_alive():
        raise AdapterError("action deadline expired while reading the boundary request")
    _deadline_remaining(deadline, "after reading the boundary request")
    value = result[0]
    if isinstance(value, Exception):
        raise value
    return value


def _safe_data_root(request: Mapping[str, Any]) -> Path:
    inputs = _mapping(request.get("inputs"), "request inputs")
    raw = Path(_text(inputs.get("data_root"), "request data_root"))
    if not raw.is_absolute():
        raise AdapterError("request data_root must be absolute")
    if raw.is_symlink():
        raise AdapterError("request data_root must not be a symlink")
    raw.mkdir(parents=True, exist_ok=True)
    resolved = raw.resolve(strict=True)
    if not resolved.is_dir() or resolved.is_symlink():
        raise AdapterError("request data_root must be a real directory")
    return resolved


def _validate_evidence_mode(
    evidence_kind: str,
    request: Mapping[str, Any],
    command_provenance: Mapping[str, Any] | None,
) -> None:
    if evidence_kind not in EVIDENCE_KINDS:
        raise AdapterError(f"unsupported evidence kind: {evidence_kind!r}")
    if evidence_kind == "disposable-executable":
        return
    if (
        os.environ.get("CI", "").lower() != "true"
        or os.environ.get("GITHUB_ACTIONS", "").lower() != "true"
    ):
        raise AdapterError("installed-executable evidence requires the hosted CI boundary")
    actual_os = {"darwin": "macos", "linux": "linux", "win32": "windows"}.get(sys.platform)
    expected_os = _mapping(request.get("scenario"), "request scenario").get("os")
    _expect(actual_os, expected_os, "installed-executable host OS")
    provenance = _mapping(command_provenance, "trusted command provenance")
    _expect(set(provenance), set(SURFACES), "trusted command surfaces")
    for surface in SURFACES:
        observed = _mapping(provenance.get(surface), f"trusted {surface} command")
        _expect(observed.get("trusted"), True, f"trusted {surface} registration")
        _digest(observed.get("identity_sha256"), f"trusted {surface} command identity")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise AdapterError(f"refusing symlinked adapter journal: {path}")
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


def _read_journal(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise AdapterError("adapter journal must be a non-symlink regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(f"adapter journal is unavailable or malformed: {exc}") from exc
    journal = _mapping(value, "adapter journal")
    _validate_journal(journal)
    return journal


@contextmanager
def _journal_lock(path: Path, deadline: float):
    """Hold an OS-released cross-process lock without treating a stale file as held."""

    _deadline_remaining(deadline, "before opening the adapter journal lock")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise AdapterError("adapter lock must be a non-symlink regular file")
    path.touch(mode=0o600, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {13, 36}:
                        raise
                    remaining = _deadline_remaining(
                        deadline, "while acquiring the adapter journal lock"
                    )
                    time.sleep(min(0.01, remaining))
            try:
                _deadline_remaining(deadline, "after acquiring the adapter journal lock")
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = _deadline_remaining(
                        deadline, "while acquiring the adapter journal lock"
                    )
                    time.sleep(min(0.01, remaining))
            try:
                _deadline_remaining(deadline, "after acquiring the adapter journal lock")
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _resolve_command(executable: str, arguments: Sequence[str], label: str) -> tuple[str, ...]:
    if not executable or any(not isinstance(part, str) or not part for part in arguments):
        raise AdapterError(f"{label} command must contain non-empty separate argv values")
    selected = shutil.which(executable) if os.sep not in executable else executable
    if not selected:
        raise AdapterError(f"{label} executable was not found: {executable}")
    path = Path(selected)
    if path.is_symlink():
        path = path.resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise AdapterError(f"{label} executable is not an executable regular file: {path}")
    return (str(path.resolve()), *arguments)


def _command_identity(command: Sequence[str], *, deadline: float | None = None) -> dict[str, Any]:
    if deadline is not None:
        _deadline_remaining(deadline, "before hashing a command executable")
    executable = Path(command[0])
    identity = {
        "argv": list(command),
        "argv_sha256": _sha256_bytes(_canonical_bytes(list(command))),
        "executable": str(executable),
        "executable_sha256": _sha256_bytes(executable.read_bytes()),
    }
    if deadline is not None:
        _deadline_remaining(deadline, "after hashing a command executable")
    return identity


def _command_provenance(
    *,
    surface: str,
    command: Sequence[str],
    artifact_paths: Sequence[str],
    mutable_arg_indices: Sequence[int],
    trusted_identities: Sequence[str],
    require_trust: bool,
    deadline: float | None = None,
) -> dict[str, Any]:
    mutable_indices: set[int] = set()
    for raw_index in mutable_arg_indices:
        if (
            not isinstance(raw_index, int)
            or isinstance(raw_index, bool)
            or raw_index < 1
            or raw_index >= len(command)
        ):
            raise AdapterError(
                f"{surface} mutable argv indices must identify values after the executable"
            )
        if raw_index in mutable_indices:
            raise AdapterError(f"{surface} mutable argv index {raw_index} was supplied twice")
        mutable_indices.add(raw_index)

    artifacts: dict[Path, dict[str, str]] = {}
    for index, raw in enumerate(artifact_paths, 1):
        if deadline is not None:
            _deadline_remaining(deadline, f"before hashing {surface} artifact {index}")
        path = Path(raw)
        if not path.is_absolute():
            raise AdapterError(f"{surface} artifact {index} must be an absolute path")
        if path.is_symlink() or not path.is_file():
            raise AdapterError(f"{surface} artifact {index} must be a non-symlink regular file")
        resolved = path.resolve(strict=True)
        if resolved in artifacts:
            raise AdapterError(f"{surface} artifact {resolved} was supplied more than once")
        artifacts[resolved] = {
            "path": str(resolved),
            "sha256": _sha256_bytes(resolved.read_bytes()),
        }
        if deadline is not None:
            _deadline_remaining(deadline, f"after hashing {surface} artifact {index}")

    normalized_argv = ["@executable"]
    referenced_artifacts: set[Path] = set()
    for index, value in enumerate(command[1:], 1):
        candidate = Path(value)
        resolved: Path | None = None
        if candidate.exists() and candidate.is_file():
            if candidate.is_symlink():
                raise AdapterError(f"{surface} argv[{index}] must not be a symlinked artifact")
            resolved = candidate.resolve(strict=True)
        if resolved is not None:
            if index in mutable_indices and resolved in artifacts:
                raise AdapterError(
                    f"{surface} argv[{index}] cannot be both mutable and an artifact pin"
                )
            if index in mutable_indices:
                normalized_argv.append(value)
                continue
            if resolved in artifacts:
                referenced_artifacts.add(resolved)
                normalized_argv.append(f"@artifact:{artifacts[resolved]['sha256']}")
                continue
            if require_trust:
                raise AdapterError(
                    f"{surface} argv[{index}] regular file is neither a registered artifact "
                    "nor a declared mutable/output argument"
                )
        normalized_argv.append(value)
    if require_trust and "-m" in command[1:]:
        raise AdapterError(
            f"{surface} installed command cannot use unpinned Python -m module source"
        )
    if require_trust and set(artifacts) != referenced_artifacts:
        raise AdapterError(f"{surface} registered artifacts must each appear in its argv")

    command_identity = _command_identity(command, deadline=deadline)
    identity_document = {
        "surface": surface,
        "executable_sha256": command_identity["executable_sha256"],
        "normalized_argv": normalized_argv,
        "artifact_sha256": sorted(item["sha256"] for item in artifacts.values()),
        "mutable_arg_indices": sorted(mutable_indices),
    }
    identity_sha256 = _sha256_bytes(_canonical_bytes(identity_document))
    trusted = False
    if require_trust:
        if not trusted_identities:
            raise AdapterError(
                f"trusted {surface} command identities are required in installed mode"
            )
        pins = [_digest(value, f"trusted {surface} identity") for value in trusted_identities]
        if identity_sha256 not in pins:
            raise AdapterError(f"{surface} command/artifact identity is not registered")
        trusted = True
    return {
        "identity_sha256": identity_sha256,
        "trusted": trusted,
        "command": command_identity,
        "artifacts": [artifacts[path] for path in sorted(artifacts, key=str)],
        "mutable_arg_indices": sorted(mutable_indices),
    }


def _reverify_trusted_command(
    surface: str,
    command: Sequence[str],
    command_provenance: Mapping[str, Any] | None,
    deadline: float,
) -> None:
    provenance = _mapping(command_provenance, f"trusted {surface} command")
    _expect(provenance.get("trusted"), True, f"trusted {surface} registration")
    artifacts_raw = provenance.get("artifacts")
    if not isinstance(artifacts_raw, list):
        raise AdapterError(f"trusted {surface} artifacts must be a list")
    artifact_paths = [
        _text(_mapping(item, f"trusted {surface} artifact").get("path"), "artifact path")
        for item in artifacts_raw
    ]
    identity = _digest(provenance.get("identity_sha256"), f"trusted {surface} identity")
    mutable_arg_indices = provenance.get("mutable_arg_indices")
    if not isinstance(mutable_arg_indices, list):
        raise AdapterError(f"trusted {surface} mutable argv indices must be a list")
    observed = _command_provenance(
        surface=surface,
        command=command,
        artifact_paths=artifact_paths,
        mutable_arg_indices=mutable_arg_indices,
        trusted_identities=[identity],
        require_trust=True,
        deadline=deadline,
    )
    _expect(observed, provenance, f"trusted {surface} command revalidation")


def _expected_actions(scenario: Mapping[str, Any]) -> tuple[str, ...]:
    channel = scenario.get("channel")
    os_name = scenario.get("os")
    if channel == "button":
        actions = ["inspect-release1"]
        if os_name == "linux":
            actions.append("button-unsaved-draft")
        actions.extend(("button-update-lost-ack", "button-update-explicit"))
    elif channel == "install-line":
        actions = ["inspect-release1", "install-line-update"]
    elif channel == "pinokio-update":
        actions = ["inspect-release1", "pinokio-update"]
    else:
        raise AdapterError(f"unsupported M4-5 channel: {channel!r}")
    actions.extend(("inspect-release2", "cleanup"))
    return tuple(actions)


def _phase_after(action: str, before: str) -> str:
    transitions = {
        ("inspect-release1", "uninspected"): "release1",
        ("button-unsaved-draft", "release1"): "release1",
        ("button-update-lost-ack", "release1"): "recovery-needed",
        ("button-update-explicit", "recovery-needed"): "release2",
        ("install-line-update", "release1"): "release2",
        ("pinokio-update", "release1"): "release2",
        ("inspect-release2", "release2"): "release2-inspected",
        ("cleanup", "release2-inspected"): "terminal",
    }
    try:
        return transitions[(action, before)]
    except KeyError as exc:
        raise AdapterError(f"invalid journal transition {before!r} -> {action!r}") from exc


def _validate_process_evidence(value: object, surface: str, label: str) -> None:
    evidence = _mapping(value, label)
    _expect(
        set(evidence),
        {
            "surface",
            "pid",
            "command",
            "interface_evidence",
            "request_sha256",
            "response_sha256",
            "observation_sha256",
        },
        f"{label} fields",
    )
    _expect(evidence.get("surface"), surface, f"{label} surface")
    pid = evidence.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise AdapterError(f"{label} PID must be a positive integer")
    command = _mapping(evidence.get("command"), f"{label} command")
    _expect(
        set(command),
        {"argv", "argv_sha256", "executable", "executable_sha256"},
        f"{label} command fields",
    )
    argv = command.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(part, str) or not part for part in argv)
    ):
        raise AdapterError(f"{label} command argv must contain separate non-empty values")
    _expect(command.get("executable"), argv[0], f"{label} command executable")
    _expect(
        command.get("argv_sha256"),
        _sha256_bytes(_canonical_bytes(argv)),
        f"{label} command argv identity",
    )
    for name in (
        "argv_sha256",
        "executable_sha256",
        "request_sha256",
        "response_sha256",
        "observation_sha256",
    ):
        source = command if name in command else evidence
        _digest(source.get(name), f"{label} {name}")
    interface = _mapping(evidence.get("interface_evidence"), f"{label} interface evidence")
    _expect(interface.get("kind"), f"{surface}-process", f"{label} interface kind")
    _expect(interface.get("pid"), pid, f"{label} interface PID")
    _expect(interface.get("synthetic"), False, f"{label} interface synthetic flag")
    _expect(interface.get("source_only"), False, f"{label} interface source-only flag")


def _validate_journal(journal: Mapping[str, Any]) -> None:
    required = {
        "format",
        "scenario",
        "inputs_sha256",
        "expected_actions",
        "next_sequence",
        "phase",
        "operation_id",
        "semantic_data_sha256",
        "inflight",
        "actions",
        "cleanup",
    }
    _expect(set(journal), required, "adapter journal fields")
    _expect(journal.get("format"), JOURNAL_FORMAT, "adapter journal format")
    scenario = _mapping(journal.get("scenario"), "journal scenario")
    _validate_scenario(scenario)
    expected_actions = list(_expected_actions(scenario))
    _expect(journal.get("expected_actions"), expected_actions, "journal route")
    _digest(journal.get("inputs_sha256"), "journal inputs identity")
    actions = journal.get("actions")
    if not isinstance(actions, list):
        raise AdapterError("journal actions must be a list")
    next_sequence = journal.get("next_sequence")
    if (
        not isinstance(next_sequence, int)
        or isinstance(next_sequence, bool)
        or next_sequence != len(actions) + 1
        or next_sequence > len(expected_actions) + 1
    ):
        raise AdapterError("journal next sequence is inconsistent with completed actions")
    phase = "uninspected"
    operation_id: str | None = None
    for index, item_raw in enumerate(actions, 1):
        item = _mapping(item_raw, f"journal action {index}")
        _expect(
            set(item),
            {
                "sequence",
                "action",
                "handoff_id",
                "operation_id",
                "state_transition",
                "interfaces",
                "observation_sha256",
            },
            f"journal action {index} fields",
        )
        _expect(item.get("sequence"), index, f"journal action {index} sequence")
        action = expected_actions[index - 1]
        _expect(item.get("action"), action, f"journal action {index} identity")
        _digest(item.get("handoff_id"), f"journal action {index} handoff")
        _digest(item.get("observation_sha256"), f"journal action {index} observation")
        interfaces = item.get("interfaces")
        action_surfaces = _surfaces_for(action, scenario)
        if not isinstance(interfaces, list) or len(interfaces) != len(action_surfaces):
            raise AdapterError(f"journal action {index} has incomplete interface evidence")
        for surface, evidence in zip(action_surfaces, interfaces, strict=True):
            _validate_process_evidence(evidence, surface, f"journal action {index} {surface}")
        after = _phase_after(action, phase)
        _expect(
            item.get("state_transition"),
            {"from": phase, "to": after},
            f"journal action {index} transition",
        )
        if action == "button-update-lost-ack":
            operation_id = _text(item.get("operation_id"), "journal lost-ack operation ID")
        elif operation_id is None:
            _expect(item.get("operation_id"), None, f"journal action {index} operation ID")
        else:
            _expect(item.get("operation_id"), operation_id, f"journal action {index} operation ID")
        phase = after
    _expect(journal.get("phase"), phase, "journal phase")
    _expect(journal.get("operation_id"), operation_id, "journal operation identity")
    semantic = journal.get("semantic_data_sha256")
    if actions:
        _digest(semantic, "journal semantic-data identity")
    else:
        _expect(semantic, None, "unstarted journal semantic-data identity")
    _expect(journal.get("cleanup"), "pass" if phase == "terminal" else "pending", "cleanup")
    inflight_raw = journal.get("inflight")
    if inflight_raw is not None:
        if next_sequence > len(expected_actions):
            raise AdapterError("terminal journal cannot contain an in-flight handoff")
        inflight = _mapping(inflight_raw, "in-flight handoff")
        _expect(
            set(inflight),
            {
                "sequence",
                "action",
                "handoff_id",
                "current_surface",
                "completed_surfaces",
            },
            "in-flight handoff fields",
        )
        _expect(inflight.get("sequence"), next_sequence, "in-flight sequence")
        action = expected_actions[next_sequence - 1]
        _expect(inflight.get("action"), action, "in-flight action")
        _digest(inflight.get("handoff_id"), "in-flight handoff identity")
        surfaces = _surfaces_for(action, scenario)
        completed = inflight.get("completed_surfaces")
        if not isinstance(completed, list) or len(completed) > len(surfaces):
            raise AdapterError("in-flight completed surfaces are malformed")
        for index, result_raw in enumerate(completed):
            result = _mapping(result_raw, f"completed surface {index + 1}")
            _expect(set(result), {"surface", "observation", "evidence"}, "surface result fields")
            _expect(result.get("surface"), surfaces[index], "completed surface order")
            observation = _mapping(result.get("observation"), "completed surface observation")
            _validate_process_evidence(
                result.get("evidence"), surfaces[index], "completed surface evidence"
            )
            evidence = _mapping(result.get("evidence"), "completed surface evidence")
            _expect(
                evidence.get("observation_sha256"),
                _sha256_bytes(_canonical_bytes(observation)),
                "completed surface observation identity",
            )
            _validate_surface_observation(
                surface=surfaces[index],
                action=action,
                observation=observation,
                request={"scenario": scenario},
            )
        current = inflight.get("current_surface")
        if len(completed) == len(surfaces):
            _expect(current, None, "completed in-flight current surface")
        else:
            _expect(current, surfaces[len(completed)], "in-flight current surface")


def _surfaces_for(action: str, scenario: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    if action in {"inspect-release1", "inspect-release2"}:
        return ("installed",)
    if action == "button-unsaved-draft":
        return ("browser",)
    if action in {"button-update-lost-ack", "button-update-explicit"}:
        return ("browser", "update")
    if action in {"install-line-update", "pinokio-update"}:
        return ("update",)
    if action == "cleanup":
        if scenario is not None and scenario.get("channel") != "button":
            return ("update", "installed")
        return ("browser", "update", "installed")
    raise AdapterError(f"unsupported boundary action: {action!r}")


def _new_journal(request: Mapping[str, Any]) -> dict[str, Any]:
    _validate_request_envelope(request)
    scenario = _mapping(request.get("scenario"), "request scenario")
    actions = _expected_actions(scenario)
    _expect(request.get("sequence"), 1, "initial request sequence")
    _expect(request.get("action"), actions[0], "initial request action")
    return {
        "format": JOURNAL_FORMAT,
        "scenario": deepcopy(scenario),
        "inputs_sha256": _sha256_bytes(_canonical_bytes(request.get("inputs"))),
        "expected_actions": list(actions),
        "next_sequence": 1,
        "phase": "uninspected",
        "operation_id": None,
        "semantic_data_sha256": None,
        "inflight": None,
        "actions": [],
        "cleanup": "pending",
    }


def _validate_request(request: Mapping[str, Any], journal: Mapping[str, Any]) -> None:
    _validate_request_envelope(request)
    sequence = request.get("sequence")
    scenario = _mapping(request.get("scenario"), "request scenario")
    expected_scenario = journal.get("scenario")
    _expect(scenario, expected_scenario, "request scenario")
    _expect(
        _sha256_bytes(_canonical_bytes(request.get("inputs"))),
        journal.get("inputs_sha256"),
        "request inputs identity",
    )
    expected_actions = journal.get("expected_actions")
    if not isinstance(expected_actions, list) or sequence > len(expected_actions):
        raise AdapterError("request sequence is outside the admitted route")
    _expect(sequence, journal.get("next_sequence"), "request sequence")
    _expect(request.get("action"), expected_actions[sequence - 1], "request action")


def _interface_request(
    *,
    request: Mapping[str, Any],
    journal: Mapping[str, Any],
    surface: str,
    handoff_id: str,
    prior: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "protocol": INTERFACE_PROTOCOL,
        "handoff_id": handoff_id,
        "surface": surface,
        "action": request["action"],
        "request": deepcopy(dict(request)),
        "adapter_state": {
            "phase": journal["phase"],
            "operation_id": journal.get("operation_id"),
            "next_sequence": journal["next_sequence"],
        },
        "prior_observations": deepcopy(dict(prior)),
    }


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=PROCESS_TREE_KILL_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            process.kill()
        else:
            if completed.returncode and process.poll() is None:
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.communicate(timeout=PROCESS_PIPE_DRAIN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=PROCESS_FINAL_WAIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _invoke_interface(
    *,
    surface: str,
    command: Sequence[str],
    envelope: Mapping[str, Any],
    deadline: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _deadline_remaining(deadline, f"before invoking the {surface} interface")
    command_identity = _command_identity(command, deadline=deadline)
    encoded = _canonical_bytes(envelope).decode("utf-8") + "\n"
    process: subprocess.Popen[str] | None = None
    try:
        group_options: dict[str, Any]
        if os.name == "nt":
            group_options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            group_options = {"start_new_session": True}
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            **group_options,
        )
        stdout, stderr = process.communicate(
            encoded,
            timeout=_deadline_remaining(deadline, f"while invoking the {surface} interface"),
        )
    except AdapterError:
        if process is not None:
            _terminate_process_tree(process)
        raise
    except subprocess.TimeoutExpired as exc:
        assert process is not None
        _terminate_process_tree(process)
        raise AdapterError(f"{surface} interface timed out") from exc
    except OSError as exc:
        raise AdapterError(f"{surface} interface was unavailable: {exc}") from exc
    assert process is not None
    if process.returncode:
        detail = stderr.strip()
        raise AdapterError(
            f"{surface} interface exited {process.returncode}" + (f": {detail}" if detail else "")
        )
    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"{surface} interface returned invalid or ambiguous JSON") from exc
    response = _mapping(raw, f"{surface} interface response")
    _expect(response.get("protocol"), INTERFACE_PROTOCOL, f"{surface} response protocol")
    _expect(response.get("handoff_id"), envelope["handoff_id"], f"{surface} handoff identity")
    _expect(response.get("surface"), surface, f"{surface} response surface")
    _expect(response.get("action"), envelope["action"], f"{surface} response action")
    _expect(response.get("status"), "pass", f"{surface} response status")
    evidence = _mapping(response.get("evidence"), f"{surface} process evidence")
    _expect(evidence.get("kind"), f"{surface}-process", f"{surface} evidence kind")
    _expect(evidence.get("pid"), process.pid, f"{surface} evidence PID")
    _expect(evidence.get("synthetic"), False, f"{surface} synthetic flag")
    _expect(evidence.get("source_only"), False, f"{surface} source-only flag")
    observation = _mapping(response.get("observation"), f"{surface} observation")
    process_evidence = {
        "surface": surface,
        "pid": process.pid,
        "command": command_identity,
        "interface_evidence": evidence,
        "request_sha256": _sha256_bytes(_canonical_bytes(envelope)),
        "response_sha256": _sha256_bytes(_canonical_bytes(response)),
        "observation_sha256": _sha256_bytes(_canonical_bytes(observation)),
    }
    return observation, process_evidence


def _merge_observations(observations: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for surface, observation in observations.items():
        for name, value in observation.items():
            if name in merged and merged[name] != value:
                raise AdapterError(
                    f"ambiguous {name} observation differs across interfaces at {surface}"
                )
            merged[name] = deepcopy(value)
    return merged


def _validate_surface_observation(
    *,
    surface: str,
    action: str,
    observation: Mapping[str, Any],
    request: Mapping[str, Any],
) -> None:
    if action == "inspect-release1" and surface == "installed":
        fields = {
            "release",
            "candidate_checkouts",
            "semantic_data",
            "os_build",
        }
        if request["scenario"].get("mode") == "direct":
            fields.add("service_manager")
    elif action == "inspect-release2" and surface == "installed":
        fields = {
            "installed",
            "semantic_data",
            "service",
            "health",
            "task_settled",
            "one_runtime",
            "unrelated_sentinel",
        }
    elif action == "button-unsaved-draft" and surface == "browser":
        fields = {"surface", "save", "update_requests", "native_jobs", "installed_release"}
    elif action == "button-update-lost-ack" and surface in {"browser", "update"}:
        fields = {
            "states",
            "automatic_update_requests",
            "terminal_inferred",
            "operation_id",
        }
    elif action == "button-update-explicit" and surface == "browser":
        fields = {"states", "action", "operation_id"}
        if request["scenario"].get("os") == "linux":
            fields.add("draft_control")
    elif action == "button-update-explicit" and surface == "update":
        fields = {
            "states",
            "action",
            "reconciled_under_lock",
            "logical_acceptances",
            "new_operation_id",
            "result",
            "operation_id",
            "native_job",
        }
    elif action == "install-line-update" and surface == "update":
        fields = {
            "channel",
            "entrypoint",
            "update_flag",
            "manifest_sha256",
            "root_bound",
            "data_root_bound",
        }
    elif action == "pinokio-update" and surface == "update":
        fields = {
            "channel",
            "entrypoint",
            "managed_by",
            "mode",
            "route",
            "foreground_fallback",
            "action_sha256",
        }
    elif action == "cleanup" and surface in SURFACES:
        fields = {"cleanup"}
    else:
        raise AdapterError(f"{surface} does not own observations for {action}")
    if set(observation) != fields:
        missing = sorted(fields - set(observation))
        extra = sorted(set(observation) - fields)
        raise AdapterError(
            f"{surface} {action} observation ownership mismatch; "
            f"missing={missing!r}, extra={extra!r}"
        )


def _validate_candidate(observation: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    _expect(observation.get("release"), 1, "installed Release-1 version")
    inputs = _mapping(request["inputs"], "request inputs")
    candidate = _mapping(inputs.get("candidate"), "candidate identity")
    checkouts = _mapping(observation.get("candidate_checkouts"), "installed candidate checkouts")
    if set(checkouts) != set(candidate):
        raise AdapterError("installed candidate checkout set is incomplete or ambiguous")
    for name, expected in candidate.items():
        checkout = _mapping(checkouts.get(name), f"installed {name} checkout")
        _expect(set(checkout), {"sha"}, f"installed {name} checkout fields")
        _expect(checkout.get("sha"), expected, f"installed {name} candidate SHA")


def _validate_installed_release2(
    observation: Mapping[str, Any], request: Mapping[str, Any]
) -> None:
    installed = _mapping(observation.get("installed"), "installed Release-2 identity")
    inputs = _mapping(request["inputs"], "request inputs")
    stand_in = _mapping(inputs.get("stand_in"), "stand-in identity")
    target = _mapping(stand_in.get("target"), "stand-in target")
    expected = {
        "release": 2,
        "qualification_marker": inputs.get("stand_in_marker"),
        "manifest_sha256": stand_in.get("manifest_sha256"),
        "agent": target.get("agent"),
        "runtime": target.get("runtime"),
        "schema_digest": target.get("schema_digest"),
    }
    for name, value in expected.items():
        _expect(installed.get(name), value, f"installed Release-2 {name}")


def _validate_action_observation(
    observation: Mapping[str, Any],
    request: Mapping[str, Any],
    journal: dict[str, Any],
    evidence_kind: str,
) -> str:
    action = request["action"]
    phase = journal["phase"]
    payload = _mapping(request.get("payload"), "request payload")
    if action == "inspect-release1":
        _expect(phase, "uninspected", "adapter phase")
        _validate_candidate(observation, request)
        semantic = _mapping(observation.get("semantic_data"), "initial semantic data")
        semantic_digest = _digest(semantic.get("sha256"), "initial semantic data digest")
        os_name = request["scenario"]["os"]
        runner_os = {"macos": "macOS", "linux": "Linux", "windows": "Windows"}[os_name]
        os_build = _text(observation.get("os_build"), "installed OS build")
        if not os_build.startswith(runner_os + "-"):
            raise AdapterError("installed OS build does not match the admitted scenario")
        if evidence_kind == "installed-executable" and any(
            word in os_build.lower() for word in ("synthetic", "source", "disposable")
        ):
            raise AdapterError("installed OS build cannot be synthetic, source-only, or disposable")
        if request["scenario"]["mode"] == "direct":
            service = _mapping(observation.get("service_manager"), "native service manager")
            manager = {
                "macos": "launchd",
                "linux": "systemd-user",
                "windows": "task-scheduler",
            }[os_name]
            _expect(service.get("manager"), manager, "native service manager")
            _text(service.get("version"), "native service-manager version")
            _expect(service.get("foreground_fallback"), False, "foreground fallback")
        journal["semantic_data_sha256"] = semantic_digest
        return "release1"
    if action == "button-unsaved-draft":
        _expect(phase, "release1", "adapter phase")
        expected = {
            "surface": "browser",
            "save": "failed",
            "update_requests": 0,
            "native_jobs": 0,
            "installed_release": 1,
        }
        for name, value in expected.items():
            _expect(observation.get(name), value, f"unsaved-draft {name}")
        return "release1"
    if action == "button-update-lost-ack":
        _expect(phase, "release1", "adapter phase")
        _expect(payload.get("request"), "explicit-update", "lost-ack request")
        _expect(payload.get("simulate_lost_ack"), True, "lost-ack control")
        _expect(observation.get("states"), ["active", "recovery-needed"], "lost-ack states")
        _expect(observation.get("automatic_update_requests"), 0, "automatic update requests")
        _expect(observation.get("terminal_inferred"), False, "lost-ack terminal inference")
        operation_id = _text(observation.get("operation_id"), "lost-ack operation ID")
        if journal.get("operation_id") not in {None, operation_id}:
            raise AdapterError("lost-ack operation identity changed")
        journal["operation_id"] = operation_id
        return "recovery-needed"
    if action == "button-update-explicit":
        _expect(phase, "recovery-needed", "adapter phase")
        operation_id = _text(journal.get("operation_id"), "journal operation ID")
        _expect(payload.get("operation_id"), operation_id, "explicit Update operation ID")
        _expect(observation.get("operation_id"), operation_id, "recovered operation ID")
        _expect(
            observation.get("states"),
            ["recovery-needed", "active", "terminal"],
            "explicit recovery states",
        )
        _expect(observation.get("action"), "explicit-update", "explicit recovery action")
        _expect(observation.get("reconciled_under_lock"), True, "locked reconciliation")
        _expect(observation.get("logical_acceptances"), 1, "logical acceptances")
        _expect(observation.get("new_operation_id"), False, "new operation identity")
        _expect(observation.get("result"), "updated", "explicit recovery result")
        native_job = _mapping(observation.get("native_job"), "native update job")
        expected_native = {
            "format": "astrid-update-platform/v1",
            "system": {
                "macos": "darwin",
                "linux": "linux",
                "windows": "windows",
            }[request["scenario"]["os"]],
            "one_shot": True,
            "owned": True,
            "acknowledged": True,
            "operation_id": operation_id,
        }
        for name, value in expected_native.items():
            _expect(native_job.get(name), value, f"native job {name}")
        if request["scenario"].get("os") == "linux":
            draft = _mapping(observation.get("draft_control"), "saved-draft observation")
            expected = {
                "surface": "browser",
                "save": "pass",
                "update_requests": 1,
                "native_jobs": 1,
                "installed_release": 2,
            }
            for name, value in expected.items():
                _expect(draft.get(name), value, f"saved-draft {name}")
        return "release2"
    if action == "install-line-update":
        _expect(phase, "release1", "adapter phase")
        expected = {
            "channel": "install-line",
            "entrypoint": "public-bootstrap",
            **payload,
        }
        for name, value in expected.items():
            _expect(observation.get(name), value, f"install-line {name}")
        return "release2"
    if action == "pinokio-update":
        _expect(phase, "release1", "adapter phase")
        expected = {
            "channel": "pinokio-update",
            "entrypoint": "pinokio-update",
            "managed_by": "pinokio",
            **payload,
        }
        for name, value in expected.items():
            _expect(observation.get(name), value, f"Pinokio update {name}")
        _digest(observation.get("action_sha256"), "Pinokio action provenance")
        return "release2"
    if action == "inspect-release2":
        _expect(phase, "release2", "adapter phase")
        _validate_installed_release2(observation, request)
        semantic = _mapping(observation.get("semantic_data"), "final semantic data")
        _expect(
            _digest(semantic.get("sha256"), "final semantic data digest"),
            journal.get("semantic_data_sha256"),
            "preserved semantic data digest",
        )
        for name in ("service", "health", "task_settled", "one_runtime", "unrelated_sentinel"):
            _expect(observation.get(name), "pass", f"final {name} check")
        return "release2-inspected"
    if action == "cleanup":
        _expect(phase, "release2-inspected", "adapter phase")
        _expect(observation, {"cleanup": "pass"}, "combined cleanup observation")
        journal["cleanup"] = "pass"
        return "terminal"
    raise AdapterError(f"unsupported boundary action: {action!r}")


def _handoff_id(request: Mapping[str, Any]) -> str:
    identity = {
        "sequence": request.get("sequence"),
        "action": request.get("action"),
        "scenario": request.get("scenario"),
        "inputs": request.get("inputs"),
        "payload": request.get("payload"),
    }
    return _sha256_bytes(_canonical_bytes(identity))


def _execute(
    *,
    request: dict[str, Any],
    commands: Mapping[str, Sequence[str]],
    evidence_kind: str,
    timeout: float,
    journal_name: str,
    command_provenance: Mapping[str, Any] | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    if not math.isfinite(timeout) or timeout <= 0 or timeout > DEFAULT_ACTION_TIMEOUT_SECONDS:
        raise AdapterError(
            f"timeout must be positive and at most {DEFAULT_ACTION_TIMEOUT_SECONDS:g} seconds"
        )
    now = time.monotonic()
    if deadline is None:
        action_deadline = now + timeout
    else:
        if not math.isfinite(deadline) or deadline > now + timeout + 0.001:
            raise AdapterError("absolute action deadline is invalid for the timeout budget")
        action_deadline = deadline
    _deadline_remaining(action_deadline, "before request validation")
    _validate_request_envelope(request)
    _validate_evidence_mode(evidence_kind, request, command_provenance)
    data_root = _safe_data_root(request)
    if Path(journal_name).name != journal_name or journal_name in {".", ".."}:
        raise AdapterError("journal name must be one safe file name")
    journal_path = data_root / journal_name
    lock_path = data_root / f".{journal_name}.lock"
    _deadline_remaining(action_deadline, "after request validation")
    with _journal_lock(lock_path, action_deadline):
        _deadline_remaining(action_deadline, "before reading the adapter journal")
        journal = _read_journal(journal_path)
        _deadline_remaining(action_deadline, "after reading the adapter journal")
        if journal is None:
            journal = _new_journal(request)
        _validate_request(request, journal)
        _deadline_remaining(action_deadline, "after validating the adapter journal")
        action = str(request["action"])
        surfaces = _surfaces_for(action, journal["scenario"])
        handoff_id = _handoff_id(request)
        inflight = journal.get("inflight")
        if inflight is not None:
            detail = _mapping(inflight, "in-flight handoff")
            _expect(detail.get("sequence"), request["sequence"], "in-flight sequence")
            _expect(detail.get("action"), action, "in-flight action")
            _expect(detail.get("handoff_id"), handoff_id, "in-flight handoff identity")
            raise AdapterError(
                "in-flight handoff has no product reconciliation proof; refusing to replay"
            )
        journal["inflight"] = {
            "sequence": request["sequence"],
            "action": action,
            "handoff_id": handoff_id,
            "current_surface": surfaces[0],
            "completed_surfaces": [],
        }
        _deadline_remaining(action_deadline, "before reserving the in-flight handoff")
        _write_json(journal_path, journal)
        _deadline_remaining(action_deadline, "after reserving the in-flight handoff")
        prior: dict[str, dict[str, Any]] = {}
        interface_evidence: list[dict[str, Any]] = []
        for index, surface in enumerate(surfaces):
            if evidence_kind == "installed-executable":
                provenance = _mapping(command_provenance, "trusted command provenance")
                _reverify_trusted_command(
                    surface,
                    commands[surface],
                    provenance.get(surface),
                    action_deadline,
                )
            envelope = _interface_request(
                request=request,
                journal=journal,
                surface=surface,
                handoff_id=handoff_id,
                prior=prior,
            )
            observation, evidence = _invoke_interface(
                surface=surface,
                command=commands[surface],
                envelope=envelope,
                deadline=action_deadline,
            )
            inflight_state = _mapping(journal["inflight"], "in-flight handoff")
            _expect(inflight_state.get("current_surface"), surface, "reserved interface surface")
            completed = inflight_state.get("completed_surfaces")
            if not isinstance(completed, list):
                raise AdapterError("in-flight completed surfaces must be a list")
            completed.append(
                {
                    "surface": surface,
                    "observation": deepcopy(observation),
                    "evidence": deepcopy(evidence),
                }
            )
            inflight_state["current_surface"] = (
                surfaces[index + 1] if index + 1 < len(surfaces) else None
            )
            journal["inflight"] = inflight_state
            # This durability write records a side effect which already happened.  It must
            # complete even if response parsing consumed the final fraction of the budget.
            _write_json(journal_path, journal)
            _deadline_remaining(action_deadline, f"after recording the {surface} interface result")
            _validate_surface_observation(
                surface=surface,
                action=action,
                observation=observation,
                request=request,
            )
            prior[surface] = observation
            interface_evidence.append(evidence)
        _deadline_remaining(action_deadline, "before validating the combined observation")
        observation = _merge_observations(prior)
        before_phase = str(journal["phase"])
        after_phase = _validate_action_observation(observation, request, journal, evidence_kind)
        _deadline_remaining(action_deadline, "after validating the combined observation")
        journal["phase"] = after_phase
        journal["next_sequence"] = int(request["sequence"]) + 1
        journal["actions"].append(
            {
                "sequence": request["sequence"],
                "action": action,
                "handoff_id": handoff_id,
                "operation_id": journal.get("operation_id"),
                "state_transition": {"from": before_phase, "to": after_phase},
                "interfaces": deepcopy(interface_evidence),
                "observation_sha256": _sha256_bytes(_canonical_bytes(observation)),
            }
        )
        journal["inflight"] = None
        _deadline_remaining(action_deadline, "before committing the action journal")
        _write_json(journal_path, journal)
        _deadline_remaining(action_deadline, "after committing the action journal")
    _deadline_remaining(action_deadline, "after releasing the adapter journal lock")
    return {
        "protocol": BOUNDARY_PROTOCOL,
        "sequence": request["sequence"],
        "action": action,
        "status": "pass",
        "evidence": {
            "kind": evidence_kind,
            "synthetic": False,
            "source_only": False,
            "adapter_protocol": INTERFACE_PROTOCOL,
            "handoff_id": handoff_id,
            "state_transition": {"from": before_phase, "to": after_phase},
            "operation_id": journal.get("operation_id"),
            "interfaces": interface_evidence,
            "command_provenance": deepcopy(dict(command_provenance or {})),
            "journal": str(journal_path),
        },
        "observation": observation,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", required=True, help="installed-product interface executable")
    parser.add_argument(
        "--installed-arg", action="append", default=[], help="one installed argv value"
    )
    parser.add_argument(
        "--installed-artifact", action="append", default=[], help="installed argv file to hash"
    )
    parser.add_argument(
        "--installed-mutable-arg-index",
        action="append",
        default=[],
        type=int,
        help="registered installed argv index for mutable/output data",
    )
    parser.add_argument(
        "--trusted-installed-identity",
        action="append",
        default=[],
        help="registered installed command/artifact identity",
    )
    parser.add_argument("--browser", required=True, help="browser interface executable")
    parser.add_argument("--browser-arg", action="append", default=[], help="one browser argv value")
    parser.add_argument(
        "--browser-artifact", action="append", default=[], help="browser argv file to hash"
    )
    parser.add_argument(
        "--browser-mutable-arg-index",
        action="append",
        default=[],
        type=int,
        help="registered browser argv index for mutable/output data",
    )
    parser.add_argument(
        "--trusted-browser-identity",
        action="append",
        default=[],
        help="registered browser command/artifact identity",
    )
    parser.add_argument("--update", required=True, help="update interface executable")
    parser.add_argument("--update-arg", action="append", default=[], help="one update argv value")
    parser.add_argument(
        "--update-artifact", action="append", default=[], help="update argv file to hash"
    )
    parser.add_argument(
        "--update-mutable-arg-index",
        action="append",
        default=[],
        type=int,
        help="registered update argv index for mutable/output data",
    )
    parser.add_argument(
        "--trusted-update-identity",
        action="append",
        default=[],
        help="registered update command/artifact identity",
    )
    parser.add_argument("--evidence-kind", required=True, choices=EVIDENCE_KINDS)
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_ACTION_TIMEOUT_SECONDS,
        help="whole-action budget; must remain below the outer executor timeout",
    )
    parser.add_argument("--journal-name", default="installed-browser-adapter.json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if (
            not math.isfinite(args.timeout)
            or args.timeout <= 0
            or args.timeout > DEFAULT_ACTION_TIMEOUT_SECONDS
        ):
            raise AdapterError(
                f"timeout must be positive and at most {DEFAULT_ACTION_TIMEOUT_SECONDS:g} seconds"
            )
        action_deadline = time.monotonic() + args.timeout
        _deadline_remaining(action_deadline, "before resolving interface commands")
        commands = {
            "installed": _resolve_command(args.installed, args.installed_arg, "installed"),
            "browser": _resolve_command(args.browser, args.browser_arg, "browser"),
            "update": _resolve_command(args.update, args.update_arg, "update"),
        }
        identities = {_sha256_bytes(_canonical_bytes(list(value))) for value in commands.values()}
        if len(identities) != len(SURFACES):
            raise AdapterError(
                "installed, browser, and update interfaces require distinct argv vectors"
            )
        require_trust = args.evidence_kind == "installed-executable"
        command_provenance = {
            surface: _command_provenance(
                surface=surface,
                command=commands[surface],
                artifact_paths=getattr(args, f"{surface}_artifact"),
                mutable_arg_indices=getattr(args, f"{surface}_mutable_arg_index"),
                trusted_identities=getattr(args, f"trusted_{surface}_identity"),
                require_trust=require_trust,
                deadline=action_deadline,
            )
            for surface in SURFACES
        }
        _deadline_remaining(action_deadline, "after hashing interface provenance")
        request = _read_request_before(action_deadline)
        response = _execute(
            request=request,
            commands=commands,
            evidence_kind=args.evidence_kind,
            timeout=args.timeout,
            journal_name=args.journal_name,
            command_provenance=command_provenance,
            deadline=action_deadline,
        )
    except (AdapterError, OSError, subprocess.SubprocessError) as exc:
        print(f"installed_browser_adapter: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(response, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
