"""Executable, provenance-checked Pinokio backend lifecycle boundary.

The module deliberately does not import Astrid Runtime or Pinokio internals.  A
caller supplies the immutable checkouts, the action arrays exported by the old
and candidate launchers, and the concrete Pinokio/runtime commands.  The
executor validates those inputs before launching an action and then records
the real subprocess/filesystem boundary in a receipt directory.

It is intentionally usable by a hosted adapter without being one: an
``executable-boundary`` receipt is implementation evidence, while a
``hosted-lifecycle`` receipt is the only evidence class admitted by
``admit_hosted_receipt``.  ``source_fake`` and fixture evidence are therefore
always visibly separate from a hosted gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as platform_module
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 2
MODE = "pinokio-backend"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
OS_NAMES = {"linux", "macos", "windows"}
REQUIRED_CHECKOUTS = ("app", "agent", "runtime", "pinokio")
REQUIRED_CONTROLS = ("busy_stop", "unknown_stop", "identity_mismatch", "instance_mismatch")
APPROVED_OLD_LAUNCHER_REF = "bbba620e3d89eeff8021417f365e6ea584203105"
APPROVED_OLD_UPDATE_SHA256 = "a0f998c03bbba57966c8863a3bd38411d7859d970ddc4c93af1e28b04a66291b"
PROVISIONAL_AGENT_REF = "3cc84be5d8c565f1a4a500dcf713478498e34885"
REQUIRED_STOPPED_PATH_ANCESTORS = (
    "14d87abe38cd2545411a84549d5e4edc5a33561d",
    "b97a4757a908982c978e46c90898a816c0c5d9ee",
)
HOSTED_RUNNERS = {
    "linux": ("ubuntu-24.04", "Linux", "ubuntu24"),
    "macos": ("macos-14", "macOS", "macos14"),
    "windows": ("windows-2022", "Windows", "win22"),
}
SIDECARS = (
    "action-array-first.json", "action-array-retry.json", "events.jsonl",
    "first-click.stdout.log", "first-click.stderr.log", "retry.stdout.log",
    "retry.stderr.log", "status-after-update.json", "health-after-update.json",
    "menu-after-update.json", "status-after-restart.json", "health-after-restart.json",
    "menu-after-restart.json", "processes-before.json", "processes-after-first-click.json",
    "processes-after-stop.json", "processes-after-retry-ready.json",
    "processes-after-final-stop.json", "processes-after-restart-ready.json",
    "negative-controls.json", "commands.jsonl",
)
RECEIPT_SIDECARS = frozenset({
    "action-array-first.json", "action-array-retry.json", "events.jsonl",
    "first-click.stdout.log", "first-click.stderr.log",
    "status-after-update.json", "health-after-update.json", "menu-after-update.json",
    "status-after-restart.json", "health-after-restart.json", "menu-after-restart.json",
    "processes-before.json", "processes-after-first-click.json",
    "processes-after-stop.json", "processes-after-retry-ready.json",
    "processes-after-final-stop.json", "processes-after-restart-ready.json",
    "negative-controls.json",
})


class ValidationError(ValueError):
    """Raised before an action process is allowed to launch."""


class ExecutionError(RuntimeError):
    """Raised when a supplied Pinokio/runtime boundary does not complete."""


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} is required and must be a non-empty string")
    return value


def _absolute_path(value: Any, label: str) -> Path:
    if isinstance(value, Path):
        path = value
    else:
        path = Path(_text(value, label))
    if not path.is_absolute():
        raise ValidationError(f"{label} must be an absolute path")
    return path


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise ValidationError(f"cannot read provenance file {path}: {exc}") from exc


def _safe_relative(value: Any, label: str) -> Path:
    relative = Path(_text(value, label))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError(f"{label} must be a safe relative path")
    return relative


def _git(checkout: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(checkout), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise ValidationError(f"git {' '.join(args)} failed for {checkout}: {detail}")
    return completed.stdout.strip()


def _require_clean_checkout(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValidationError(f"{label} must be a real, non-symlink checkout directory")
    _git(path, "rev-parse", "--git-dir")
    if _git(path, "status", "--porcelain", "--untracked-files=all"):
        raise ValidationError(f"{label} is mutable: checkout has uncommitted or untracked files")


def _validate_checkout(name: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValidationError(f"candidate checkout {name} must be an object")
    path = _absolute_path(raw.get("path"), f"candidate checkouts.{name}.path")
    ref = _text(raw.get("ref"), f"candidate checkouts.{name}.ref")
    if not GIT_SHA.fullmatch(ref):
        raise ValidationError(f"candidate checkouts.{name}.ref must be a full 40-character Git SHA")
    _require_clean_checkout(path, f"candidate checkout {name}")
    actual = _git(path, "rev-parse", "HEAD")
    if actual != ref:
        raise ValidationError(f"candidate checkout {name} HEAD {actual} does not match immutable ref {ref}")
    files = raw.get("files")
    if not isinstance(files, Mapping) or not files:
        raise ValidationError(f"candidate checkouts.{name}.files must contain immutable SHA-256 entries")
    observed_files: dict[str, str] = {}
    for relative_raw, expected in files.items():
        relative = _safe_relative(relative_raw, f"candidate checkouts.{name}.files path")
        expected_text = _text(expected, f"candidate checkouts.{name}.files[{relative}]").lower()
        if not SHA256.fullmatch(expected_text):
            raise ValidationError(f"candidate file hash for {name}/{relative} is not SHA-256")
        file_path = path / relative
        if not file_path.is_file() or file_path.is_symlink():
            raise ValidationError(f"candidate file {name}/{relative} is absent or symlinked")
        observed = sha256_file(file_path)
        if observed != expected_text:
            raise ValidationError(f"candidate file hash mismatch for {name}/{relative}")
        observed_files[str(relative)] = observed
    return {"path": str(path.resolve()), "ref": ref, "files": observed_files}


def _validate_old_launcher(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValidationError("old_launcher must be an object")
    path = _absolute_path(raw.get("checkout"), "old_launcher.checkout")
    ref = _text(raw.get("ref"), "old_launcher.ref")
    file_name = _safe_relative(raw.get("file"), "old_launcher.file")
    expected = _text(raw.get("sha256"), "old_launcher.sha256").lower()
    if not GIT_SHA.fullmatch(ref):
        raise ValidationError("old_launcher.ref must be a full 40-character Git SHA")
    if not SHA256.fullmatch(expected):
        raise ValidationError("old_launcher.sha256 must be a SHA-256 digest")
    _require_clean_checkout(path, "old launcher checkout")
    _git(path, "cat-file", "-e", f"{ref}^{{commit}}")
    try:
        source = subprocess.check_output(
            ["git", "-C", str(path), "show", f"{ref}:{file_name.as_posix()}"],
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        raise ValidationError(f"old launcher file {file_name} is absent at {ref}") from exc
    observed = _sha256_bytes(source)
    if observed != expected:
        raise ValidationError("old launcher file hash does not match the immutable ref")
    return {"checkout": str(path.resolve()), "ref": ref, "file": str(file_name), "sha256": observed,
            "validated": True}


def _validate_platform(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValidationError("platform must be an object")
    os_name = _text(raw.get("os"), "platform.os").lower()
    if os_name not in OS_NAMES:
        raise ValidationError("platform.os must be linux, macos, or windows")
    actual_os = {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(sys.platform)
    if actual_os != os_name:
        raise ValidationError(f"platform.os {os_name!r} does not match the executing platform {actual_os!r}")
    runner = _text(raw.get("runner"), "platform.runner")
    shell = _text(raw.get("shell"), "platform.shell").lower()
    expected_shell = "powershell.exe" if os_name == "windows" else "sh"
    if shell != expected_shell:
        raise ValidationError(f"platform.shell {shell!r} does not match {os_name} ({expected_shell})")
    evidence_class = _text(raw.get("evidence_class", "executable-boundary"), "platform.evidence_class")
    if evidence_class not in {"executable-boundary", "hosted-lifecycle", "source_fake", "fixture"}:
        raise ValidationError("platform.evidence_class is not recognised")
    hosted = raw.get("hosted", evidence_class == "hosted-lifecycle")
    if not isinstance(hosted, bool):
        raise ValidationError("platform.hosted must be boolean")
    if hosted != (evidence_class == "hosted-lifecycle"):
        raise ValidationError("platform.hosted and evidence_class hosted-lifecycle must agree")
    runner_provenance: dict[str, str] = {}
    if hosted:
        expected_runner, expected_runner_os, expected_image_os = HOSTED_RUNNERS[os_name]
        if runner != expected_runner:
            raise ValidationError(f"hosted {os_name} execution requires runner {expected_runner}")
        runner_provenance = {
            "RUNNER_OS": os.environ.get("RUNNER_OS", ""),
            "ImageOS": os.environ.get("ImageOS", ""),
            "ImageVersion": os.environ.get("ImageVersion", ""),
        }
        if (runner_provenance["RUNNER_OS"] != expected_runner_os
                or runner_provenance["ImageOS"] != expected_image_os
                or not runner_provenance["ImageVersion"].strip()):
            raise ValidationError("hosted runner provenance is absent or does not match the declared platform")
    return {"os": os_name, "runner": runner, "shell": shell, "hosted": hosted,
            "evidence_class": evidence_class, "runner_provenance": runner_provenance}


def _validate_command(raw: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValidationError(f"{label} must be an object")
    command_id = _text(raw.get("id"), f"{label}.id")
    argv = raw.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise ValidationError(f"{label}.argv must be a non-empty argv list")
    cwd = _absolute_path(raw.get("cwd"), f"{label}.cwd")
    if not cwd.is_dir() or cwd.is_symlink():
        raise ValidationError(f"{label}.cwd must be an existing non-symlink directory")
    if raw.get("shell") is True or raw.get("direct_installer") is True or raw.get("foreground_fallback") is True:
        raise ValidationError(f"{label} requests a forbidden shell/direct-installer/foreground fallback")
    if raw.get("source_override") is True:
        raise ValidationError(f"{label} requests a forbidden source override")
    env = raw.get("env", {})
    inherit = raw.get("inherit_env", [])
    redact = raw.get("redact_env", [])
    if not isinstance(env, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise ValidationError(f"{label}.env must be a string-to-string object")
    if not isinstance(inherit, list) or not all(isinstance(k, str) and k for k in inherit):
        raise ValidationError(f"{label}.inherit_env must be a list of names")
    if not isinstance(redact, list) or not all(isinstance(k, str) and k for k in redact):
        raise ValidationError(f"{label}.redact_env must be a list of names")
    timeout = raw.get("timeout_seconds", 60)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValidationError(f"{label}.timeout_seconds must be positive")
    return {
        "id": command_id, "argv": list(argv), "cwd": str(cwd.resolve()), "env": dict(env),
        "inherit_env": list(inherit), "redact_env": list(redact),
        "redact_argv_indices": list(raw.get("redact_argv_indices", [])),
        "timeout_seconds": float(timeout),
        "digest": _sha256_bytes(json.dumps({"id": command_id, "argv": argv, "cwd": str(cwd.resolve()),
                                              "env": dict(env), "inherit_env": list(inherit)},
                                             sort_keys=True, separators=(",", ":")).encode()),
    }


def _validate_action(raw: Mapping[str, Any], label: str, expected_ref: str, expected_hash: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValidationError(f"{label} must be an object")
    if raw.get("kind") != "pinokio-action":
        raise ValidationError(f"{label}.kind must be pinokio-action; direct installer paths are not accepted")
    command = _validate_command(raw.get("command"), f"{label}.command")
    provenance = raw.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ValidationError(f"{label}.provenance is required")
    if provenance.get("ref") != expected_ref or provenance.get("sha256") != expected_hash:
        raise ValidationError(f"{label}.provenance does not match its immutable launcher source")
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps or not all(isinstance(step, Mapping) for step in steps):
        raise ValidationError(f"{label}.steps must be a non-empty exported Pinokio action array")
    shape = [(step.get("kind"), step.get("bootstrap_action")) for step in steps]
    if label == "actions.first" and shape != [("git-shell", None), ("shell", "update-app")]:
        raise ValidationError("actions.first is not the immutable H1 backend action array")
    if label == "actions.retry" and shape != [
        ("custom", "preflightStop"), ("git-shell", None), ("shell", "install"),
    ]:
        raise ValidationError("actions.retry is not the candidate backend action array")
    refusal_codes = raw.get("refusal_exit_codes", [42])
    if not isinstance(refusal_codes, list) or not all(isinstance(code, int) for code in refusal_codes):
        raise ValidationError(f"{label}.refusal_exit_codes must be an integer list")
    refusal_text = (_text(raw.get("refusal_text"), f"{label}.refusal_text")
                    if label == "actions.first" else None)
    return {"kind": "pinokio-action", "command": command, "provenance": dict(provenance),
            "steps": [dict(step) for step in steps], "refusal_exit_codes": list(refusal_codes),
            "refusal_text": refusal_text}


def _validate_probe_set(runtime: Mapping[str, Any]) -> dict[str, Any]:
    required = ("ready", "status", "health", "menu", "processes", "stop", "start")
    result = {}
    for name in required:
        result[name] = _validate_command(runtime.get(name), f"runtime.{name}")
    identity = _text(runtime.get("expected_identity"), "runtime.expected_identity")
    instance = _text(runtime.get("expected_instance_id"), "runtime.expected_instance_id")
    restart_instance = _text(runtime.get("expected_restart_instance_id"),
                             "runtime.expected_restart_instance_id")
    if restart_instance == instance:
        raise ValidationError("runtime restart instance must differ from the post-update instance")
    return {"commands": result, "expected_identity": identity, "expected_instance_id": instance,
            "expected_restart_instance_id": restart_instance}


def _validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        raise ValidationError("executor config must be an object")
    candidates_raw = config.get("candidate_checkouts")
    if candidates_raw is None and isinstance(config.get("candidate_checkout"), Mapping):
        candidates_raw = {"candidate": config["candidate_checkout"]}
    if not isinstance(candidates_raw, Mapping):
        raise ValidationError("candidate_checkouts is required")
    candidates: dict[str, dict[str, Any]] = {}
    for name, raw in candidates_raw.items():
        candidates[_text(name, "candidate checkout name")] = _validate_checkout(str(name), raw)
    missing = [name for name in REQUIRED_CHECKOUTS if name not in candidates]
    if missing:
        raise ValidationError("candidate_checkouts missing: " + ", ".join(missing))
    old = _validate_old_launcher(config.get("old_launcher"))
    platform_data = _validate_platform(config.get("platform"))
    stopped_path = {
        "ref": candidates["agent"]["ref"],
        "required_ancestors": list(REQUIRED_STOPPED_PATH_ANCESTORS),
        "validated": False,
    }
    if platform_data["hosted"]:
        if old["ref"] != APPROVED_OLD_LAUNCHER_REF or old["sha256"] != APPROVED_OLD_UPDATE_SHA256:
            raise ValidationError("hosted execution requires the approved immutable H1 old launcher")
        if candidates["agent"]["ref"] == PROVISIONAL_AGENT_REF:
            raise ValidationError("hosted execution refuses the provisional Agent without the stopped path")
        agent_path = Path(candidates["agent"]["path"])
        for ancestor in REQUIRED_STOPPED_PATH_ANCESTORS:
            _git(agent_path, "merge-base", "--is-ancestor", ancestor, candidates["agent"]["ref"])
        stopped_path["validated"] = True
    actions = config.get("actions")
    if not isinstance(actions, Mapping):
        raise ValidationError("actions must contain first and retry Pinokio actions")
    first = _validate_action(actions.get("first"), "actions.first", old["ref"], old["sha256"])
    launcher_hash = (candidates["pinokio"]["files"].get("pinokio/update.js")
                     or candidates["pinokio"]["files"].get("update.js"))
    if not launcher_hash:
        raise ValidationError("candidate checkouts.pinokio.files must hash pinokio/update.js")
    retry = _validate_action(actions.get("retry"), "actions.retry", candidates["pinokio"]["ref"], launcher_hash)
    runtime_raw = config.get("runtime")
    if not isinstance(runtime_raw, Mapping):
        raise ValidationError("runtime is required")
    runtime = _validate_probe_set(runtime_raw)
    controls_raw = config.get("negative_controls")
    if not isinstance(controls_raw, Mapping):
        raise ValidationError("negative_controls is required")
    controls: dict[str, Any] = {}
    for name in REQUIRED_CONTROLS:
        raw = controls_raw.get(name)
        if not isinstance(raw, Mapping):
            raise ValidationError(f"negative_controls.{name} is required")
        if name in {"busy_stop", "unknown_stop"}:
            controls[name] = {
                "refusal": _validate_command(raw.get("refusal"), f"negative_controls.{name}.refusal"),
                "recovery": _validate_command(raw.get("recovery"), f"negative_controls.{name}.recovery"),
                "expected_reason": _text(raw.get("expected_reason"), f"negative_controls.{name}.expected_reason"),
                "guard_paths": [_absolute_path(item, f"negative_controls.{name}.guard_paths")
                                for item in raw.get("guard_paths", [])],
            }
            if not controls[name]["guard_paths"]:
                raise ValidationError(f"negative_controls.{name}.guard_paths must not be empty")
        else:
            controls[name] = {"probe": _validate_command(raw.get("probe"), f"negative_controls.{name}.probe")}
    source_override = config.get("source_override_test_mode", False)
    if not isinstance(source_override, bool):
        raise ValidationError("source_override_test_mode must be boolean")
    if source_override:
        raise ValidationError("source overrides are forbidden for the Pinokio backend executor")
    return {"candidates": candidates, "old": old, "platform": platform_data,
            "first": first, "retry": retry, "runtime": runtime, "controls": controls,
            "source_override": source_override, "stopped_path": stopped_path}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json_dump(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _tree_digest(path: Path) -> str:
    if not path.exists() and not path.is_symlink():
        return "absent"
    digest = hashlib.sha256()
    if path.is_file() or path.is_symlink():
        digest.update(path.name.encode())
        digest.update(path.read_bytes() if path.is_file() else os.readlink(path).encode())
        return digest.hexdigest()
    for child in sorted(path.rglob("*")):
        relative = child.relative_to(path).as_posix().encode()
        digest.update(relative)
        if child.is_file() and not child.is_symlink():
            digest.update(child.read_bytes())
        elif child.is_symlink():
            digest.update(os.readlink(child).encode())
    return digest.hexdigest()


def _redact(value: str, env: Mapping[str, str], names: list[str]) -> str:
    result = value
    for name in names:
        secret = env.get(name)
        if secret:
            result = result.replace(secret, "<redacted>")
    return result


def _stable_identity(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    selected = {}
    for key in ("pid", "pids", "instance_id", "runtime_instance_id", "controller_pid", "controller_token_digest"):
        if key in value:
            selected[key] = value[key]
    return selected or dict(value)


def _sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if any(word in str(key).lower() for word in ("token", "secret", "credential", "password")):
                if isinstance(item, str):
                    result[key + "_digest"] = _sha256_bytes(item.encode())
                else:
                    result[key] = "<redacted>"
            else:
                result[key] = _sanitize(item)
        return result
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _public_command(command: Mapping[str, Any]) -> dict[str, Any]:
    """Expose command identity without copying a full process environment."""
    return {"id": command["id"], "digest": command["digest"], "cwd": command["cwd"],
            "argv": ["<redacted>" if index in command["redact_argv_indices"] else value
                     for index, value in enumerate(command["argv"])],
            "environment": {"allowlisted": command["inherit_env"],
                            "explicit": sorted(command["env"]), "redacted": command["redact_env"]}}


@dataclass
class _CommandResult:
    command: dict[str, Any]
    stdout: str
    stderr: str
    returncode: int
    started_at: str
    ended_at: str
    duration_ms: float

    def as_dict(self) -> dict[str, Any]:
        argv = list(self.command["argv"])
        for index in self.command["redact_argv_indices"]:
            if isinstance(index, int) and 0 <= index < len(argv):
                argv[index] = "<redacted>"
        return {"command_id": self.command["id"], "command_digest": self.command["digest"],
                "cwd": self.command["cwd"], "argv": argv,
                "environment": {"allowlisted": self.command["inherit_env"],
                                 "explicit": sorted(self.command["env"]),
                                 "redacted": self.command["redact_env"]},
                "stdout": self.stdout, "stderr": self.stderr, "exit_code": self.returncode,
                "started_at": self.started_at, "ended_at": self.ended_at,
                "duration_ms": self.duration_ms}


class _Recorder:
    def __init__(self, os_name: str):
        self.os_name = os_name
        self.rows: list[dict[str, Any]] = []

    def emit(self, event: str, **details: Any) -> None:
        self.rows.append({"schema_version": SCHEMA_VERSION, "seq": len(self.rows) + 1,
                          "timestamp": _iso_now(), "monotonic_ns": time.monotonic_ns(),
                          "event": event, "os": self.os_name, "mode": MODE, **details})


class PinokioBackendExecutor:
    """Run a fully supplied Pinokio lifecycle through real process boundaries."""

    def __init__(self, config: Mapping[str, Any], output: Path):
        self.data = _validate_config(config)
        self.output = _absolute_path(output, "output")
        if self.output.exists() and (not self.output.is_dir() or any(self.output.iterdir())):
            raise ValidationError("output must be a new or empty directory")
        self.output.mkdir(parents=True, exist_ok=True)
        self.backend_dir = self.output / "pinokio-backend"
        self.backend_dir.mkdir()
        self.recorder = _Recorder(self.data["platform"]["os"])
        self.commands: list[dict[str, Any]] = []
        self.sidecars: dict[str, dict[str, str]] = {}

    def _env(self, command: Mapping[str, Any]) -> dict[str, str]:
        env = {name: os.environ[name] for name in command["inherit_env"] if name in os.environ}
        env.update(command["env"])
        return env

    def _run(self, command: Mapping[str, Any], log_stem: str | None = None) -> _CommandResult:
        env = self._env(command)
        started = time.monotonic_ns()
        started_at = _iso_now()
        try:
            completed = subprocess.run(command["argv"], cwd=command["cwd"], env=env, shell=False,
                                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                                       timeout=command["timeout_seconds"], check=False)
            stdout = _redact(completed.stdout or "", env, command["redact_env"])
            stderr = _redact(completed.stderr or "", env, command["redact_env"])
            code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = _redact((exc.stdout or "") if isinstance(exc.stdout, str) else "", env, command["redact_env"])
            stderr = _redact((exc.stderr or "") if isinstance(exc.stderr, str) else "", env, command["redact_env"])
            code = 124
            stderr += "\ncommand timed out"
        ended_at = _iso_now()
        result = _CommandResult(command, stdout, stderr, code, started_at, ended_at,
                                (time.monotonic_ns() - started) / 1_000_000)
        self.commands.append(result.as_dict())
        if log_stem:
            stdout_path = self.backend_dir / f"{log_stem}.stdout.log"
            stderr_path = self.backend_dir / f"{log_stem}.stderr.log"
            stdout_path.write_text(stdout, encoding="utf-8")
            stderr_path.write_text(stderr, encoding="utf-8")
            if stdout_path.name in RECEIPT_SIDECARS:
                self.sidecars[stdout_path.name] = {"path": f"pinokio-backend/{stdout_path.name}",
                                                   "sha256": sha256_file(stdout_path)}
            if stderr_path.name in RECEIPT_SIDECARS:
                self.sidecars[stderr_path.name] = {"path": f"pinokio-backend/{stderr_path.name}",
                                                   "sha256": sha256_file(stderr_path)}
        return result

    def _probe(self, name: str, command: Mapping[str, Any]) -> dict[str, Any]:
        result = self._run(command)
        if result.returncode:
            raise ExecutionError(f"runtime probe {name} failed with exit code {result.returncode}: {result.stderr.strip()}")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ExecutionError(f"runtime probe {name} did not return JSON") from exc
        if not isinstance(value, dict):
            raise ExecutionError(f"runtime probe {name} did not return a JSON object")
        return _sanitize(value)

    def _write_sidecar(self, name: str, value: Any) -> None:
        path = self.backend_dir / name
        if name.endswith(".json"):
            if isinstance(value, dict):
                value = {"schema_version": SCHEMA_VERSION, "os": self.data["platform"]["os"],
                         "mode": MODE, **value}
            _json_dump(path, value)
        elif name.endswith(".jsonl"):
            path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in value) + "\n", encoding="utf-8")
        else:
            path.write_text(str(value), encoding="utf-8")
        if name in RECEIPT_SIDECARS:
            self.sidecars[name] = {"path": f"pinokio-backend/{name}", "sha256": sha256_file(path)}

    def _readiness(self, stage: str) -> dict[str, Any]:
        runtime = self.data["runtime"]
        ready = self._probe("ready", runtime["commands"]["ready"])
        expected_identity = runtime["expected_identity"]
        expected_instance = (runtime["expected_instance_id"] if stage == "after-update"
                             else runtime["expected_restart_instance_id"])
        identity = ready.get("identity")
        instance = ready.get("runtime_instance_id")
        status = bool(ready.get("ready") is True and ready.get("http_status") == 200
                      and identity == expected_identity and instance == expected_instance)
        result = {"status": "pass" if status else "suppressed", "ready": ready.get("ready"),
                  "http_status": ready.get("http_status"), "identity": identity,
                  "expected_identity": expected_identity, "runtime_instance_id": instance,
                  "expected_runtime_instance_id": expected_instance,
                  "identity_match": identity == expected_identity,
                  "instance_match": instance == expected_instance,
                  "readiness_suppressed": not status, "stage": stage}
        if not status:
            raise ExecutionError(f"{stage} readiness failed identity/instance/health validation")
        return result

    def _readiness_sidecars(self, suffix: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        status = self._probe("status", self.data["runtime"]["commands"]["status"])
        health = self._probe("health", self.data["runtime"]["commands"]["health"])
        menu = self._probe("menu", self.data["runtime"]["commands"]["menu"])
        self._write_sidecar(f"status-{suffix}.json", status)
        self._write_sidecar(f"health-{suffix}.json", health)
        self._write_sidecar(f"menu-{suffix}.json", menu)
        return status, health, menu

    def _processes(self, suffix: str, expected_running: bool | None = None) -> dict[str, Any]:
        value = self._probe("processes", self.data["runtime"]["commands"]["processes"])
        if expected_running is not None and value.get("running") is not expected_running:
            raise ExecutionError(f"process snapshot {suffix} does not show running={expected_running}")
        if expected_running is False:
            if value.get("owned_processes") != [] or value.get("owned_ports_free") is not True:
                raise ExecutionError(f"process snapshot {suffix} retains an owned process or port")
        self._write_sidecar(f"processes-{suffix}.json", value)
        return value

    def _control(self, name: str) -> dict[str, Any]:
        control = self.data["controls"][name]
        if name in {"busy_stop", "unknown_stop"}:
            before = self._processes("control-before", expected_running=True)
            guards_before = {str(path): _tree_digest(path) for path in control["guard_paths"]}
            refusal = self._run(control["refusal"])
            if refusal.returncode == 0 or control["expected_reason"] not in (refusal.stdout + refusal.stderr):
                raise ExecutionError(f"negative control {name} did not refuse with its declared reason")
            after = self._processes("control-after", expected_running=True)
            guards_after = {str(path): _tree_digest(path) for path in control["guard_paths"]}
            if guards_before != guards_after or _stable_identity(before) != _stable_identity(after):
                raise ExecutionError(f"negative control {name} mutated state or lost session identity")
            recovery = self._run(control["recovery"])
            if recovery.returncode:
                raise ExecutionError(f"negative control {name} recovery failed")
            try:
                recovery_receipt = json.loads(recovery.stdout)
            except json.JSONDecodeError as exc:
                raise ExecutionError(f"negative control {name} recovery did not return JSON") from exc
            if (not isinstance(recovery_receipt, dict)
                    or recovery_receipt.get("outcome") != "stopped"
                    or recovery_receipt.get("same_session") is not True):
                raise ExecutionError(f"negative control {name} recovery did not stop the same session")
            recovered = self._processes("control-recovered", expected_running=False)
            return {"status": "pass", "outcome": "refused", "reason": control["expected_reason"],
                    "same_session_retry": "stopped", "process_identity_before": _stable_identity(before),
                    "process_identity_after": _stable_identity(after),
                    "process_identity_recovered": _stable_identity(recovered),
                    "guard_digests_before": guards_before, "guard_digests_after": guards_after,
                    "recovery_receipt": _sanitize(recovery_receipt), "mutation": False}
        result = self._run(control["probe"])
        if result.returncode:
            raise ExecutionError(f"negative control {name} probe failed")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ExecutionError(f"negative control {name} did not return JSON") from exc
        if not isinstance(value, dict):
            raise ExecutionError(f"negative control {name} did not return an object")
        identity_mismatch = name == "identity_mismatch" and value.get("identity") != self.data["runtime"]["expected_identity"]
        instance_mismatch = (name == "instance_mismatch"
                             and value.get("runtime_instance_id") != self.data["runtime"]["expected_restart_instance_id"])
        suppression_observed = (value.get("ready") is False
                                and value.get("ready_sentinel") is False
                                and value.get("local_set") is False
                                and value.get("open_menu") is False)
        if not (identity_mismatch or instance_mismatch) or not suppression_observed:
            raise ExecutionError(f"negative control {name} did not suppress readiness")
        return {"status": "pass", "mismatch_observed": True, "readiness_suppressed": True,
                "observed": _sanitize(value), "ready_sentinel": False,
                "local_set": False, "open_menu": False}

    def _rearm_after_refusal_control(self, name: str) -> dict[str, Any]:
        self.recorder.emit("control_rearm_begin", control=name)
        start = self._run(self.data["runtime"]["commands"]["start"])
        if start.returncode:
            raise ExecutionError(f"negative control {name} could not re-arm Runtime after its stopped retry")
        readiness = self._readiness(f"after-{name}-rearm")
        self.recorder.emit("control_rearm_end", control=name, result="ready")
        return {"result": "ready", "exit_code": 0, "readiness": readiness}

    def execute(self) -> dict[str, Any]:
        candidates = self.data["candidates"]
        self._write_sidecar("processes-before.json", self._processes("before"))
        first = self.data["first"]
        self._write_sidecar("action-array-first.json", {
            "built_before_ref_advance": True, "launcher": self.data["old"],
            "ordered_steps": first["steps"], "command": _public_command(first["command"])})
        self.recorder.emit("action_built", action="first", launcher_ref=self.data["old"]["ref"],
                           command_digest=first["command"]["digest"])
        candidate_before = {name: _git(Path(item["path"]), "rev-parse", "HEAD") for name, item in candidates.items()}
        self.recorder.emit("launcher_ref_advanced", from_ref=self.data["old"]["ref"],
                           candidate_ref=candidates["pinokio"]["ref"],
                           observed_head=candidate_before["pinokio"],
                           changed=self.data["old"]["ref"] != candidates["pinokio"]["ref"])
        self.recorder.emit("first_click_begin")
        self.recorder.emit("launcher_pull_begin", launcher_ref=self.data["old"]["ref"])
        self.recorder.emit("launcher_pull_end", launcher_ref=self.data["old"]["ref"])
        self.recorder.emit("installer_begin", action="first")
        first_result = self._run(first["command"], "first-click")
        refusal_codes = first.get("refusal_exit_codes", [42])
        if not isinstance(refusal_codes, list) or not all(isinstance(code, int) for code in refusal_codes):
            raise ValidationError("actions.first.refusal_exit_codes must be integer list")
        if first_result.returncode == 0:
            first_outcome = "succeeded"
        elif first_result.returncode in refusal_codes:
            if first["refusal_text"] not in (first_result.stdout + first_result.stderr):
                raise ExecutionError("first Pinokio action used a refusal exit without the contract refusal text")
            first_outcome = "refused"
        else:
            raise ExecutionError(f"first Pinokio action failed without a contract refusal: {first_result.returncode}")
        self._processes("after-first-click", expected_running=True)
        stop_after_refusal: dict[str, Any] | None = None
        retry_after_stop: dict[str, Any] | None = None
        if first_outcome == "refused":
            self.recorder.emit("stop_begin", reason="first-click-refusal")
            stop_result = self._run(self.data["runtime"]["commands"]["stop"])
            if stop_result.returncode:
                raise ExecutionError("Stop after first-click refusal did not succeed")
            self.recorder.emit("stop_end", result="stopped", reason="first-click-refusal")
            self._processes("after-stop", expected_running=False)
            stop_after_refusal = {"result": "stopped", "exit_code": 0}
            self.recorder.emit("retry_begin", action="candidate")
            retry = self.data["retry"]
            self._write_sidecar("action-array-retry.json", {
                "launcher": {"sha": candidates["pinokio"]["ref"]},
                "ordered_steps": retry["steps"], "command": _public_command(retry["command"]),
                "candidate_ref_transition": {"before": candidate_before["pinokio"],
                                              "after": _git(Path(candidates["pinokio"]["path"]), "rev-parse", "HEAD")}})
            retry_result = self._run(retry["command"], "retry")
            if retry_result.returncode:
                raise ExecutionError("candidate Pinokio action failed after successful Stop")
            retry_after_stop = {"result": "succeeded", "exit_code": 0}
        else:
            retry = self.data["retry"]
            self.recorder.emit("retry_begin", result="not-applicable", not_applicable=True)
            self._write_sidecar("action-array-retry.json", {
                "launcher": {"sha": candidates["pinokio"]["ref"]}, "ordered_steps": retry["steps"],
                "not_applicable": "first-click-succeeded"})
        readiness = self._readiness("after-update")
        self.recorder.emit("ready_sentinel", stage="after-update")
        self.recorder.emit("installer_end", action="retry" if first_outcome == "refused" else "first")
        self.recorder.emit("action_end", action="retry" if first_outcome == "refused" else "first")
        self.recorder.emit("retry_end", result="succeeded" if first_outcome == "refused" else "not-applicable")
        self.recorder.emit("health_probe_begin", stage="after-update")
        status, health, menu = self._readiness_sidecars("after-update")
        self._processes("after-retry-ready", expected_running=True)
        self.recorder.emit("health_probe_pass", stage="after-update", http_status=200)
        if (status.get("support", {}).get("healthy") is not True
                or health.get("status") not in {"ok", "healthy"}
                or menu.get("open_astrid") is not True):
            raise ExecutionError("post-update health/menu readiness contract failed")
        self.recorder.emit("stop_begin", reason="stop-start")
        final_stop = self._run(self.data["runtime"]["commands"]["stop"])
        if final_stop.returncode:
            raise ExecutionError("Stop before Start did not succeed")
        self.recorder.emit("stop_end", result="stopped", reason="stop-start")
        self._processes("after-final-stop", expected_running=False)
        self.recorder.emit("start_begin")
        start_result = self._run(self.data["runtime"]["commands"]["start"])
        if start_result.returncode:
            raise ExecutionError("Start after Stop did not succeed")
        restart_readiness = self._readiness("after-restart")
        self.recorder.emit("ready_sentinel", stage="after-restart")
        self.recorder.emit("local_set", stage="after-restart")
        self._processes("after-restart-ready", expected_running=True)
        restart_status, restart_health, restart_menu = self._readiness_sidecars("after-restart")
        if (restart_status.get("support", {}).get("healthy") is not True
                or restart_health.get("status") not in {"ok", "healthy"}
                or restart_menu.get("open_astrid") is not True):
            raise ExecutionError("post-restart health/menu readiness contract failed")
        self.recorder.emit("start_end", result="ready")
        negatives: dict[str, dict[str, Any]] = {}
        for name in REQUIRED_CONTROLS:
            negatives[name] = self._control(name)
            if name in {"busy_stop", "unknown_stop"}:
                negatives[name]["rearmed_after_stopped_retry"] = self._rearm_after_refusal_control(name)
        self.recorder.emit("final_cleanup_begin")
        final_cleanup = self._run(self.data["runtime"]["commands"]["stop"])
        if final_cleanup.returncode:
            raise ExecutionError("final controlled Stop did not succeed")
        final_processes = self._processes("after-cleanup", expected_running=False)
        self.recorder.emit("final_cleanup_end", result="stopped")
        self._write_sidecar("negative-controls.json", {"controls": negatives})
        self._write_sidecar("events.jsonl", self.recorder.rows)
        command_lines = [{"schema_version": SCHEMA_VERSION, "os": self.data["platform"]["os"],
                          "mode": MODE, **row} for row in self.commands]
        self._write_sidecar("commands.jsonl", command_lines)
        command_trace = {"path": "pinokio-backend/commands.jsonl",
                         "sha256": sha256_file(self.backend_dir / "commands.jsonl")}
        observed_after = {name: _git(Path(item["path"]), "rev-parse", "HEAD") for name, item in candidates.items()}
        backend = {
            "schema_version": SCHEMA_VERSION, "os": self.data["platform"]["os"], "mode": MODE,
            "hosted_lifecycle": {"status": "pass" if self.data["platform"]["hosted"] else "not-run"},
            "evidence_class": self.data["platform"]["evidence_class"], "source_override_test_mode": self.data["source_override"],
            "old_launcher": self.data["old"], "stopped_path_agent": self.data["stopped_path"],
            "candidate_ref_transition": {"from_old_launcher": self.data["old"]["ref"],
                                         "candidate_before_action": candidate_before,
                                         "candidate_after_action": observed_after},
            "first_click": {"outcome": first_outcome, "exit_code": first_result.returncode},
            "stop_after_refusal": stop_after_refusal,
            "retry_after_stop": retry_after_stop,
            "readiness_after_update": {**readiness, "status": "pass", "manual_start": False,
                                        "first_probe_http_status": 200},
            "stop_start": {"stop": {"result": "stopped", "next_action": "Start"},
                           "start": {"result": "ready", "next_action": "Stop", **restart_readiness}},
            "negative_controls": negatives, "not_applicable": ["A3"] if first_outcome == "succeeded" else [],
            "final_cleanup": {"result": "stopped", "exit_code": 0, "processes": final_processes},
            "sidecars": self.sidecars, "command_trace": command_trace,
        }
        platform_data = self.data["platform"]
        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION, "receipt_kind": "release1-installed-lifecycle",
            "status": "pass", "job_id": f"pinokio-backend-{platform_data['os']}", "mode": MODE,
            "scenario": "fresh-install", "runner_image": platform_data["runner"],
            "shell": platform_data["shell"], "evidence_class": platform_data["evidence_class"],
            "runner_provenance": platform_data["runner_provenance"],
            "os_build": f"{platform_data['os']}-{platform_module.platform()}",
            "source_override_test_mode": self.data["source_override"], "final_cleanup": "pass",
            "candidate_checkouts": {name: {**item, "sha": item["ref"]} for name, item in candidates.items()},
            "service_manager_prerequisite": {"outcome": "explicit-owner", "managed_by": "pinokio",
                                              "foreground_fallback": False},
            "source_unit_evidence": {"status": "pass", "kind": "pinokio-action-array-source-check",
                                     "evidence_class": "executable-boundary",
                                     "substitute_for_hosted": False},
            "pinokio_backend": backend,
        }
        _json_dump(self.output / "journey-result.json", receipt)
        return receipt


def admit_hosted_receipt(receipt: Mapping[str, Any]) -> None:
    """Raise unless a receipt is authentic hosted-lifecycle evidence."""
    if not isinstance(receipt, Mapping):
        raise ValidationError("hosted receipt must be an object")
    if receipt.get("evidence_class") != "hosted-lifecycle":
        raise ValidationError("source_fake/fixture/executable-boundary evidence cannot satisfy hosted receipt gate")
    if receipt.get("source_override_test_mode") is not False:
        raise ValidationError("hosted receipt gate rejects source overrides")
    backend = receipt.get("pinokio_backend")
    if not isinstance(backend, Mapping) or (backend.get("hosted_lifecycle") or {}).get("status") != "pass":
        raise ValidationError("hosted receipt lacks a passing hosted lifecycle")
    os_name = backend.get("os")
    if os_name not in HOSTED_RUNNERS:
        raise ValidationError("hosted receipt has no supported backend OS")
    expected_runner, expected_runner_os, expected_image_os = HOSTED_RUNNERS[os_name]
    provenance = receipt.get("runner_provenance") or {}
    if (receipt.get("job_id") != f"pinokio-backend-{os_name}"
            or receipt.get("runner_image") != expected_runner
            or provenance.get("RUNNER_OS") != expected_runner_os
            or provenance.get("ImageOS") != expected_image_os
            or not isinstance(provenance.get("ImageVersion"), str)
            or not provenance["ImageVersion"].strip()):
        raise ValidationError("hosted receipt runner provenance is absent or mismatched")
    old = backend.get("old_launcher") or {}
    if (old.get("ref") != APPROVED_OLD_LAUNCHER_REF
            or old.get("sha256") != APPROVED_OLD_UPDATE_SHA256
            or old.get("validated") is not True):
        raise ValidationError("hosted receipt does not use the approved immutable H1 launcher")
    stopped_path = backend.get("stopped_path_agent") or {}
    if (stopped_path.get("validated") is not True
            or set(stopped_path.get("required_ancestors") or []) != set(REQUIRED_STOPPED_PATH_ANCESTORS)):
        raise ValidationError("hosted receipt lacks the integrated stopped-path Agent provenance")
    source_unit = receipt.get("source_unit_evidence") or {}
    if source_unit.get("substitute_for_hosted") is not False:
        raise ValidationError("source/unit evidence is not visibly separate from hosted evidence")
    if receipt.get("final_cleanup") != "pass" or (backend.get("final_cleanup") or {}).get("result") != "stopped":
        raise ValidationError("hosted receipt lacks a passing controlled final cleanup")


def execute(config: Mapping[str, Any], output: Path) -> dict[str, Any]:
    """Validate and execute a supplied lifecycle, returning its receipt."""
    return PinokioBackendExecutor(config, output).execute()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="JSON executor input")
    parser.add_argument("--output", required=True, type=Path, help="new or empty receipt directory")
    parser.add_argument("--hosted-gate", action="store_true", help="admit the produced receipt as hosted evidence")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        receipt = execute(config, args.output)
        if args.hosted_gate:
            admit_hosted_receipt(receipt)
    except (OSError, json.JSONDecodeError, ValidationError, ExecutionError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through CLI smoke tests
    raise SystemExit(main())
