"""Named, installed-product install/update journeys for Linux, macOS and Windows.

The App checkout supplies the shipped manifest verbatim.  ``--source-override``
is an explicit test mode for candidate Agent and Runtime Git commits.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import ntpath
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from . import ci_windows_journey as windows
    from .journey_matrix import (
        M4_4A_OLD_LAUNCHER_REF,
        M4_4A_OLD_UPDATE_SHA256,
        M4_4A_PROVISIONAL_AGENT_REF,
        M4_4A_REQUIRED_AGENT_ANCESTORS,
        RELEASE1_MODES,
        validate_job,
    )
else:
    import ci_windows_journey as windows
    from journey_matrix import (
        M4_4A_OLD_LAUNCHER_REF,
        M4_4A_OLD_UPDATE_SHA256,
        M4_4A_PROVISIONAL_AGENT_REF,
        M4_4A_REQUIRED_AGENT_ANCESTORS,
        RELEASE1_MODES,
        validate_job,
    )


ROOT = Path(__file__).resolve().parents[1]
HEALTH_URL = windows.HEALTH_URL
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
OWNER_FILE = "tst-b1-owner.json"
VERSION_JUMP_LEGS = ("update-from-served", "update-from-release:pre-updater",
                     "update-over-old-declaration", "old-runtime-busy",
                     "release0-to-release1")
RUNNER_FAMILY = {"ubuntu-24.04": "linux", "ubuntu-22.04": "linux",
                 "windows-2022": "win32", "macos-14": "darwin", "macos-15": "darwin"}
GITHUB_RUNNER_OS = {"Linux": "linux", "Windows": "win32", "macOS": "darwin"}
PACK_HOST_READINESS_OBSERVATIONS = (
    "first-failed-poll",
    "first-service-exit",
    "definitive-failure",
)
PACK_HOST_READINESS_EVIDENCE_NAMES = frozenset(
    f"pack-host-readiness-{observation}{suffix}"
    for observation in PACK_HOST_READINESS_OBSERVATIONS
    for suffix in (".json", "-service-pack-host.log", "-runtime-service.log", "-generic-host.log")
)
PACK_HOST_READINESS_NESTED_EVIDENCE_ROOTS = (
    Path("release0-api-seed") / "release0-task",
    Path("release1-working-task"),
)
WINDOWS_JOB_DRAIN_CAPTURE_NAME = "runtime-service-service-job-drain-capture.log"
WINDOWS_JOB_DRAIN_CAPTURE_LIMIT = 64 * 1024
WINDOWS_JOB_DRAIN_LOG_LIMIT = 32 * 1024
WINDOWS_JOB_DRAIN_LAUNCH_ID = re.compile(r"[0-9a-f]{32}")
WINDOWS_JOB_DRAIN_BIRTH_ID = re.compile(r"windows-creation:[0-9]{1,32}")
WINDOWS_JOB_DRAIN_PRIVATE_PATH = re.compile(r"(?i)(?:[a-z]:\\[^\r\n]*|/(?:home|users|var|tmp)/[^\r\n]*)")

JOURNEY_EVIDENCE_NAMES = frozenset({
    "journey-result.json", "install.log", "uninstall.log", "final-uninstall.log",
    "install-bootstrap.log", "update-bootstrap.log", "release0-bootstrap.log", "start.log",
    "verify-install.json", "verify-install.log", "verify-update.json", "verify-update.log",
    "service-manager-prerequisite.log", "direct-service-ownership.log",
    "preflight-scheduled-task.txt",
    "windows-bootstrap-environment.log",
    "runtime-owner-processes.json",
    "runtime-owner-assertion.json",
    "runtime-service-runtime.log",
    "manager-state-before-cleanup.json", "manager-state-after-cleanup.json",
    "manager-state-before-cleanup.log", "manager-state-after-cleanup.log",
    "unit-file-before-cleanup.txt", "unit-file-after-cleanup.txt",
})
EVIDENCE_LOG_LIMIT = 1024 * 1024
OWNER_CENSUS_MAX_RECORDS = 8
OWNER_ASSERTION_SNAPSHOT = "runtime-owner-assertion.json"
NATIVE_RUNTIME_LOG_DIR_ENV = "ASTRID_CI_NATIVE_RUNTIME_LOG_DIR"
VIBECOMFY_PIN_PATH = "astrid/packs/vibecomfy/pins.json"


def journey_os_label() -> str:
    return {"darwin": "macos", "linux": "linux", "win32": "windows"}.get(sys.platform, sys.platform)


def _git_show(checkout: Path, ref: str, path: str) -> bytes:
    try:
        return subprocess.check_output(["git", "-C", str(checkout), "show", f"{ref}:{path}"])
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"Pinokio old-launcher input does not contain {path} at {ref}") from exc


def validate_old_launcher_provenance(pinokio: Path, old_launcher_ref: str) -> dict:
    """Authenticate the historical action before any Pinokio lifecycle call."""
    if not isinstance(old_launcher_ref, str) or not re.fullmatch(r"[0-9a-f]{40}", old_launcher_ref):
        raise ValueError("--old-launcher-ref must be a full 40-character commit SHA")
    if pinokio.is_symlink():
        raise ValueError("--pinokio-checkout must not be a symlink")
    pinokio = pinokio.resolve(strict=True)
    try:
        subprocess.run(["git", "-C", str(pinokio), "cat-file", "-e", f"{old_launcher_ref}^{{commit}}"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("--old-launcher-ref is not present in the Pinokio checkout") from exc
    source = _git_show(pinokio, old_launcher_ref, "pinokio/update.js")
    source_sha256 = hashlib.sha256(source).hexdigest()
    if old_launcher_ref == M4_4A_OLD_LAUNCHER_REF and source_sha256 != M4_4A_OLD_UPDATE_SHA256:
        raise ValueError("the approved old-launcher update.js bytes do not match its provenance")
    if old_launcher_ref != M4_4A_OLD_LAUNCHER_REF or source_sha256 != M4_4A_OLD_UPDATE_SHA256:
        raise ValueError("M4-4a requires the approved release-0 old-launcher action provenance")
    return {"ref": old_launcher_ref, "path": "pinokio/update.js", "sha256": source_sha256,
            "vector": "H1", "validated": True}


_PINOKIO_ACTION_SCRIPT = r'''
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const [sourceRoot, appDirectory, mode] = process.argv.slice(1);
const lib = require(path.join(sourceRoot, 'pinokio', 'lib.js'));
const app = lib.createApp({ appDirectory });
const bootstrapActions = new Map();
const realBootstrap = app.bootstrapScript.bind(app);
app.bootstrapScript = (action, ...args) => {
  const message = realBootstrap(action, ...args);
  bootstrapActions.set(message, action);
  return message;
};
const realShell = app.shellStep.bind(app);
app.shellStep = (message, ...args) => {
  const step = realShell(message, ...args);
  step.__bootstrap_action = bootstrapActions.get(message) || null;
  return step;
};
app.readInstallMode = () => mode;
app.hasPendingUpdate = () => false;
const source = path.join(sourceRoot, 'pinokio', 'update.js');
const sourceText = fs.readFileSync(source, 'utf8');
const module = { exports: {} };
const injectedRequire = (name) => name === './lib' ? app : require(name);
vm.runInNewContext(sourceText, { module, exports: module.exports, require: injectedRequire }, { filename: source });
Promise.resolve(module.exports({}, {})).then((action) => {
  const normalize = (value) => {
    if (typeof value === 'function') return { kind: 'custom', name: value.name || 'anonymous' };
    if (Array.isArray(value)) return value.map(normalize);
    if (!value || typeof value !== 'object') return value;
    if (value.method === 'shell.run') {
      const params = value.params || {};
      const message = String(params.message || '');
      return { kind: params.env?.ASTRID_PINOKIO_ENVIRONMENT === '1' ? 'git-shell' : 'shell',
        message, bootstrap_action: value.__bootstrap_action || null };
    }
    if (typeof value.method === 'function') return { kind: 'custom', name: value.method.name || 'anonymous' };
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, normalize(item)]));
  };
  process.stdout.write(JSON.stringify({ run: normalize(action.run || []), on: normalize(action.on || null) }));
}).catch((error) => { console.error(error.stack || error); process.exitCode = 1; });
'''


def _export_pinokio_source(pinokio: Path, ref: str, destination: Path) -> None:
    """Export committed launcher bytes without checking out or mutating the input."""
    archive = subprocess.check_output(
        ["git", "-C", str(pinokio), "archive", "--format=tar", ref, "pinokio"],
    )
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
        for member in stream.getmembers():
            relative = Path(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Pinokio source archive contains an unsafe path")
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                extracted = stream.extractfile(member)
                if extracted is None:
                    raise ValueError("Pinokio source archive contains an unreadable file")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(extracted.read())
            else:
                raise ValueError("Pinokio source archive contains a non-regular entry")


def build_pinokio_action(pinokio: Path, source_ref: str, *, mode: str = "backend",
                         app_directory: Path | None = None) -> dict:
    """Build an action from one immutable launcher commit and its matching lib."""
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is required to build the Pinokio action")
    pinokio = pinokio.resolve(strict=True)
    if not re.fullmatch(r"[0-9a-f]{40}", source_ref):
        raise ValueError("Pinokio action source must be a full commit SHA")
    app_directory = (app_directory or pinokio).resolve()
    with tempfile.TemporaryDirectory(prefix="astrid-m4-4a-action-") as temporary:
        source_root = Path(temporary)
        _export_pinokio_source(pinokio, source_ref, source_root)
        node_args = ["-e", _PINOKIO_ACTION_SCRIPT, str(source_root),
                     str(app_directory), mode]
        argv = (windows.powershell5_command(node, node_args)
                if sys.platform == "win32" else [node, *node_args])
        completed = subprocess.run(
            argv,
            capture_output=True, text=True, timeout=30, check=False,
        )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise RuntimeError(f"Pinokio action construction failed: {detail[-1] if detail else 'unknown error'}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Pinokio action construction returned invalid JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("run"), list):
        raise RuntimeError("Pinokio action did not return a run array")
    return value


def _write_sidecar(path: Path, value: object, *, receipt_root: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, str):
        path.write_text(value, encoding="utf-8")
    else:
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"path": path.relative_to(receipt_root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _emit_pinokio_event(stream, sequence: int, name: str, *, mode: str = "pinokio-backend",
                        details: dict | None = None) -> int:
    row = {"schema_version": 1, "seq": sequence, "timestamp": time.time(),
           "event": name, "os": journey_os_label(), "mode": mode}
    if details:
        row["details"] = details
    stream.write(json.dumps(row, sort_keys=True) + "\n")
    stream.flush()
    return sequence + 1


def validate_m4_4a_product_ref(args: argparse.Namespace) -> dict:
    """Require an explicit integrated stopped-path Agent before hosted execution."""
    supplied = getattr(args, "stopped_path_agent_ref", None)
    if not isinstance(supplied, str) or not re.fullmatch(r"[0-9a-f]{40}", supplied):
        raise ValueError("pinokio-backend requires --stopped-path-agent-ref as a full commit SHA")
    if supplied == M4_4A_PROVISIONAL_AGENT_REF:
        raise ValueError("the provisional Agent base does not contain the integrated stopped path")
    checkout = getattr(args, "candidate_agent_checkout", None)
    if checkout is None or git_source(checkout)["sha"] != supplied:
        raise ValueError("--stopped-path-agent-ref must equal the frozen candidate Agent checkout")
    missing = [ref for ref in M4_4A_REQUIRED_AGENT_ANCESTORS if subprocess.run(
        ["git", "-C", str(checkout), "merge-base", "--is-ancestor", ref, supplied],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode != 0]
    if missing:
        raise ValueError("candidate Agent lacks the paired stopped-path commits: " + ", ".join(missing))
    candidate_path = getattr(args, "candidate", None)
    if candidate_path is None or read_json(candidate_path).get("agent") != supplied:
        raise ValueError("--stopped-path-agent-ref must equal the frozen candidate Agent input")
    return {"ref": supplied, "required_ancestors": list(M4_4A_REQUIRED_AGENT_ANCESTORS),
            "provisional_base_refused": M4_4A_PROVISIONAL_AGENT_REF, "validated": True}


def prepare_pinokio_backend(args: argparse.Namespace, evidence: Path) -> dict:
    """Build the old and retry action receipts before lifecycle execution.

    This phase is intentionally independent of hosted execution.  It is useful
    in unit/source checks, but the aggregate gate only accepts it when the same
    receipt also contains hosted lifecycle evidence.
    """
    if not getattr(args, "old_launcher_ref", None):
        raise ValueError("pinokio-backend requires --old-launcher-ref")
    pinokio = args.pinokio_checkout.resolve(strict=True)
    provenance = validate_old_launcher_provenance(pinokio, args.old_launcher_ref)
    product = validate_m4_4a_product_ref(args)
    backend_evidence = evidence / "pinokio-backend"
    backend_evidence.mkdir(parents=True, exist_ok=True)
    events_path = backend_evidence / "events.jsonl"
    sequence = 1
    with events_path.open("w", encoding="utf-8") as events:
        sequence = _emit_pinokio_event(events, sequence, "action_built",
                                       details={"launcher_ref": provenance["ref"]})
        first = build_pinokio_action(pinokio, provenance["ref"], app_directory=pinokio)
        first_steps = first["run"]
        shape = [(step.get("kind"), step.get("bootstrap_action"))
                 for step in first_steps if isinstance(step, dict)]
        if shape != [("git-shell", None), ("shell", "update-app")]:
            raise ValueError("approved old-launcher action is not the H1 backend update array")
        first_record = {"schema_version": 1, "os": journey_os_label(), "mode": "pinokio-backend",
                        "built_before_ref_advance": True, "launcher": provenance,
                        "ordered_steps": first_steps}
        first_sidecar = _write_sidecar(
            backend_evidence / "action-array-first.json", first_record, receipt_root=evidence)
        sequence = _emit_pinokio_event(events, sequence, "launcher_ref_advanced",
                                       details={"launcher_ref": git_source(pinokio)["sha"]})
        retry_ref = git_source(pinokio)["sha"]
        retry = build_pinokio_action(pinokio, retry_ref, app_directory=pinokio)
        retry_record = {"schema_version": 1, "os": journey_os_label(), "mode": "pinokio-backend",
                        "launcher": git_source(pinokio), "ordered_steps": retry["run"]}
        retry_sidecar = _write_sidecar(
            backend_evidence / "action-array-retry.json", retry_record, receipt_root=evidence)
    events_sidecar = {"path": events_path.relative_to(evidence).as_posix(),
                      "sha256": hashlib.sha256(events_path.read_bytes()).hexdigest()}
    def blocked(phase: str) -> dict:
        return {"schema_version": 1, "os": journey_os_label(), "mode": "pinokio-backend",
                "phase": phase, "status": "blocked",
                "reason": "hosted lifecycle executor not admitted"}
    return {"schema_version": 1, "os": journey_os_label(), "mode": "pinokio-backend",
            "evidence_kind": "source-unit", "hosted_lifecycle": {
                "status": "not-run", "reason": "integrated-product execution not admitted",
            },
            "old_launcher": provenance, "stopped_path_agent": product,
            "first_click": blocked("first-click"),
            "stop_after_refusal": blocked("stop-after-refusal"),
            "retry_after_stop": blocked("retry-after-stop"),
            "readiness_after_update": blocked("readiness-after-update"),
            "stop_start": {"schema_version": 1, "os": journey_os_label(),
                           "mode": "pinokio-backend", "status": "blocked",
                           "reason": "hosted lifecycle executor not admitted"},
            "negative_controls": {
                name: blocked(name) for name in (
                    "busy_stop", "unknown_stop", "identity_mismatch", "instance_mismatch")
            },
            "historical_compatibility": blocked("historical-compatibility"),
            "provisional_dependencies": {
                "required_agent_ancestors": list(M4_4A_REQUIRED_AGENT_ANCESTORS),
                "hosted_receipts": "not-run", "g3": "external-gate",
            },
            "first_action_sha256": first_sidecar["sha256"],
            "retry_action_sha256": retry_sidecar["sha256"],
            "events_sha256": events_sidecar["sha256"],
            "sidecars": {"action-array-first.json": first_sidecar,
                          "action-array-retry.json": retry_sidecar,
                          "events.jsonl": events_sidecar},
            "not_applicable": []}


def execute_pinokio_backend(args: argparse.Namespace, evidence: Path) -> dict:
    """Run the real M4-4a executor from a complete, explicit run-owned config."""
    config_path = getattr(args, "pinokio_backend_config", None)
    if config_path is None:
        raise ValueError("pinokio-backend requires --pinokio-backend-config; source-only receipts cannot pass")
    config = read_json(config_path)
    if config.get("source_override_test_mode") is not False:
        raise ValueError("M4-4a executor config must explicitly disable source overrides")
    expected_os = journey_os_label()
    identity = runner_identity()
    runner = identity["runner_image"]
    expected_platform = {
        "os": expected_os,
        "runner": runner,
        "shell": args.shell,
        "hosted": runner != "local",
    }
    platform_input = config.get("platform") or {}
    for name, value in expected_platform.items():
        if platform_input.get(name) != value:
            raise ValueError(f"M4-4a config platform.{name} must match this matrix job")
    if config.get("candidate_checkouts") is None:
        raise ValueError("M4-4a executor requires explicit candidate checkout inputs")
    actual = getattr(args, "candidate_checkouts", None)
    if not isinstance(actual, dict):
        raise ValueError("M4-4a executor requires the frozen four-checkout tuple")
    checkouts = {"app": args.app_checkout,
                 "agent": args.candidate_agent_checkout,
                 "runtime": args.candidate_runtime_checkout,
                 "pinokio": args.pinokio_checkout}
    for name, checkout in checkouts.items():
        supplied = config["candidate_checkouts"].get(name)
        if not isinstance(supplied, dict):
            raise ValueError(f"M4-4a config candidate_checkouts.{name} is required")
        if (Path(supplied.get("path", "")).resolve() != checkout.resolve()
                or supplied.get("ref") != actual[name]["sha"]):
            raise ValueError(f"M4-4a config candidate {name} differs from the frozen checkout")
    old_input = config.get("old_launcher")
    if not isinstance(old_input, dict) or old_input.get("ref") != args.old_launcher_ref:
        raise ValueError("M4-4a config old launcher differs from --old-launcher-ref")
    old = validate_old_launcher_provenance(args.pinokio_checkout, args.old_launcher_ref)
    first_steps = build_pinokio_action(args.pinokio_checkout, args.old_launcher_ref,
                                       app_directory=args.pinokio_checkout)["run"]
    candidate_pinokio_ref = actual["pinokio"]["sha"]
    retry_steps = build_pinokio_action(args.pinokio_checkout, candidate_pinokio_ref,
                                       app_directory=args.pinokio_checkout)["run"]
    actions = config.get("actions") or {}
    if (not isinstance(actions.get("first"), dict)
            or actions["first"].get("steps") != first_steps
            or not isinstance(actions.get("retry"), dict)
            or actions["retry"].get("steps") != retry_steps):
        raise ValueError("M4-4a explicit action arrays must match the real immutable Pinokio action builder")
    try:
        from .shared_journey_driver import JourneyDriver
    except ImportError:
        from shared_journey_driver import JourneyDriver
    config = JourneyDriver.compose_pinokio_backend_config(config)
    config.setdefault("old_launcher", {}).update({
        "checkout": str(args.pinokio_checkout.resolve()), "path": old["path"],
        "sha256": old["sha256"],
    })
    try:
        from . import pinokio_backend_executor as backend_executor
    except ImportError:
        import pinokio_backend_executor as backend_executor
    with tempfile.TemporaryDirectory(prefix="astrid-m4-4a-executor-") as temporary:
        executor_output = Path(temporary) / "output"
        receipt = JourneyDriver.execute_pinokio_backend(
            config, executor_output, backend_executor
        )
        if platform_input["hosted"]:
            backend_executor.admit_hosted_receipt(receipt)
        sidecar_root = executor_output / "pinokio-backend"
        if not sidecar_root.is_dir():
            raise RuntimeError("M4-4a executor omitted its required sidecars")
        destination = evidence / "pinokio-backend"
        if destination.exists():
            raise RuntimeError("M4-4a receipt sidecar destination already exists")
        shutil.copytree(sidecar_root, destination)
    backend = receipt.get("pinokio_backend")
    if not isinstance(backend, dict):
        raise RuntimeError("M4-4a executor did not return a backend receipt")
    return receipt


def runner_identity() -> dict:
    """Record the workflow's declared image and raw hosted-runner observations."""
    label = os.environ.get("ASTRID_CI_EXPECTED_RUNNER") or "local"
    raw = {key: os.environ[key] for key in ("RUNNER_OS", "ImageOS", "ImageVersion")
           if os.environ.get(key)}
    return {"runner_image": label,
            "runner_provenance": {"observed_os_family": sys.platform, **raw}}


def assert_runner_identity(identity: dict) -> None:
    label = identity["runner_image"]
    if label != "local":
        if label not in RUNNER_FAMILY:
            raise ValueError(f"Unknown controlled runner label: {label}")
        if RUNNER_FAMILY[label] != sys.platform:
            raise RuntimeError(f"Runner label {label} disagrees with observed {sys.platform}")
        missing = [key for key in ("RUNNER_OS", "ImageOS", "ImageVersion")
                   if key not in identity["runner_provenance"]]
        if missing:
            raise RuntimeError(f"Hosted runner provenance is missing: {', '.join(missing)}")
    reported_os = identity["runner_provenance"].get("RUNNER_OS")
    if reported_os is not None and GITHUB_RUNNER_OS.get(reported_os) != sys.platform:
        raise RuntimeError(f"RUNNER_OS {reported_os} disagrees with observed {sys.platform}")


def credential_name(name: str) -> bool:
    upper = name.upper()
    return (upper in {"FAL_KEY", "GH_TOKEN", "GITHUB_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS"}
            or upper.startswith(("AWS_", "AZURE_"))
            or upper.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIALS")))


@contextmanager
def without_provider_credentials():
    removed = {key: value for key, value in os.environ.items() if credential_name(key)}
    for key in removed:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        os.environ.update(removed)


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def capture_manager_state(evidence: Path, data: Path, phase: str) -> None:
    """Record small, named service-manager and unit observations around cleanup."""
    evidence.mkdir(parents=True, exist_ok=True)
    manager_path = evidence / f"manager-state-{phase}.json"
    unit_path = evidence / f"unit-file-{phase}.txt"
    manager = {"platform": sys.platform, "phase": phase}
    unit_content = None
    try:
        if sys.platform == "linux":
            unit = data.parent / "home" / ".config" / "systemd" / "user" / "astrid.service"
            command = ["systemctl", "--user", "show", "astrid.service",
                       "--property=LoadState,FragmentPath,ActiveState,SubState"]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
            manager.update(command=command, returncode=completed.returncode,
                           stdout=completed.stdout, stderr=completed.stderr)
        elif sys.platform == "darwin":
            unit = data.parent / "home" / "Library" / "LaunchAgents" / "haus.astrid.runtime.plist"
            command = ["launchctl", "print", f"gui/{os.getuid()}/haus.astrid.runtime"]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
            manager.update(command=command, returncode=completed.returncode,
                           stdout=completed.stdout, stderr=completed.stderr)
        elif sys.platform == "win32":
            output = windows.powershell(
                "$t=Get-ScheduledTask -TaskName 'Astrid Runtime' -ErrorAction SilentlyContinue; "
                "if ($null -eq $t) { 'not-found' } else { 'STATE:'; $t | Get-ScheduledTaskInfo | "
                "Select-Object TaskName,LastRunTime,LastTaskResult,NumberOfMissedRuns | "
                "ConvertTo-Json -Compress; 'TASK_XML:'; Export-ScheduledTask -TaskName 'Astrid Runtime' }",
                f"manager-state-{phase}.log", show=False, timeout=30)
            manager.update(command="Get-ScheduledTask / Get-ScheduledTaskInfo", stdout=output)
            task_xml = re.split(r"TASK_XML:\r?\n", output, maxsplit=1)
            unit_content = task_xml[1] if len(task_xml) == 2 else None
            manager["unit_file"] = "Task Scheduler exported task definition" if unit_content else None
        else:
            raise RuntimeError(f"Unsupported service-manager platform: {sys.platform}")
        if sys.platform == "win32" and unit_content:
            unit_path.write_text(unit_content, encoding="utf-8")
        elif sys.platform != "win32" and unit.is_file() and not unit.is_symlink():
            unit_path.write_bytes(unit.read_bytes())
            manager["unit_file"] = str(unit)
        elif sys.platform != "win32":
            unit_path.write_text(f"Unit file unavailable: {unit}\n", encoding="utf-8")
            manager["unit_file"] = None
        else:
            unit_path.write_text("Task Scheduler definition unavailable\n", encoding="utf-8")
        if sys.platform in {"linux", "darwin"} and completed.returncode:
            missing_service = (sys.platform == "darwin" and phase == "after-cleanup"
                               and completed.returncode == 113
                               and completed.stderr.strip() ==
                               f'Could not find service "haus.astrid.runtime" in domain for user gui: {os.getuid()}')
            if missing_service:
                manager["service_state"] = "absent"
            else:
                manager["error"] = f"manager command exited {completed.returncode}"
                print(f"Journey manager-state capture ({phase}) failed: {manager['error']}: "
                      f"{(completed.stderr or completed.stdout).strip()}", file=sys.stderr, flush=True)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        print(f"Journey evidence capture ({phase}) failed: {message}", file=sys.stderr, flush=True)
        manager["error"] = message
        possible_unit = {
            "linux": data.parent / "home" / ".config" / "systemd" / "user" / "astrid.service",
            "darwin": data.parent / "home" / "Library" / "LaunchAgents" / "haus.astrid.runtime.plist",
        }.get(sys.platform)
        try:
            if possible_unit is not None and possible_unit.is_file() and not possible_unit.is_symlink():
                unit_path.write_bytes(possible_unit.read_bytes())
                manager["unit_file"] = str(possible_unit)
            else:
                unit_path.write_text(f"Unit file capture failed: {message}\n", encoding="utf-8")
        except OSError as unit_error:
            print(f"Journey unit-file capture ({phase}) failed: {type(unit_error).__name__}: {unit_error}",
                  file=sys.stderr, flush=True)
    try:
        write_json(manager_path, manager)
    except Exception as exc:
        print(f"Journey manager-state write ({phase}) failed: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)


def capture_runtime_service_logs(evidence: Path, run_root: Path, phase: str) -> None:
    """Retain bounded tails of the run-owned Runtime and named service logs."""
    if sys.platform == "darwin":
        log_dir = run_root / "home" / "Library" / "Logs" / "Astrid Runtime"
    elif sys.platform == "win32":
        log_dir = run_root / "localappdata" / "Astrid Runtime" / "logs"
    else:
        log_dir = run_root / "home" / ".local" / "state" / "astrid-runtime" / "logs"
    paths = [log_dir / "install.log", log_dir / "runtime.log", *sorted(log_dir.glob("service-*.log"))]
    for source in paths:
        if source.is_symlink() or not source.is_file():
            continue
        name = ("install.log" if source.name == "install.log" else
                "runtime-service-runtime.log" if source.name == "runtime.log" else
                f"runtime-service-{source.name}")
        target = evidence / name
        try:
            with source.open("rb") as stream:
                stream.seek(max(0, source.stat().st_size - EVIDENCE_LOG_LIMIT))
                target.write_bytes(stream.read(EVIDENCE_LOG_LIMIT))
        except Exception as exc:
            print(f"Journey Runtime log capture ({phase}) failed for {source.name}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def _windows_job_drain_file(path: Path, *, limit: int, tail: bool = False) -> tuple[dict, bytes | None]:
    """Read a bounded regular diagnostic file without following a leaf symlink."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"status": "absent"}, None
    except OSError as exc:
        return {"status": "unreadable", "error": type(exc).__name__}, None
    if stat.S_ISLNK(info.st_mode):
        return {"status": "unsafe", "reason": "symlink"}, None
    if not stat.S_ISREG(info.st_mode):
        return {"status": "unsafe", "reason": "non-regular"}, None
    size = info.st_size
    if not tail and size > limit:
        return {"status": "unsafe", "reason": "oversized", "bytes": size, "limit": limit}, None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                return {"status": "unsafe", "reason": "opened-non-regular"}, None
            if tail:
                os.lseek(descriptor, max(0, opened.st_size - limit), os.SEEK_SET)
            chunks = []
            remaining = min(opened.st_size, limit)
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
    except OSError as exc:
        return {"status": "unreadable", "error": type(exc).__name__}, None
    content = b"".join(chunks)
    return {"status": "read", "bytes": size, "truncated": bool(tail and size > limit)}, content


def _windows_job_drain_file_metadata(path: Path) -> dict:
    """Observe a file's safe leaf type without opening authority material."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"status": "absent"}
    except OSError as exc:
        return {"status": "unreadable", "error": type(exc).__name__}
    if stat.S_ISLNK(info.st_mode):
        return {"status": "unsafe", "reason": "symlink"}
    if not stat.S_ISREG(info.st_mode):
        return {"status": "unsafe", "reason": "non-regular"}
    return {"status": "regular", "bytes": info.st_size}


def _windows_job_drain_ownership(path: Path) -> dict:
    """Summarize Windows ACL facts without retaining account names or ACL text."""
    if os.name != "nt":
        return {"status": "unavailable", "reason": "not a native Windows capture"}
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$acl = Get-Acl -LiteralPath $args[0] -ErrorAction Stop
$owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
$current = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
$writeMask = [int]([Security.AccessControl.FileSystemRights]::WriteData -bor
    [Security.AccessControl.FileSystemRights]::AppendData -bor
    [Security.AccessControl.FileSystemRights]::WriteAttributes -bor
    [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
    [Security.AccessControl.FileSystemRights]::Delete)
$foreignAllowWrites = @($rules | Where-Object {
    $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
    ([int]$_.FileSystemRights -band $writeMask) -ne 0 -and $_.IdentityReference.Value -ne $owner
})
@{
    status = 'observed'
    owner_matches_current_user = ($owner -eq $current)
    access_rule_count = $rules.Count
    foreign_allow_write_rule_count = $foreignAllowWrites.Count
} | ConvertTo-Json -Compress
"""
    env = {key: value for key, value in os.environ.items() if key.casefold() != "psmodulepath"}
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script, str(path)],
            env=env, capture_output=True, text=True, timeout=15, check=False,
        )
        if completed.returncode:
            return {"status": "unavailable", "reason": "acl-query-failed",
                    "exit_code": completed.returncode}
        value = json.loads(completed.stdout)
        if not isinstance(value, dict) or value.get("status") != "observed":
            return {"status": "unavailable", "reason": "acl-query-malformed"}
        if (type(value.get("owner_matches_current_user")) is not bool or
                type(value.get("access_rule_count")) is not int or
                type(value.get("foreign_allow_write_rule_count")) is not int):
            return {"status": "unavailable", "reason": "acl-query-malformed"}
        return value
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"status": "unavailable", "reason": "acl-query-unavailable"}


def _windows_job_drain_identifier(value: object, pattern: re.Pattern[str]) -> dict:
    """Keep only known opaque identifiers; never serialize arbitrary state text."""
    if isinstance(value, str) and pattern.fullmatch(value):
        return {"status": "observed", "value": value}
    if value is None:
        return {"status": "unavailable"}
    return {"status": "invalid"}


def _windows_job_drain_pid(value: object) -> dict:
    if type(value) is int and value > 1:
        return {"status": "observed", "value": value}
    if value is None:
        return {"status": "unavailable"}
    return {"status": "invalid"}


def _windows_job_drain_path_binding(value: object, expected: Path) -> str:
    if not isinstance(value, str) or not value:
        return "unavailable"
    return "matches-runtime-support" if _same_windows_path(value, str(expected)) else "mismatch"


def _windows_job_drain_entry_matches(entry: object, state: dict | None, support: Path) -> bool:
    """Mirror only the frozen receipt fields for a diagnostic classification.

    This helper intentionally does not establish the Agent's file-ownership
    predicate or take any lifecycle action; the installed Agent remains the
    authoritative consumer.
    """
    if not isinstance(entry, dict) or not isinstance(state, dict) or entry.get("job_drained") is not True:
        return False
    if _windows_job_drain_path_binding(state.get("support_root"), support) != "matches-runtime-support":
        return False
    if entry.get("service_name", "pack-host") != "pack-host":
        return False
    if _windows_job_drain_path_binding(entry.get("support_root", str(support)), support) != "matches-runtime-support":
        return False
    launch = state.get("runtime_service_launch_id")
    if launch is not None:
        if not isinstance(launch, str) or WINDOWS_JOB_DRAIN_LAUNCH_ID.fullmatch(launch) is None:
            return False
        identities_valid = ((entry.get("pid") is None and entry.get("process_birth_id") is None) or
                            (type(entry.get("pid")) is int and entry["pid"] > 1 and
                             isinstance(entry.get("process_birth_id"), str) and bool(entry["process_birth_id"])))
        return identities_valid and entry.get("runtime_service_launch_id") == launch
    return (type(entry.get("pid")) is int and entry["pid"] > 1 and
            isinstance(entry.get("process_birth_id"), str) and bool(entry["process_birth_id"]) and
            entry.get("pid") == state.get("pid") and
            entry.get("process_birth_id") == state.get("process_birth_id"))


def _windows_job_drain_entry_projection(entry: object, state: dict | None, support: Path) -> dict:
    if not isinstance(entry, dict):
        return {"status": "invalid"}
    birth = entry.get("process_birth_id")
    if birth is None:
        identity_shape = "none"
    elif type(entry.get("pid")) is int and entry["pid"] > 1 and isinstance(birth, str) and birth:
        identity_shape = "valid"
    else:
        identity_shape = "invalid"
    return {
        "status": "observed",
        "runtime_service_launch_id": _windows_job_drain_identifier(
            entry.get("runtime_service_launch_id"), WINDOWS_JOB_DRAIN_LAUNCH_ID),
        "pid": _windows_job_drain_pid(entry.get("pid")),
        "process_identity_shape": identity_shape,
        "job_drained": entry.get("job_drained") is True,
        "service_name": "pack-host" if entry.get("service_name", "pack-host") == "pack-host" else "other",
        "support_root_binding": _windows_job_drain_path_binding(
            entry.get("support_root", str(support)), support),
        "matches_stale_registration": _windows_job_drain_entry_matches(entry, state, support),
    }


def _windows_job_drain_log(path: Path) -> dict:
    details, content = _windows_job_drain_file(
        path, limit=WINDOWS_JOB_DRAIN_LOG_LIMIT, tail=True)
    if content is None:
        return details
    if not content:
        return {**details, "status": "empty", "captured_bytes": 0}
    safe = windows._redact_pack_host_text(content.decode("utf-8", errors="replace"))
    safe = WINDOWS_JOB_DRAIN_PRIVATE_PATH.sub("<redacted path>", safe)
    encoded = safe.encode("utf-8")[-WINDOWS_JOB_DRAIN_LOG_LIMIT:]
    return {**details, "status": "captured", "captured_bytes": len(encoded),
            "redacted": encoded != content, "content": encoded.decode("utf-8", errors="replace")}


def capture_windows_job_drain_evidence(evidence: Path, run_root: Path, data: Path) -> dict:
    """Capture bounded R0 Job-drain facts without reading credentials or mutating state."""
    support = data / "runtime"
    registration_path = support / "generic-host.json"
    receipt_path = support / "pack-host-job-cleanup.json"
    discovery_path = support / "discovery.json"
    registration_file, registration_bytes = _windows_job_drain_file(
        registration_path, limit=WINDOWS_JOB_DRAIN_CAPTURE_LIMIT)
    registration = {"file": registration_file, "status": registration_file["status"]}
    state = None
    if registration_bytes is not None:
        try:
            state = json.loads(registration_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            registration["status"] = "malformed"
        if isinstance(state, dict):
            registration.update(
                status="captured",
                pid=_windows_job_drain_pid(state.get("pid")),
                process_birth_id=_windows_job_drain_identifier(
                    state.get("process_birth_id"), WINDOWS_JOB_DRAIN_BIRTH_ID),
                runtime_instance_id=_windows_job_drain_identifier(
                    state.get("runtime_instance_id"), WINDOWS_JOB_DRAIN_LAUNCH_ID),
                runtime_service_launch_id=_windows_job_drain_identifier(
                    state.get("runtime_service_launch_id"), WINDOWS_JOB_DRAIN_LAUNCH_ID),
                support_root_binding=_windows_job_drain_path_binding(state.get("support_root"), support),
            )
        elif registration_bytes is not None and registration["status"] != "malformed":
            registration["status"] = "malformed"
    if not isinstance(state, dict):
        state = None

    discovery_file, discovery_bytes = _windows_job_drain_file(
        discovery_path, limit=WINDOWS_JOB_DRAIN_CAPTURE_LIMIT)
    current_runtime = {"file": discovery_file, "status": discovery_file["status"]}
    if discovery_bytes is not None:
        try:
            discovery = json.loads(discovery_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            discovery = None
        if isinstance(discovery, dict):
            current_runtime.update(
                status="captured",
                runtime_instance_id=_windows_job_drain_identifier(
                    discovery.get("runtime_instance_id"), WINDOWS_JOB_DRAIN_LAUNCH_ID),
                pid=_windows_job_drain_pid(discovery.get("pid")),
                process_birth_id=_windows_job_drain_identifier(
                    discovery.get("process_birth_id"), WINDOWS_JOB_DRAIN_BIRTH_ID),
            )
        else:
            current_runtime["status"] = "malformed"

    receipt_file, receipt_bytes = _windows_job_drain_file(
        receipt_path, limit=WINDOWS_JOB_DRAIN_CAPTURE_LIMIT)
    lock_path = support / ".pack-host-job-cleanup.lock"
    lock = _windows_job_drain_file_metadata(lock_path)
    receipt = {"file": {**receipt_file, "ownership": (_windows_job_drain_ownership(receipt_path)
                                                         if receipt_file["status"] == "read"
                                                         else {"status": "not-observed"})},
               "lock": {**lock, "ownership": (_windows_job_drain_ownership(lock_path)
                                                  if lock["status"] == "regular"
                                                  else {"status": "not-observed"})},
               "status": receipt_file["status"]}
    if receipt_bytes is not None:
        try:
            value = json.loads(receipt_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            value = None
        if not isinstance(value, dict):
            receipt["status"] = "malformed"
        else:
            entries = value.get("receipts")
            if (value.get("version") != 1 or value.get("service_name") != "pack-host" or
                    _windows_job_drain_path_binding(value.get("support_root"), support) != "matches-runtime-support" or
                    not isinstance(entries, list)):
                receipt["status"] = "malformed"
            else:
                projected = [_windows_job_drain_entry_projection(entry, state, support)
                             for entry in entries[:64]]
                matching = state is not None and any(
                    _windows_job_drain_entry_matches(entry, state, support) for entry in entries)
                receipt.update(
                    status="valid-matching" if matching else "present-nonmatching",
                    version=1,
                    service_name="pack-host",
                    support_root_binding="matches-runtime-support",
                    receipt_count=len(entries),
                    entries=projected,
                    entries_truncated=len(entries) > len(projected),
                    consumer_contract_scope=(
                        "content/path comparison only; installed Agent ownership and file checks remain authoritative"),
                )

    native_log_dir = os.environ.get(NATIVE_RUNTIME_LOG_DIR_ENV)
    run_log_dir = run_root / "localappdata" / "Astrid Runtime" / "logs"
    logs = {
        "run_owned_runtime_service_log": _windows_job_drain_log(run_log_dir / "runtime-service.log"),
        "run_owned_runtime_log": _windows_job_drain_log(run_log_dir / "runtime.log"),
    }
    if native_log_dir:
        native_log_root = Path(native_log_dir)
        logs["native_runtime_service_log"] = _windows_job_drain_log(
            native_log_root / "runtime-service.log")
        logs["native_runtime_log"] = _windows_job_drain_log(native_log_root / "runtime.log")
    else:
        logs["native_runtime_service_log"] = {
            "status": "unavailable", "reason": "native log directory was not declared"}
        logs["native_runtime_log"] = {
            "status": "unavailable", "reason": "native log directory was not declared"}

    capture = {
        "schema_version": 1,
        "kind": "windows-runtime-job-drain-diagnostic",
        "capture_scope": "read-only bounded redacted diagnostic",
        "stale_host_registration": registration,
        "current_runtime": current_runtime,
        "cleanup_receipt": receipt,
        "previous_runtime_service_log": logs,
    }
    encoded = (json.dumps(capture, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
    if len(encoded) > WINDOWS_JOB_DRAIN_CAPTURE_LIMIT:
        capture["capture_error"] = "bounded capture exceeded its artifact limit"
        capture["previous_runtime_service_log"] = {
            key: {field: value for field, value in item.items() if field != "content"}
            for key, item in logs.items()
        }
    write_json(evidence / WINDOWS_JOB_DRAIN_CAPTURE_NAME, capture)
    return capture


def _turnover_file(path: Path, *, identities: bool = False) -> tuple[dict, dict | None]:
    """Read only a bounded, sanitized projection; hashes do not prove a writer."""
    meta, content = _windows_job_drain_file(path, limit=WINDOWS_JOB_DRAIN_CAPTURE_LIMIT)
    result = {"file": meta, "status": meta["status"]}
    if content is None:
        return result, None
    result["sha256"] = hashlib.sha256(content).hexdigest()
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        result["status"] = "malformed"
        return result, None
    if not isinstance(value, dict):
        result["status"] = "malformed"
        return result, None
    result["status"] = "captured"
    if identities:
        result.update(
            pid=_windows_job_drain_pid(value.get("pid")),
            process_birth_id=_windows_job_drain_identifier(value.get("process_birth_id"), WINDOWS_JOB_DRAIN_BIRTH_ID),
            runtime_instance_id=_windows_job_drain_identifier(value.get("runtime_instance_id"), WINDOWS_JOB_DRAIN_LAUNCH_ID),
            runtime_service_launch_id=_windows_job_drain_identifier(value.get("runtime_service_launch_id"), WINDOWS_JOB_DRAIN_LAUNCH_ID),
        )
    return result, value


def capture_windows_turnover(evidence: Path, data: Path, action: str, phase: str,
                             before: dict | None) -> dict:
    """Retain independent observations; comparison is to the pre-stop launch only."""
    if action not in {"seed", "snapshot"} or phase not in {"before-stop", "owner-absent", "after-start"}:
        raise ValueError("invalid turnover capture boundary")
    support = data / "runtime"
    registration, state = _turnover_file(support / "generic-host.json", identities=True)
    if state is not None:
        registration["support_root_binding"] = _windows_job_drain_path_binding(state.get("support_root"), support)
    readiness, _ = _turnover_file(support / "generic-host.ready.json", identities=True)
    discovery, _ = _turnover_file(support / "discovery.json", identities=True)
    instance_lock, _ = _turnover_file(support / "instance.lock", identities=True)
    receipt_path = support / "pack-host-job-cleanup.json"
    receipt, value = _turnover_file(receipt_path)
    lock_path = support / ".pack-host-job-cleanup.lock"
    lock = _windows_job_drain_file_metadata(lock_path)
    receipt["ownership"] = (_windows_job_drain_ownership(receipt_path) if receipt["file"]["status"] == "read"
                            else {"status": "not-observed"})
    lock["ownership"] = (_windows_job_drain_ownership(lock_path) if lock["status"] == "regular"
                         else {"status": "not-observed"})
    reference = state if phase == "before-stop" else before
    launch = reference.get("runtime_service_launch_id") if isinstance(reference, dict) else None
    comparison = {"status": "unavailable", "reason": "pre-stop launch ID unavailable or invalid"}
    if value is not None:
        entries = value.get("receipts")
        if (value.get("version") != 1 or value.get("service_name") != "pack-host" or
                _windows_job_drain_path_binding(value.get("support_root"), support) != "matches-runtime-support" or
                not isinstance(entries, list)):
            receipt["status"] = "malformed"
            comparison = {"status": "unavailable", "reason": "receipt malformed"}
        elif not isinstance(launch, str) or not WINDOWS_JOB_DRAIN_LAUNCH_ID.fullmatch(launch):
            comparison = {"status": "unavailable", "reason": "pre-stop launch ID unavailable or invalid"}
        else:
            exact = [entry for entry in entries if isinstance(entry, dict) and
                     entry.get("runtime_service_launch_id") == launch]
            comparison = {"status": "exact-id-present" if exact else "exact-id-not-observed",
                          "exact_id_count": len(exact),
                          "exact_id_job_drained_count": sum(e.get("job_drained") is True for e in exact)}
            receipt["receipt_count"] = len(entries)
    else:
        comparison = {"status": "unavailable", "reason": "receipt " + receipt["status"]}
    current_launch = state.get("runtime_service_launch_id") if isinstance(state, dict) else None
    registration_comparison = {"status": "unavailable"}
    if phase != "before-stop":
        if registration["status"] == "absent" and isinstance(launch, str) and WINDOWS_JOB_DRAIN_LAUNCH_ID.fullmatch(launch):
            registration_comparison = {"status": "registration-absent"}
        elif (isinstance(launch, str) and WINDOWS_JOB_DRAIN_LAUNCH_ID.fullmatch(launch) and
              isinstance(current_launch, str) and WINDOWS_JOB_DRAIN_LAUNCH_ID.fullmatch(current_launch)):
            registration_comparison = {"status": "same-launch-id" if current_launch == launch else "different-launch-id"}
    native_dir = os.environ.get(NATIVE_RUNTIME_LOG_DIR_ENV)
    logs = {}
    for name in ("runtime-service.log", "runtime.log"):
        if native_dir:
            logs[name] = _windows_job_drain_log(Path(native_dir) / name)
            if "content" in logs[name]:
                logs[name]["content"] = logs[name]["content"][-4096:]
        else:
            logs[name] = {"status": "unavailable", "reason": "native log directory was not declared"}
    capture = {"schema_version": 1, "kind": "windows-registration-turnover-diagnostic",
               "phase": phase, "action": action, "observed_at_utc": datetime.now(timezone.utc).isoformat(),
               "registration": registration, "readiness": readiness, "discovery": discovery,
               "instance_lock": instance_lock, "cleanup_receipt": receipt, "cleanup_lock": lock,
               "pre_stop_launch_receipt_comparison": comparison,
               "pre_stop_registration_comparison": registration_comparison, "native_logs": logs,
               "owner_observation": ("existing owner census returned empty immediately before capture"
                                     if phase == "owner-absent" else "not observed by this capture"),
               "writer_event": {"status": "unavailable", "reason": "no authenticated producer write hook"}}
    path = evidence / f"runtime-service-service-turnover-{action}-{phase}.log"
    if path.is_symlink():
        raise OSError("unsafe turnover artifact")
    write_json(path, capture)
    return state if phase == "before-stop" and isinstance(state, dict) else {}


def retained_pack_host_evidence_name(name: str) -> str:
    """Map a finite pack-host diagnostic source to the existing hosted pattern."""
    if name not in PACK_HOST_READINESS_EVIDENCE_NAMES:
        raise ValueError(f"Unexpected pack-host readiness evidence name: {name}")
    if name.endswith(".json"):
        name = f"{name.removesuffix('.json')}-manifest.log"
    return f"runtime-service-service-{name}"


def retain_named_evidence(evidence: Path, phase: str) -> dict[str, tuple[str, str]]:
    """Copy only the allowlisted journey evidence, clipping verbose text logs."""
    destination = evidence.parent.parent / "evidence-upload" / phase
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        print(f"Journey evidence staging ({phase}) failed: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return {}
    sources = sorted(evidence.iterdir(), key=lambda path: path.name)
    # The Release-0 assertion captures its write-once failure snapshot below
    # the leg's evidence root. Prefer that bounded source over a same-named
    # top-level file so later leg diagnostics cannot replace the R0 record.
    release0_assertion = evidence / "release0" / OWNER_ASSERTION_SNAPSHOT
    if release0_assertion.is_file() and not release0_assertion.is_symlink():
        sources = [source for source in sources if source.name != OWNER_ASSERTION_SNAPSHOT]
        sources.append(release0_assertion)
    # Windows task readiness is captured below the task's evidence directory.
    # The hosted boundary only accepts flat, finite filenames, so lift exactly
    # those known diagnostics rather than admitting arbitrary nested evidence.
    for relative_root in PACK_HOST_READINESS_NESTED_EVIDENCE_ROOTS:
        if any((evidence / Path(*relative_root.parts[:index])).is_symlink()
               for index in range(1, len(relative_root.parts) + 1)):
            continue
        source_root = evidence / relative_root
        if not source_root.is_dir() or source_root.is_symlink():
            continue
        sources.extend(source_root / name for name in sorted(PACK_HOST_READINESS_EVIDENCE_NAMES))
    turnover_root = evidence / "release0-api-seed"
    if turnover_root.is_dir() and not turnover_root.is_symlink():
        for action in ("seed", "snapshot"):
            for boundary in ("before-stop", "owner-absent", "after-start"):
                sources.append(turnover_root / f"runtime-service-service-turnover-{action}-{boundary}.log")
    retained_sources = {}
    for source in sources:
        name = source.name
        is_pack_host_diagnostic = name in PACK_HOST_READINESS_EVIDENCE_NAMES
        if (name not in JOURNEY_EVIDENCE_NAMES and not is_pack_host_diagnostic and
                not (name.startswith("runtime-service-service-") and name.endswith(".log"))):
            continue
        target_name = retained_pack_host_evidence_name(name) if is_pack_host_diagnostic else name
        if target_name in {row[0] for row in retained_sources.values()}:
            continue
        if not source.is_file() or source.is_symlink():
            continue
        target = destination / target_name
        try:
            if source.suffix == ".log":
                with source.open("rb") as stream:
                    stream.seek(max(0, source.stat().st_size - EVIDENCE_LOG_LIMIT))
                    target.write_bytes(stream.read(EVIDENCE_LOG_LIMIT))
            else:
                shutil.copyfile(source, target)
            retained_sources[name] = (target_name, source.relative_to(evidence).as_posix())
        except Exception as exc:
            print(f"Journey evidence copy ({phase}) failed for {name}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    # Release legs put their bootstrap logs below the evidence root. Preserve
    # every nested bootstrap source under a collision-safe staged name: both
    # release0 and release1 can otherwise produce identically named files.
    for source in sorted(evidence.rglob("*-bootstrap.log")):
        relative = source.relative_to(evidence)
        if len(relative.parts) < 2 or source.is_symlink() or not source.is_file():
            continue
        if any((evidence.joinpath(*relative.parts[:index])).is_symlink()
               for index in range(1, len(relative.parts))):
            continue
        # Windows Release-0 has a separately receipted, byte-for-byte copy.
        if sys.platform == "win32" and relative.as_posix() == "release0/release0-bootstrap.log":
            continue
        identity = hashlib.sha256(relative.as_posix().encode("utf-8")).hexdigest()
        target = destination / f"runtime-service-service-bootstrap-{identity}.log"
        try:
            with source.open("rb") as stream:
                stream.seek(max(0, source.stat().st_size - EVIDENCE_LOG_LIMIT))
                target.write_bytes(stream.read(EVIDENCE_LOG_LIMIT))
        except Exception as exc:
            print(f"Journey bootstrap evidence copy ({phase}) failed for {relative}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    # Cleanup may remove the release directory after the first snapshot.
    if phase == "after-cleanup":
        before = destination.parent / "before-cleanup"
        for source in sorted(before.glob("runtime-service-service-bootstrap-*.log")):
            target = destination / source.name
            if not target.exists() and source.is_file() and not source.is_symlink():
                shutil.copyfile(source, target)

    # The existing hosted boundary accepts this legacy service-log filename
    # pattern. Flatten the seed directories into it without truncating bytes.
    # Numbering follows sorted relative paths so distinct invocations survive.
    seed_dirs = sorted(path for path in evidence.glob("**/seed-diagnostics/*")
                       if len(path.name) >= 4 and path.name.isascii() and path.name.isdecimal())
    for index, directory in enumerate(seed_dirs, 1):
        if directory.is_symlink() or not directory.is_dir():
            continue
        for kind in ("input", "command", "stdout", "stderr", "exit"):
            suffix = "json" if kind in {"input", "command", "exit"} else "txt"
            source = directory / f"{kind}.{suffix}"
            if source.is_file() and not source.is_symlink():
                target = destination / f"runtime-service-service-seed-{index:04d}-{kind}.log"
                try:
                    shutil.copyfile(source, target)
                except Exception as exc:
                    print(f"Journey seed evidence copy ({phase}) failed for {source}: "
                          f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    return retained_sources


def snapshot_failure_evidence(evidence: Path, run_root: Path, data: Path, phase: str) -> None:
    """Stage named diagnostics and bind their availability to the journey receipt."""
    result_path = evidence / "journey-result.json"
    try:
        recorded = read_json(result_path) if result_path.is_file() else {}
        job_drain_capture = None
        if sys.platform == "win32" and recorded.get("scenario") == "release0-to-release1":
            try:
                job_drain_capture = capture_windows_job_drain_evidence(evidence, run_root, data)
            except Exception as exc:
                job_drain_capture = {"capture_error": type(exc).__name__}
        capture_runtime_service_logs(evidence, run_root, phase)
        capture_manager_state(evidence, data, phase)
        retained_sources = retain_named_evidence(evidence, phase)
        destination = evidence.parent.parent / "evidence-upload" / phase
        diagnostic = {}
        for key, name in (("install_log", "install.log"),
                          ("service_manager", f"manager-state-{phase}.json")):
            path = destination / name
            item = {"status": "captured" if path.is_file() else "unavailable",
                    "path": path.relative_to(run_root).as_posix() if path.is_file() else None}
            if key == "service_manager" and path.is_file():
                manager = read_json(path)
                item["observation_status"] = "error" if manager.get("error") else "observed"
                if manager.get("error"):
                    item["error"] = manager["error"]
            diagnostic[key] = item
        if sys.platform == "win32":
            pack_host = []
            for observation in PACK_HOST_READINESS_OBSERVATIONS:
                source_name = f"pack-host-readiness-{observation}.json"
                retained = retained_sources.get(source_name)
                target_name = retained[0] if retained is not None else retained_pack_host_evidence_name(source_name)
                path = destination / target_name
                item = {
                    "observation": observation,
                    "status": "captured" if path.is_file() else "unavailable",
                    "path": path.relative_to(run_root).as_posix() if path.is_file() else None,
                }
                if path.is_file():
                    item["source_path"] = retained[1] if retained is not None else None
                    try:
                        captured = read_json(path)
                        item["capture_status"] = ("observed" if captured.get("schema_version") == 1
                                                  else "malformed")
                        for log in captured.get("logs", []):
                            source = log.get("evidence") if isinstance(log, dict) else None
                            log_retention = retained_sources.get(source)
                            if log_retention is not None:
                                log["retained_evidence_path"] = (
                                    destination / log_retention[0]).relative_to(run_root).as_posix()
                        item["capture"] = captured
                    except (OSError, ValueError, json.JSONDecodeError):
                        item["capture_status"] = "malformed"
                pack_host.append(item)
            diagnostic["pack_host_readiness"] = pack_host
            if recorded.get("scenario") == "release0-to-release1":
                path = destination / WINDOWS_JOB_DRAIN_CAPTURE_NAME
                receipt = (job_drain_capture or {}).get("cleanup_receipt", {})
                diagnostic["windows_job_drain"] = {
                    "status": "captured" if path.is_file() else "unavailable",
                    "path": path.relative_to(run_root).as_posix() if path.is_file() else None,
                    "cleanup_receipt_status": receipt.get("status"),
                    "stale_host_registration_status": (job_drain_capture or {}).get(
                        "stale_host_registration", {}).get("status"),
                }
                if job_drain_capture and job_drain_capture.get("capture_error"):
                    diagnostic["windows_job_drain"]["capture_error"] = job_drain_capture["capture_error"]
        if sys.platform == "win32" and recorded.get("scenario") == "release0-to-release1":
            # This log is nested under release0, and must remain byte-for-byte
            # intact rather than taking the usual bounded tail of text logs.
            source = evidence / "release0" / "release0-bootstrap.log"
            exit_path = evidence / "release0" / "release0-bootstrap-exit.json"
            previous = recorded.get("failure_diagnostics", {}).get("before-cleanup", {}).get(
                "release0_bootstrap", {})
            exit_code = previous.get("exit_code")
            if exit_path.is_file() and not exit_path.is_symlink():
                exit_code = read_json(exit_path).get("exit_code")
            if not source.is_file() or source.is_symlink():
                source = destination.parent / "before-cleanup" / "release0-bootstrap.log"
            target = destination / "release0-bootstrap.log"
            item = {"status": "unavailable", "path": None, "exit_code": exit_code, "sha256": None}
            if source.is_file() and not source.is_symlink():
                if source != target:
                    shutil.copyfile(source, target)
                digest = hashlib.sha256()
                with target.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                item.update(status="captured", path=target.relative_to(run_root).as_posix(),
                            sha256=digest.hexdigest())
            diagnostic["release0_bootstrap"] = item
        bootstrap_paths = sorted(
            list(destination.glob("*-bootstrap.log")) +
            list(destination.glob("runtime-service-service-bootstrap-*.log")))
        diagnostic["bootstrap"] = {
            "status": "captured" if bootstrap_paths else "unavailable",
            "paths": [path.relative_to(run_root).as_posix() for path in bootstrap_paths],
            "source_paths": [source.relative_to(evidence).as_posix()
                             for source in sorted(evidence.rglob("*-bootstrap.log"))
                             if source.is_file() and not source.is_symlink()],
        }
        recorded.setdefault("failure_diagnostics", {})[phase] = diagnostic
        write_json(result_path, recorded)
        # Refresh the staged receipt after adding paths and observation status.
        retain_named_evidence(evidence, phase)
    except Exception as exc:
        print(f"Journey evidence snapshot ({phase}) failed: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)


def _under(path: Path, parent: Path) -> bool:
    return path != parent and path.is_relative_to(parent)


def run_scope(run_root: Path, root: Path, data: Path, leg: str, *,
              diagnostic_start_at: str | None = None) -> tuple[Path, Path, Path, Path]:
    """Fail closed before a lifecycle command can touch login-managed state."""
    if not all(path.is_absolute() for path in (run_root, root, data)):
        raise ValueError("--run-root, --root and --data-root must be absolute")
    if any(path != path.resolve() for path in (run_root, root, data)):
        raise ValueError("Journey roots cannot contain symlinks or path aliases")
    temporary = Path(tempfile.gettempdir()).resolve()
    runner_temp = Path(os.environ["RUNNER_TEMP"]).resolve() if os.environ.get("RUNNER_TEMP") else temporary
    run_root = run_root.resolve()
    if not any(_under(run_root, allowed) for allowed in (temporary, runner_temp)):
        raise ValueError("--run-root must be a child of the runner temporary directory")
    if root.resolve() != run_root / "program" or data.resolve() != run_root / "data":
        raise ValueError("--root and --data-root must be the run-owned program/data children")
    root, data = root.resolve(), data.resolve()
    owner_path = run_root / OWNER_FILE
    if diagnostic_start_at is not None:
        if diagnostic_start_at != "release0-api-seed":
            raise ValueError("Unknown diagnostic start stage")
        if owner_path.is_symlink():
            raise ValueError("Journey ownership record cannot be a symlink")
        owner = read_json(owner_path)
        if (owner.get("schema_version") != 1 or
                not re.fullmatch(r"[0-9a-f]{32}", str(owner.get("run_id", ""))) or
                owner.get("root") != str(root) or owner.get("data_root") != str(data)):
            raise ValueError("Existing journey roots lack this run's ownership record")
    elif leg == "fresh-install" or leg in VERSION_JUMP_LEGS:
        if run_root.exists() and any(run_root.iterdir()):
            raise ValueError("A fresh journey requires an empty run-owned directory")
        run_root.mkdir(parents=True, exist_ok=True)
        with owner_path.open("x", encoding="utf-8") as stream:
            json.dump({"schema_version": 1, "run_id": uuid.uuid4().hex,
                       "root": str(root), "data_root": str(data)}, stream)
            stream.write("\n")
    else:
        if owner_path.is_symlink():
            raise ValueError("Journey ownership record cannot be a symlink")
        owner = read_json(owner_path)
        if (owner.get("schema_version") != 1 or
                not re.fullmatch(r"[0-9a-f]{32}", str(owner.get("run_id", ""))) or
                owner.get("root") != str(root) or owner.get("data_root") != str(data)):
            raise ValueError("Existing journey roots lack this run's ownership record")
    for scoped in (run_root / "home", run_root / "localappdata", run_root / "appdata",
                   run_root / "tmp", run_root / "home" / ".config",
                   run_root / "home" / ".local", run_root / "home" / ".local" / "share"):
        if scoped != scoped.resolve():
            raise ValueError("Journey environment path cannot alias another installation")
    evidence_leg = (f"{leg}-diagnostic-{diagnostic_start_at}"
                    if diagnostic_start_at is not None else leg)
    evidence = run_root / "evidence" / evidence_leg
    if evidence != evidence.resolve():
        raise ValueError("Journey evidence cannot alias another path")
    return run_root, root, data, evidence


@contextmanager
def isolated_environment(run_root: Path, root: Path, data: Path):
    """Keep bootstrap state, selection records and platform data under this run."""
    home = run_root / "home"
    local = run_root / "localappdata"
    for path in (home, local, run_root / "tmp"):
        path.mkdir(parents=True, exist_ok=True)
    changes = {"HOME": str(home), "USERPROFILE": str(home), "LOCALAPPDATA": str(local),
               "APPDATA": str(run_root / "appdata"), "TMP": str(run_root / "tmp"),
               "TEMP": str(run_root / "tmp"), "TMPDIR": str(run_root / "tmp"),
               "XDG_DATA_HOME": str(home / ".local" / "share"),
               "XDG_STATE_HOME": str(home / ".local" / "state"),
               "XDG_CONFIG_HOME": str(home / ".config"),
               "ASTRID_RUNTIME_DATA_ROOT": str(data),
               "ASTRID_CI_PROGRAM_ROOT": str(root), "ASTRID_CI_DATA_ROOT": str(data)}
    if sys.platform == "win32":
        # Task Scheduler builds the service process environment from the login
        # session, not from these process-local isolation overrides. Preserve
        # that native Runtime log root so failure diagnostics read the same
        # service-pack-host.log that frozen R0 actually writes.
        native_local = os.environ.get("LOCALAPPDATA")
        if native_local:
            changes[NATIVE_RUNTIME_LOG_DIR_ENV] = str(
                Path(native_local) / "Astrid Runtime" / "logs"
            )
    prior = {key: os.environ.get(key) for key in changes}
    os.environ.update(changes)
    try:
        yield
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def process_birth(pid: int) -> str | None:
    if sys.platform == "win32":
        output = windows.powershell(
            f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; "
            "if ($p) { $p.CreationDate.ToUniversalTime().ToString('o') }",
            "sentinel-birth.txt", show=False, timeout=30)
        return output.strip() or None
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                            capture_output=True, text=True, timeout=10, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def sentinel_alive(process: subprocess.Popen, birth: str) -> bool:
    return sentinel_observation(process, birth)["alive"]


def sentinel_observation(process: subprocess.Popen, birth: str) -> dict:
    """Keep exit and OS birth checks distinct in the small result receipt."""
    exit_code = process.poll()
    try:
        observed_birth = process_birth(process.pid)
        birth_error = None
    except (OSError, subprocess.SubprocessError) as exc:
        observed_birth = None
        birth_error = f"{type(exc).__name__}: {exc}"
    observation = {"pid": process.pid, "exit_code": exit_code,
                   "observed_birth": observed_birth,
                   "alive": exit_code is None and observed_birth == birth}
    if birth_error:
        observation["birth_error"] = birth_error
    return observation


@contextmanager
def sentinel_process(run_root: Path, evidence: Path | None = None):
    """A harmless unrelated process, terminated only while its birth still matches."""
    token = uuid.uuid4().hex
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", "import time; time.sleep(7200)", token],
        cwd=run_root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=os.name == "posix")
    entered = False
    try:
        birth = process_birth(process.pid)
        if not birth or not sentinel_alive(process, birth):
            raise RuntimeError("Could not establish the sentinel process birth identity")
        entered = True
        yield process, birth
    finally:
        primary = sys.exc_info()[1]
        try:
            # Popen retains this child handle; poll() cannot mistake a reused PID for it.
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
        except Exception as cleanup_error:
            if primary is None:
                raise
            if not entered:
                print(f"Journey failed: {type(primary).__name__}: {primary}", file=sys.stderr, flush=True)
            print(f"Journey sentinel cleanup failed: {type(cleanup_error).__name__}: {cleanup_error}",
                  file=sys.stderr, flush=True)
            if evidence is not None:
                try:
                    result_path = evidence / "journey-result.json"
                    recorded = read_json(result_path) if result_path.is_file() else {}
                    recorded.update(status="fail", sentinel_cleanup_error=
                                    f"{type(cleanup_error).__name__}: {cleanup_error}")
                    write_json(result_path, recorded)
                    retain_named_evidence(evidence, "after-cleanup")
                except Exception as receipt_error:
                    print(f"Journey sentinel cleanup receipt failed: {type(receipt_error).__name__}: {receipt_error}",
                          file=sys.stderr, flush=True)


def git_source(checkout: Path) -> dict:
    checkout = checkout.resolve(strict=True)
    sha = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"Not a full Git commit: {checkout}")
    return {"sha": sha, "repo": str(checkout)}


def selected_manifest(app: Path, *, source_override: bool, agent: Path | None,
                      runtime: Path | None, evidence: Path) -> tuple[Path, dict]:
    shipped = app / "public" / "install-manifest.json"
    original = shipped.read_bytes()
    manifest = json.loads(original)
    if not isinstance(manifest, dict):
        raise ValueError("The shipped install manifest is not an object")
    if source_override:
        if agent is None or runtime is None:
            raise ValueError("--source-override requires --agent-checkout and --runtime-checkout")
        manifest = {**manifest, "agent": git_source(agent), "runtime": git_source(runtime)}
        selected = evidence / "source-override-manifest.json"
        write_json(selected, manifest)
    else:
        if agent is not None or runtime is not None:
            raise ValueError("Checkout sources require the explicitly labelled --source-override test mode")
        selected = shipped
        if selected.read_bytes() != original:
            raise RuntimeError("The shipped manifest changed during selection")
    return selected, manifest


def program_root() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "Astrid"
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "astrid"


def windows_task_owned(output: str, root: Path) -> bool:
    lines = [line.strip().strip('"') for line in output.splitlines() if line.strip()]
    if not lines or lines[0] != "present" or len(lines) != 2:
        return False
    root_prefix = ntpath.normcase(str(root).rstrip("\\/") + "\\")
    return ntpath.normcase(lines[1].replace("/", "\\")).startswith(root_prefix)


def assert_host_available(leg: str, root: Path, data: Path) -> None:
    """Reject an existing real login install before changing any service."""
    default = program_root()
    selection_path = default.parent / "astrid-install.json"
    selection = read_json(selection_path) if selection_path.exists() else None
    owned_uninstalled = (leg == "cleanup" and isinstance(selection, dict)
                         and selection.get("owner") == "haus.astrid.runtime"
                         and selection.get("program_root") == str(root)
                         and selection.get("data_root") == str(data)
                         and selection.get("uninstalled") is True
                         and not selection.get("foreground"))
    if ((default / "install.json").exists() or (default / "current").exists()
            or (selection is not None and not owned_uninstalled)):
        raise RuntimeError("A non-journey Astrid login installation is present")
    fresh = leg in {"fresh-install", "cleanup"}
    if sys.platform == "darwin":
        if (Path.home() / "Library" / "LaunchAgents" / "haus.astrid.runtime.plist").exists():
            raise RuntimeError("A non-journey Astrid LaunchAgent is present")
        manager = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/haus.astrid.runtime"],
                                 capture_output=True, text=True, timeout=10, check=False)
        if manager.returncode == 0:
            expected = str(data.parent / "home" / "Library" / "LaunchAgents" /
                           "haus.astrid.runtime.plist")
            loaded_path = re.search(r"^\s*path\s*=\s*(.+?)\s*$", manager.stdout, re.MULTILINE)
            if fresh or loaded_path is None or loaded_path.group(1) != expected:
                raise RuntimeError("An Astrid LaunchAgent outside this journey is registered")
    elif sys.platform == "linux":
        config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        if (config / "systemd" / "user" / "astrid.service").exists():
            raise RuntimeError("A non-journey Astrid user service is present")
        try:
            manager = subprocess.run(["systemctl", "--user", "show", "astrid.service",
                                      "--property=LoadState,FragmentPath"],
                                     capture_output=True, text=True, timeout=10, check=False)
        except FileNotFoundError:
            manager = None
        if manager is not None and manager.returncode == 0:
            fields = dict(line.split("=", 1) for line in manager.stdout.splitlines() if "=" in line)
            if fields.get("LoadState") not in {None, "not-found"}:
                expected = str(data.parent / "home" / ".config" / "systemd" / "user" /
                               "astrid.service")
                if fresh or fields.get("FragmentPath") != expected:
                    raise RuntimeError("An Astrid user-manager unit outside this journey is loaded")
    elif sys.platform == "win32":
        existing = windows.scheduled_task_diagnostics(leg=leg)
        if existing["status"] == "present":
            tasks = existing["tasks"]
            owned = (len(tasks) == 1 and len(tasks[0]["executables"]) == 1
                     and windows_task_owned("present\n" + tasks[0]["executables"][0], root))
            if fresh or not owned:
                raise RuntimeError("An Astrid Scheduled Task outside this journey is registered")
    if fresh and runtime_processes(data):
        raise RuntimeError("A Runtime owner is already running on this host")


def _manager_probe(argv: list[str], *, label: str) -> str:
    """Return a native manager version/probe line or fail before bootstrap.

    The direct Release-1 leg has no foreground substitute.  In particular, a
    missing user systemd session is a prerequisite failure, not a reason to let
    the product's compatibility foreground branch make the receipt look green.
    """
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                                   check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required native service manager {label} is unavailable") from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        suffix = f": {detail[0]}" if detail else ""
        raise RuntimeError(f"Required native service manager {label} is unusable{suffix}")
    line = next((line.strip() for line in (completed.stdout or completed.stderr).splitlines()
                 if line.strip()), "native-manager")
    return line


def release1_manager_prerequisite(args: argparse.Namespace, evidence: Path) -> dict:
    """Assert and record the declared owner before any lifecycle command runs."""
    job_id = getattr(args, "job_id", None)
    if not job_id:
        return {"required": False, "outcome": "not-required"}
    row = validate_job(job_id, args.mode)
    if row["mode"] != "direct":
        checkout = getattr(args, "pinokio_checkout", None)
        if checkout is None or not checkout.is_dir() or checkout.is_symlink():
            raise RuntimeError("Pinokio modes require an explicit non-symlink Pinokio checkout")
        return {"required": False, "outcome": "explicit-owner", "managed_by": "pinokio",
                "foreground_fallback": False, "checkout": str(checkout.resolve())}
    if sys.platform == "linux":
        version = _manager_probe(["systemctl", "--user", "--version"], label="systemd --user")
        matched = re.search(r"\bsystemd\s+(\d+)\b", version)
        if matched is None or int(matched.group(1)) < 254:
            raise RuntimeError("Required native service manager systemd --user must be version 254 or newer")
        _manager_probe(["systemctl", "--user", "show-environment"], label="systemd --user session")
        manager = "systemd-user"
    elif sys.platform == "darwin":
        version = _manager_probe(["launchctl", "version"], label="launchd")
        _manager_probe(["launchctl", "print", f"gui/{os.getuid()}"], label="launchd GUI domain")
        manager = "launchd"
    elif sys.platform == "win32":
        # Retain the query even if the subsequent manager prerequisite fails.
        windows.scheduled_task_preflight(args.leg)
        # The command checks both the PowerShell service-manager module and its
        # scheduler surface; it does not manufacture or start a task.
        version = windows.powershell(
            "$ErrorActionPreference='Stop'; Get-Command Get-ScheduledTask | Out-Null; "
            "$PSVersionTable.PSVersion.ToString()", "service-manager-prerequisite.log",
            show=False, timeout=30).strip()
        if not version:
            raise RuntimeError("Required native service manager Task Scheduler has no version observation")
        manager = "task-scheduler"
    else:
        raise RuntimeError(f"Unsupported service-manager platform: {sys.platform}")
    return {"required": True, "outcome": "pass", "manager": manager, "version": version,
            "foreground_fallback": False, "evidence": str(evidence / "service-manager-prerequisite.log")}


def assert_direct_service_ownership(root: Path, data: Path) -> dict:
    """Prove a direct leg selected a native service, never a foreground fallback."""
    selection_path = program_root().parent / "astrid-install.json"
    if selection_path.is_file() and read_json(selection_path).get("foreground"):
        raise RuntimeError("Direct journey selected a forbidden foreground fallback")
    if sys.platform == "linux":
        unit = data.parent / "home" / ".config" / "systemd" / "user" / "astrid.service"
        if not unit.is_file() or unit.is_symlink():
            raise RuntimeError("Direct journey did not install an owned systemd user unit")
        completed = subprocess.run(["systemctl", "--user", "show", "astrid.service",
                                    "--property=LoadState,FragmentPath,ActiveState"], capture_output=True,
                                   text=True, timeout=30, check=False)
        fields = dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)
        if (completed.returncode or fields.get("LoadState") == "not-found"
                or fields.get("FragmentPath") != str(unit)
                or fields.get("ActiveState") != "active"):
            raise RuntimeError("Direct journey service is not owned by the expected systemd user unit")
        return {"manager": "systemd-user", "service_path": str(unit), "foreground_fallback": False}
    if sys.platform == "darwin":
        plist = data.parent / "home" / "Library" / "LaunchAgents" / "haus.astrid.runtime.plist"
        if not plist.is_file() or plist.is_symlink():
            raise RuntimeError("Direct journey did not install an owned LaunchAgent")
        completed = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/haus.astrid.runtime"],
                                   capture_output=True, text=True, timeout=30, check=False)
        if completed.returncode:
            raise RuntimeError("Direct journey LaunchAgent is not loaded in the GUI domain")
        return {"manager": "launchd", "service_path": str(plist), "foreground_fallback": False}
    if sys.platform == "win32":
        existing = windows.powershell(
            "$t=Get-ScheduledTask -TaskName 'Astrid Runtime' -ErrorAction Stop; "
            "'present'; $t.Actions | ForEach-Object { $_.Execute }",
            "direct-service-ownership.log", show=False, timeout=30)
        if not windows_task_owned(existing, root):
            raise RuntimeError("Direct journey Task Scheduler action is not owned by the run root")
        return {"manager": "task-scheduler", "foreground_fallback": False}
    raise RuntimeError(f"Unsupported service-manager platform: {sys.platform}")


def candidate_checkouts(args: argparse.Namespace) -> dict | None:
    """Record and verify all four immutable candidate checkouts when supplied."""
    candidate_path = getattr(args, "candidate", None)
    if candidate_path is None:
        return None
    candidate = read_json(candidate_path)
    if set(candidate) < {"app", "agent", "runtime", "pinokio"}:
        raise ValueError("candidate must include app, agent, runtime and pinokio refs")
    checkouts = {"app": args.app_checkout,
                 "agent": getattr(args, "candidate_agent_checkout", None),
                 "runtime": getattr(args, "candidate_runtime_checkout", None),
                 "pinokio": getattr(args, "pinokio_checkout", None)}
    observed = {}
    for name, checkout in checkouts.items():
        if checkout is None or not checkout.is_dir() or checkout.is_symlink():
            raise ValueError(f"candidate {name} checkout is required and must not be a symlink")
        source = git_source(checkout)
        if source["sha"] != candidate[name]:
            raise ValueError(f"candidate {name} checkout differs from frozen candidate")
        observed[name] = source
    return observed


def installation(root: Path, expected_data: Path) -> tuple[Path, Path, dict]:
    marker_path = root / "install.json"
    if marker_path.is_symlink():
        raise RuntimeError("Install marker cannot be a symlink")
    marker = read_json(marker_path)
    data = Path(marker["data_root"])
    if data != expected_data:
        raise RuntimeError("Selected data root is outside this journey")
    current_path = root / "current"
    if current_path.is_symlink():
        raise RuntimeError("Selected version identifier is not run-owned")
    current = current_path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", current):
        raise RuntimeError("Selected version identifier is not run-owned")
    version_path = root / "versions" / current / "version.json"
    if not version_path.resolve().is_relative_to(root / "versions"):
        raise RuntimeError("Selected version path escapes this journey")
    version = read_json(version_path)
    if Path(version["root"]).resolve() != version_path.parent.resolve():
        raise RuntimeError("Selected version path disagrees with version.json")
    return version_path, data, version


def run(argv: list[str | Path], log: Path, *, env: dict | None = None, timeout: int = 3600,
        legacy_bootstrap: bool = False) -> str:
    log.parent.mkdir(parents=True, exist_ok=True)
    parts = [str(part) for part in argv]
    if (log.name == "seed.json" and len(parts) >= 4 and
            Path(parts[2]).name == "journey_workspace.py" and parts[3] == "seed"):
        return run_seed(parts, log, env=env, timeout=timeout)
    with log.open("w", encoding="utf-8") as stream:
        if legacy_bootstrap and sys.platform == "win32":
            child_env = dict(os.environ if env is None else env)
            inherited = next((value for key, value in child_env.items() if key.casefold() == "psmodulepath"), None)
            diagnostic = json.dumps({"bootstrap": log.name, "PSModulePath": inherited,
                                     "PSEdition": child_env.get("JOURNEY_LAUNCHER_PSEDITION")})
            with (log.parent.parent / "windows-bootstrap-environment.log").open("a", encoding="utf-8") as diagnostic_stream:
                diagnostic_stream.write(diagnostic + "\n")
            print(f"Windows bootstrap prelaunch: {diagnostic}", flush=True)
            child_env = {key: value for key, value in child_env.items() if key.casefold() != "psmodulepath"}
            windows_env = {key.casefold(): value for key, value in child_env.items()}
            child_env["PSModulePath"] = ";".join((
                ntpath.join(windows_env["programfiles"], "WindowsPowerShell", "Modules"),
                ntpath.join(windows_env["systemroot"], "System32", "WindowsPowerShell", "v1.0", "Modules"),
            ))
            env = child_env
        result = subprocess.run([str(part) for part in argv], env=env, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=timeout, check=False)
    if sys.platform == "win32" and log.name == "release0-bootstrap.log" and log.parent.name == "release0":
        try:
            write_json(log.parent / "release0-bootstrap-exit.json", {"exit_code": result.returncode})
        except OSError as exc:
            print(f"Journey bootstrap exit capture failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
    output = log.read_text(encoding="utf-8-sig", errors="replace")
    if result.returncode:
        raise RuntimeError(f"{log.name}: exit {result.returncode}; see {log}")
    return output


def run_seed(argv: list[str], log: Path, *, env: dict | None, timeout: int) -> str:
    """Preserve the exact seed request, argv and separate byte streams per call."""
    directory = log.parent / "seed-diagnostics"
    directory.mkdir(parents=True, exist_ok=True)
    index = 1
    while True:
        invocation = directory / f"{index:04d}"
        try:
            invocation.mkdir()
            break
        except FileExistsError:
            index += 1
    request = Path(argv[argv.index("--request") + 1])
    shutil.copyfile(request, invocation / "input.json")
    write_json(invocation / "command.json", argv)
    status = {"exit_code": None}
    try:
        with ((invocation / "stdout.txt").open("wb") as stdout,
              (invocation / "stderr.txt").open("wb") as stderr):
            result = subprocess.run(argv, env=env, stdout=stdout, stderr=stderr,
                                    timeout=timeout, check=False)
        status["exit_code"] = result.returncode
    except Exception as exc:
        status["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        write_json(invocation / "exit.json", status)
        shutil.copyfile(invocation / "stdout.txt", log)
    output = log.read_text(encoding="utf-8-sig", errors="replace")
    if result.returncode:
        raise RuntimeError(f"{log.name}: exit {result.returncode}; see {invocation}")
    return output


def bootstrap(app: Path, manifest_path: Path, shell: str, mode: str,
              evidence: Path, root: Path, data: Path, legacy_bootstrap: bool = False) -> None:
    flags: list[str | Path] = ["--manifest", manifest_path, "--root", root,
                               "--data-root", data]
    if mode == "update":
        flags.append("--update")
    env = dict(os.environ)
    env["BROWSER"] = f'"{sys.executable}" -c "pass" %s'
    if sys.platform == "win32":
        run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", app / "public" / "install.ps1", *flags], evidence / f"{mode}-bootstrap.log",
            env=env, legacy_bootstrap=legacy_bootstrap)
    else:
        run([shell, app / "public" / "install", *flags],
            evidence / f"{mode}-bootstrap.log", env=env)


def verify(mode: str, version_path: Path, data: Path, evidence: Path,
           *, before: Path | None = None) -> Path:
    output = evidence / f"verify-{mode}.json"
    argv: list[str | Path] = [sys.executable, ROOT / "scripts" / "verify_installed.py",
                              "--mode", mode, "--version", version_path,
                              "--data-root", data, "--output", output]
    if before is not None:
        argv += ["--before", before]
    run(argv, evidence / f"verify-{mode}.log", timeout=180)
    if not output.is_file():
        raise RuntimeError(f"Shared verifier wrote no receipt: {output}")
    return output


def installed_cli(version: dict, argv: list[str], evidence: Path, name: str,
                  *, module: str = "astrid", timeout: int = 300) -> str:
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(version["env"])
    return run([version["python"], "-I", "-m", module, *argv], evidence / name,
               env=env, timeout=timeout)


def healthy(data: Path) -> dict:
    request = urllib.request.Request(HEALTH_URL, headers={"Origin": "https://astrid.haus"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
        health = json.load(response)
    discovery = read_json(data / "runtime" / "discovery.json")
    if (health.get("status") != "ok" or not health.get("runtime_instance_id") or
            health["runtime_instance_id"] != discovery.get("runtime_instance_id")):
        raise RuntimeError("Live Runtime health does not match selected discovery")
    return discovery


def _runtime_owner_command(command: str) -> bool:
    # Installed services run the Runtime CLI through a Python -c prelude. Keep
    # recognizing the older runtime_protocol form for other running owners.
    return ("-m runtime_protocol start" in command or
            (re.search(r"(?:^|\s)-c(?:\s|$)", command) is not None and
             "from astrid_runtime.cli import main" in command and
             re.search(r"main\(\s*\[\s*['\"]run['\"]", command) is not None))


class _RuntimeOwnerPids(list):
    """PID list carrying bounded identity evidence for a failed strict census."""

    def __init__(self, pids: list[int], details: list[dict], task_action: dict | None = None):
        super().__init__(pids)
        self.details = details
        # The census command returns only redacted task-action facts: its
        # executable image and a digest/count for the parsed arguments.  The
        # command itself and its arguments can carry secrets and must not be
        # retained in journey evidence.
        self.task_action = task_action


def _bounded_text(value: object, *, limit: int) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    text = str(value)
    if not text:
        return None, False
    return text[:limit], len(text) > limit


def _runtime_owner_detail(row: dict, command: str, matcher: str | None = None) -> dict:
    """Retain bounded owner identity fields without copying a service prelude."""
    matcher = matcher or ("runtime_protocol-start" if "-m runtime_protocol start" in command
                          else "astrid_runtime-cli-run")
    command_line, command_line_truncated = _bounded_text(
        row.get("SanitizedCommandLine"), limit=512)
    if command_line is None:
        # This fallback deliberately describes only the recognized owner form,
        # never the untrusted raw CommandLine queried from Windows.
        command_line = ("-m runtime_protocol start" if matcher == "runtime_protocol-start"
                        else "-c <astrid_runtime.cli main(['run', ...])>")
    executable_path, executable_path_truncated = _bounded_text(row.get("ExecutablePath"), limit=1024)
    creation_date, creation_date_truncated = _bounded_text(row.get("CreationDate"), limit=128)
    native_identity, native_identity_truncated = _bounded_text(row.get("NativeIdentity"), limit=128)
    command_sha256, _ = _bounded_text(row.get("CommandSha256"), limit=64)
    command_tail_sha256, _ = _bounded_text(row.get("CommandTailSha256"), limit=64)
    if command_sha256 is None and command:
        command_sha256 = hashlib.sha256(command.encode("utf-8", errors="replace")).hexdigest()
    command_length = row.get("CommandLength")
    command_tail_argument_count = row.get("CommandTailArgumentCount")
    return {
        "command_line": command_line,
        "command_line_truncated": command_line_truncated,
        "command_length": command_length if type(command_length) is int and command_length >= 0 else None,
        "command_sha256": command_sha256,
        "command_tail_sha256": command_tail_sha256,
        "command_tail_argument_count": (
            command_tail_argument_count
            if type(command_tail_argument_count) is int and command_tail_argument_count >= 0 else None),
        "registered_arguments_match": row.get("RegisteredArgumentsMatch") is True,
        "creation_date": creation_date,
        "creation_date_truncated": creation_date_truncated,
        "executable_path": executable_path,
        "executable_path_truncated": executable_path_truncated,
        "matcher": matcher,
        "native_identity": native_identity,
        "native_identity_truncated": native_identity_truncated,
        "parent_pid": int(row["ParentProcessId"]) if row.get("ParentProcessId") is not None else None,
        "pid": int(row["ProcessId"]),
    }


WINDOWS_RUNTIME_OWNER_CENSUS = r"""
$nativeIdentityAvailable = $false
try {
    Add-Type -TypeDefinition @'
using System;
using System.Globalization;
using System.Runtime.InteropServices;
namespace AstridOwnerCensus {
  public static class Native {
    [StructLayout(LayoutKind.Sequential)] private struct FILETIME {
      public uint low; public uint high;
    }
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern bool GetProcessTimes(IntPtr handle, out FILETIME created,
      out FILETIME exited, out FILETIME kernel, out FILETIME user);
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern bool CloseHandle(IntPtr handle);
    [DllImport("shell32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern IntPtr CommandLineToArgvW(string commandLine, out int argc);
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern IntPtr LocalFree(IntPtr memory);
    public static string CreationFileTime(uint pid) {
      IntPtr handle = OpenProcess(0x1000, false, pid);
      if (handle == IntPtr.Zero) return null;
      try {
        FILETIME created, exited, kernel, user;
        if (!GetProcessTimes(handle, out created, out exited, out kernel, out user)) return null;
        ulong value = ((ulong)created.high << 32) | created.low;
        return value.ToString(CultureInfo.InvariantCulture);
      } finally { CloseHandle(handle); }
    }
    public static string[] CommandLineArguments(string commandLine) {
      if (String.IsNullOrEmpty(commandLine)) return null;
      int argc;
      IntPtr values = CommandLineToArgvW(commandLine, out argc);
      if (values == IntPtr.Zero || argc < 1) return null;
      try {
        string[] result = new string[argc];
        for (int index = 0; index < argc; index++) {
          IntPtr value = Marshal.ReadIntPtr(values, index * IntPtr.Size);
          result[index] = Marshal.PtrToStringUni(value) ?? String.Empty;
        }
        return result;
      } finally { LocalFree(values); }
    }
    public static string ArgumentVectorSha256(string[] values) {
      if (values == null) return null;
      using (var stream = new System.IO.MemoryStream()) {
        foreach (string value in values) {
          byte[] bytes = System.Text.Encoding.UTF8.GetBytes(value ?? String.Empty);
          byte[] length = BitConverter.GetBytes(bytes.Length);
          stream.Write(length, 0, length.Length);
          stream.Write(bytes, 0, bytes.Length);
        }
        using (var hash = System.Security.Cryptography.SHA256.Create()) {
          return BitConverter.ToString(hash.ComputeHash(stream.ToArray())).Replace("-", "").ToLowerInvariant();
        }
      }
    }
  }
}
'@ -ErrorAction Stop
    $nativeIdentityAvailable = $true
} catch {}
$sha256 = [Security.Cryptography.SHA256]::Create()
try {
    # This PowerShell process carries the census source in its own command
    # line, including the owner matcher literals below.  Do not count it as a
    # Runtime owner; all other live processes remain in the census.
    $ownerTails = @{}
    $owners = @(Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID } | ForEach-Object {
        $command = [string]$_.CommandLine
        $matcher = $null
        if ($command.Contains('-m runtime_protocol start')) {
            $matcher = 'runtime_protocol-start'
        } elseif ($command -cmatch '(?s)(?:^|\s)-c(?:\s|$)' -and
                  $command.Contains('from astrid_runtime.cli import main') -and
                  $command -cmatch 'main\(\s*\[\s*[''"]run[''"]') {
            $matcher = 'astrid_runtime-cli-run'
        }
        if ($null -ne $matcher) {
            $creation = $null
            if ($null -ne $_.CreationDate) {
                try { $creation = $_.CreationDate.ToUniversalTime().ToString('o') } catch {}
            }
            $native = $null
            if ($nativeIdentityAvailable) {
                try {
                    $created = [AstridOwnerCensus.Native]::CreationFileTime([uint32]$_.ProcessId)
                    if ($null -ne $created) { $native = 'windows-creation:' + $created }
                } catch {}
            }
            $tailHash = $null
            $tailCount = $null
            try {
                $argv = [AstridOwnerCensus.Native]::CommandLineArguments($command)
                if ($null -ne $argv -and $argv.Length -ge 1) {
                    $tail = @($argv | Select-Object -Skip 1)
                    $tailHash = [AstridOwnerCensus.Native]::ArgumentVectorSha256($tail)
                    $tailCount = $tail.Length
                    $ownerTails[[string]$_.ProcessId] = $tail
                }
            } catch {}
            [pscustomobject]@{
                ProcessId = $_.ProcessId
                ParentProcessId = $_.ParentProcessId
                CreationDate = $creation
                NativeIdentity = $native
                ExecutablePath = $_.ExecutablePath
                Matcher = $matcher
                CommandLength = $command.Length
                CommandSha256 = ([BitConverter]::ToString($sha256.ComputeHash(
                    [Text.Encoding]::UTF8.GetBytes($command))).Replace('-', '').ToLowerInvariant())
                CommandTailArgumentCount = $tailCount
                CommandTailSha256 = $tailHash
                SanitizedCommandLine = if ($matcher -eq 'runtime_protocol-start') {
                    '-m runtime_protocol start'
                } else {
                    "-c <astrid_runtime.cli main(['run', ...])>"
                }
            }
        }
    })
    $taskAction = @{ status = 'unavailable'; reason = 'task action query did not run' }
    $registeredTail = $null
    try {
        $tasks = @(Get-ScheduledTask -TaskName 'Astrid Runtime' -ErrorAction Stop)
        if ($tasks.Count -ne 1) { throw "expected one Astrid Runtime task, found $($tasks.Count)" }
        if ([string]$tasks[0].TaskPath -ne '\') { throw "unexpected task path $($tasks[0].TaskPath)" }
        $actions = @($tasks[0].Actions)
        if ($actions.Count -ne 1) { throw "expected one task action, found $($actions.Count)" }
        $execute = [string]$actions[0].Execute
        $arguments = [string]$actions[0].Arguments
        if ([string]::IsNullOrWhiteSpace($execute)) { throw 'task action executable was unavailable' }
        # Parse the registered argument tail with exactly the same native
        # Windows parser used for each live process.  Do not serialize the
        # potentially secret-bearing arguments into evidence.
        $parsed = [AstridOwnerCensus.Native]::CommandLineArguments(('x ' + $arguments))
        if ($null -eq $parsed -or $parsed.Length -lt 1) { throw 'task action arguments were unparsable' }
        $tail = @($parsed | Select-Object -Skip 1)
        $registeredTail = $tail
        $taskAction = @{
            status = 'observed'
            task_path = [string]$tasks[0].TaskPath
            executable_path = $execute
            arguments_sha256 = [AstridOwnerCensus.Native]::ArgumentVectorSha256($tail)
            argument_count = $tail.Length
        }
    } catch {
        $taskAction = @{ status = 'unavailable'; reason = $_.Exception.GetType().Name }
    }
    foreach ($owner in $owners) {
        $matchesRegisteredArguments = $false
        $ownerTail = $ownerTails[[string]$owner.ProcessId]
        if ($null -ne $ownerTail -and $null -ne $registeredTail -and
                $ownerTail.Length -eq $registeredTail.Length) {
            $matchesRegisteredArguments = $true
            for ($index = 0; $index -lt $ownerTail.Length; $index++) {
                if ($ownerTail[$index] -cne $registeredTail[$index]) {
                    $matchesRegisteredArguments = $false
                    break
                }
            }
        }
        # This Boolean is calculated from the unredacted native parser output
        # before JSON serialization. The raw tails are never emitted.
        $owner | Add-Member -NotePropertyName RegisteredArgumentsMatch -NotePropertyValue $matchesRegisteredArguments
    }
    @{ owners = @($owners); task_action = $taskAction } | ConvertTo-Json -Compress -Depth 4
} finally {
    $sha256.Dispose()
}
"""


def runtime_processes(data: Path) -> list[int]:
    """Count every live Runtime owner process on this isolated journey host."""
    if sys.platform == "win32":
        observation = json.loads(windows.powershell(
            WINDOWS_RUNTIME_OWNER_CENSUS,
            "runtime-owner-processes.json", show=False, timeout=30))
        if isinstance(observation, dict) and "owners" in observation:
            rows = observation.get("owners")
            task_action = observation.get("task_action")
        else:
            # Keep old, bounded test inputs readable; they do not contain
            # enough proof to normalize a parent and so remain fail-closed.
            rows = observation
            task_action = None
        rows = rows if isinstance(rows, list) else [rows]
        if not all(isinstance(row, dict) for row in rows):
            raise RuntimeError("Windows Runtime owner census returned malformed owner records")
        if task_action is not None and not isinstance(task_action, dict):
            raise RuntimeError("Windows Runtime owner census returned malformed task action evidence")
        matched = []
        for row in rows:
            command = str(row.get("CommandLine") or "")
            matcher = row.get("Matcher")
            if matcher in {"runtime_protocol-start", "astrid_runtime-cli-run"}:
                matched.append((row, command, matcher))
            elif _runtime_owner_command(command):
                # Tests and older bounded callers can still supply raw rows;
                # the retained snapshot below never copies their command text.
                matched.append((row, command, None))
        return _RuntimeOwnerPids(
            [int(row["ProcessId"]) for row, _command, _matcher in matched],
            [_runtime_owner_detail(row, command, matcher)
             for row, command, matcher in matched[:OWNER_CENSUS_MAX_RECORDS]],
            task_action,
        )
    # The installed prelude can be long and can contain newlines. Ask ps for
    # the full command, then keep continuation lines with their process.
    output = subprocess.check_output(["ps", "-ww", "-axo", "pid=,command="], text=True)
    records: list[tuple[int, str]] = []
    for line in output.splitlines():
        match = re.match(r"^\s*(\d+)\s+(.*)$", line)
        if match:
            records.append((int(match.group(1)), match.group(2)))
        elif records:
            pid, command = records[-1]
            records[-1] = (pid, command + "\n" + line)
    return [pid for pid, command in records if _runtime_owner_command(command)]


def _same_windows_path(left: object, right: object) -> bool:
    """Compare two complete Windows image paths without accepting a suffix."""
    if not isinstance(left, str) or not isinstance(right, str) or not left or not right:
        return False
    return ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right))


def _windows_venv_base_executable(venv_executable: object) -> str | None:
    """Resolve only a normal CPython Windows venv's configured pythonw image.

    This intentionally does not infer the base image from a version, filename,
    or a nearby process.  A malformed, relative, unreadable, or ambiguous
    ``pyvenv.cfg`` is no proof and leaves the raw owner census uncollapsed.
    """
    if not isinstance(venv_executable, str):
        return None
    executable = ntpath.normpath(venv_executable)
    if (ntpath.basename(executable).casefold() != "pythonw.exe" or
            ntpath.basename(ntpath.dirname(executable)).casefold() != "scripts"):
        return None
    config = Path(ntpath.join(ntpath.dirname(ntpath.dirname(executable)), "pyvenv.cfg"))
    try:
        if config.is_symlink() or not config.is_file():
            return None
        content = config.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None
    homes = []
    for line in content.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip().casefold() == "home":
            home = value.strip()
            if home:
                homes.append(home)
    if len(homes) != 1 or not ntpath.isabs(homes[0]):
        return None
    return ntpath.normpath(ntpath.join(homes[0], "pythonw.exe"))


def _windows_tail_matches(detail: dict, action: dict) -> bool:
    """Compare redacted native-parser evidence, never raw service arguments."""
    owner_digest = detail.get("command_tail_sha256")
    action_digest = action.get("arguments_sha256")
    owner_count = detail.get("command_tail_argument_count")
    action_count = action.get("argument_count")
    return (isinstance(owner_digest, str) and re.fullmatch(r"[0-9a-f]{64}", owner_digest) is not None and
            owner_digest == action_digest and type(owner_count) is int and owner_count >= 0 and
            owner_count == action_count and detail.get("registered_arguments_match") is True)


def _windows_creation_before(parent: dict, child: dict) -> bool:
    """Require an unambiguous parent-before-child creation ordering."""
    try:
        parent_time = datetime.fromisoformat(str(parent["creation_date"]).replace("Z", "+00:00"))
        child_time = datetime.fromisoformat(str(child["creation_date"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return False
    return parent_time < child_time


def _windows_logical_owner_normalization(owners: _RuntimeOwnerPids,
                                         discovery: dict) -> tuple[list[int], dict]:
    """Collapse precisely one proven CPython venv redirector parent.

    A two-process exception would hide real duplicate Runtime ownership.  This
    routine therefore accepts the physical pair only when every retained fact
    binds it to the selected discovery incarnation and to the registered Task
    Scheduler action.  Any missing fact returns the unmodified raw census.
    """
    raw_pids = [int(pid) for pid in owners]
    selected_pid = discovery.get("pid")
    selected_identity = discovery.get("process_birth_id")
    proof = {
        "status": "not-collapsed",
        "raw_owner_pids": raw_pids,
        "selected_pid": selected_pid,
    }
    if type(selected_pid) is not int or selected_pid <= 0:
        proof["reason"] = "selected discovery PID was unavailable"
        return raw_pids, proof
    if not isinstance(selected_identity, str) or not selected_identity:
        proof["reason"] = "selected discovery native identity was unavailable"
        return raw_pids, proof
    if len(raw_pids) != 2 or len(set(raw_pids)) != 2:
        proof["reason"] = "raw census did not contain exactly one parent and selected child"
        return raw_pids, proof
    if len(owners.details) != 2:
        proof["reason"] = "raw census detail evidence was incomplete"
        return raw_pids, proof
    selected_rows = [row for row in owners.details if row.get("pid") == selected_pid]
    if len(selected_rows) != 1:
        proof["reason"] = "selected discovery PID was not uniquely retained"
        return raw_pids, proof
    child = selected_rows[0]
    if child.get("native_identity") != selected_identity:
        proof["reason"] = "selected discovery native identity did not match the child census record"
        return raw_pids, proof
    parent_pid = child.get("parent_pid")
    parent_rows = [row for row in owners.details if row.get("pid") == parent_pid]
    if type(parent_pid) is not int or len(parent_rows) != 1:
        proof["reason"] = "selected child did not have one immediate raw-census parent"
        return raw_pids, proof
    parent = parent_rows[0]
    if not isinstance(parent.get("native_identity"), str) or not parent["native_identity"]:
        proof["reason"] = "parent native identity was unavailable"
        return raw_pids, proof
    action = owners.task_action
    if not isinstance(action, dict) or action.get("status") != "observed":
        proof["reason"] = "registered task action evidence was unavailable"
        return raw_pids, proof
    if action.get("task_path") != "\\":
        proof["reason"] = "registered task path was not the exact Runtime task path"
        return raw_pids, proof
    if not _same_windows_path(parent.get("executable_path"), action.get("executable_path")):
        proof["reason"] = "parent image did not equal the registered task action image"
        return raw_pids, proof
    if not _windows_tail_matches(parent, action) or not _windows_tail_matches(child, action):
        proof["reason"] = "parsed parent or child command tail did not equal registered task arguments"
        return raw_pids, proof
    expected_base = _windows_venv_base_executable(parent.get("executable_path"))
    if expected_base is None:
        proof["reason"] = "parent venv pyvenv.cfg was unreadable, ambiguous, or did not resolve an absolute base image"
        return raw_pids, proof
    if not _same_windows_path(child.get("executable_path"), expected_base):
        proof["reason"] = "pyvenv.cfg base image did not equal the selected child image"
        return raw_pids, proof
    if not _windows_creation_before(parent, child):
        proof["reason"] = "parent creation did not precede child creation"
        return raw_pids, proof
    proof.update({
        "status": "collapsed",
        "logical_owner_pids": [selected_pid],
        "redirector_pid": parent_pid,
        "selected_native_identity": selected_identity,
        "task_action": {
            "executable_path": action.get("executable_path"),
            "arguments_sha256": action.get("arguments_sha256"),
            "argument_count": action.get("argument_count"),
        },
        "venv_base_image": expected_base,
    })
    return [selected_pid], proof


def _owner_snapshot_value(value: object, *, unavailable: str, limit: int | None = None) -> dict:
    if value is None or value == "":
        return {"status": "unavailable", "reason": unavailable}
    text = str(value)
    if limit is not None and len(text) > limit:
        return {"status": "observed", "value": text[:limit], "truncated": True}
    return {"status": "observed", "value": value}


def _owner_snapshot_pid(value: object, *, unavailable: str) -> dict:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return {"status": "unavailable", "reason": unavailable}
    if pid <= 0:
        return {"status": "unavailable", "reason": unavailable}
    return {"status": "observed", "value": pid}


def _owner_snapshot_record(detail: dict, asserted_pids: set[int]) -> dict:
    parent = _owner_snapshot_pid(detail.get("parent_pid"), unavailable="parent PID was unavailable")
    pid = _owner_snapshot_pid(detail.get("pid"), unavailable="process PID was unavailable")
    pid_value = pid.get("value") if pid["status"] == "observed" else None
    parent_value = parent.get("value") if parent["status"] == "observed" else None
    return {
        "pid": pid,
        "command_line": _owner_snapshot_value(
            detail.get("command_line"), unavailable="sanitized command line was unavailable", limit=512),
        "command_sha256": _owner_snapshot_value(
            detail.get("command_sha256"), unavailable="command-line digest was unavailable", limit=64),
        "command_length": _owner_snapshot_value(
            detail.get("command_length"), unavailable="command-line length was unavailable"),
        "parsed_command_tail": {
            "sha256": _owner_snapshot_value(
                detail.get("command_tail_sha256"),
                unavailable="native parsed command-tail digest was unavailable", limit=64),
            "argument_count": _owner_snapshot_value(
                detail.get("command_tail_argument_count"),
                unavailable="native parsed command-tail argument count was unavailable"),
            "matches_registered_task_arguments": detail.get("registered_arguments_match") is True,
        },
        "executable_path": _owner_snapshot_value(
            detail.get("executable_path"), unavailable="Win32_Process.ExecutablePath was unavailable", limit=1024),
        "parent_pid": parent,
        "creation_time": _owner_snapshot_value(
            detail.get("creation_date"), unavailable="Win32_Process.CreationDate was unavailable", limit=128),
        "native_identity": _owner_snapshot_value(
            detail.get("native_identity"), unavailable="GetProcessTimes creation identity was unavailable", limit=128),
        "matcher": _owner_snapshot_value(detail.get("matcher"), unavailable="owner matcher was unavailable"),
        "parent_child_relation": {
            "status": "observed" if parent["status"] == "observed" else "unavailable",
            "parent_is_asserted_owner": parent_value in asserted_pids if parent_value is not None else None,
            "asserted_children": [],
            "shared_parent_with": [],
            **({} if parent["status"] == "observed" else {"reason": parent["reason"]}),
        },
    }


def _owner_failure_snapshot(discovery: dict, owners: _RuntimeOwnerPids | None,
                            census_error: Exception | None = None,
                            logical_owner_pids: list[int] | None = None,
                            normalization: dict | None = None) -> dict:
    asserted_pids = [int(pid) for pid in owners] if owners is not None else []
    asserted_set = set(asserted_pids)
    records = [_owner_snapshot_record(detail, asserted_set)
               for detail in (owners.details if owners is not None else [])]
    # Relationships require the complete set of retained records, not merely
    # membership in the assertion's PID list.
    by_parent: dict[int, list[int]] = {}
    for record in records:
        pid, parent = record["pid"], record["parent_pid"]
        if pid["status"] == "observed" and parent["status"] == "observed":
            by_parent.setdefault(parent["value"], []).append(pid["value"])
    for record in records:
        parent = record["parent_pid"]
        pid = record["pid"]
        if parent["status"] == "observed" and pid["status"] == "observed":
            record["parent_child_relation"]["shared_parent_with"] = sorted(
                other for other in by_parent.get(parent["value"], []) if other != pid["value"])
            record["parent_child_relation"]["asserted_children"] = sorted(
                by_parent.get(pid["value"], []))
    selected_pid = _owner_snapshot_pid(discovery.get("pid"), unavailable="discovery PID was unavailable")
    selected_value = selected_pid.get("value") if selected_pid["status"] == "observed" else None
    selected_matches = [record for record in records
                        if record["pid"].get("value") == selected_value]
    return {
        "schema_version": 1,
        "receipt_kind": ("windows-runtime-owner-cardinality-failure"
                         if logical_owner_pids is None else
                         "windows-runtime-owner-logical-owner-assertion"),
        "capture": {
            "trigger": "strict exactly-one selected Runtime owner assertion",
            "timing": ("captured before the assertion raises"
                       if logical_owner_pids is None else
                       "captured before the logical-owner assertion resolves"),
            "retention": "write-once; later census and cleanup paths do not overwrite this file",
            "atomicity": {
                "status": "not-established",
                "limitation": "Windows exposes no transaction joining discovery.json, Win32_Process enumeration, and GetProcessTimes; these observations are sequential within one assertion attempt.",
            },
        },
        "selected_discovery_identity": {
            "pid": selected_pid,
            "process_birth_id": _owner_snapshot_value(
                discovery.get("process_birth_id"), unavailable="discovery process_birth_id was unavailable", limit=128),
            "runtime_instance_id": _owner_snapshot_value(
                discovery.get("runtime_instance_id"), unavailable="discovery runtime_instance_id was unavailable", limit=128),
            "active_realm": _owner_snapshot_value(
                discovery.get("active_realm"), unavailable="discovery active_realm was unavailable", limit=128),
            "coordinator_epoch": _owner_snapshot_value(
                discovery.get("coordinator_epoch"), unavailable="discovery coordinator_epoch was unavailable", limit=128),
        },
        "assertion": {
            "expected_selected_pid": selected_pid,
            "asserted_owner_pids": asserted_pids if owners is not None else {
                "status": "unavailable", "reason": f"owner census failed: {type(census_error).__name__}"},
            "selected_pid_bound_to_asserted_owner": {
                "status": "observed" if len(selected_matches) == 1 else "unavailable",
                **({"record_index": records.index(selected_matches[0])} if len(selected_matches) == 1
                   else {"reason": "selected discovery PID was not uniquely represented in the retained owner records"}),
            },
            "retained_owner_record_limit": OWNER_CENSUS_MAX_RECORDS,
            "unretained_asserted_owner_pids": asserted_pids[len(records):],
            **({} if logical_owner_pids is None else {
                "logical_owner_pids": logical_owner_pids,
                "logical_owner_normalization": normalization,
            }),
        },
        "asserted_owners": records,
    }


def _write_owner_failure_snapshot(evidence: Path | None, discovery: dict,
                                  owners: _RuntimeOwnerPids | None,
                                  census_error: Exception | None = None,
                                  logical_owner_pids: list[int] | None = None,
                                  normalization: dict | None = None) -> str:
    if evidence is None:
        return "not-requested"
    path = evidence / OWNER_ASSERTION_SNAPSHOT
    if path.exists() or path.is_symlink():
        return "preserved" if path.is_file() and not path.is_symlink() else "unavailable"
    try:
        evidence.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            json.dump(_owner_failure_snapshot(
                discovery, owners, census_error, logical_owner_pids, normalization), stream,
                      indent=2, sort_keys=True, ensure_ascii=True)
            stream.write("\n")
        return "captured"
    except FileExistsError:
        return "preserved"
    except OSError:
        return "unavailable"


def assert_one_runtime(data: Path, evidence: Path | None = None) -> None:
    discovery = healthy(data)
    try:
        owners = runtime_processes(data)
    except Exception as exc:
        if sys.platform == "win32":
            _write_owner_failure_snapshot(evidence, discovery, None, exc)
        raise
    expected_owner = [int(discovery["pid"])]
    logical_owners = list(owners)
    normalization = None
    snapshot = "not-requested"
    if sys.platform == "win32" and isinstance(owners, _RuntimeOwnerPids):
        logical_owners, normalization = _windows_logical_owner_normalization(owners, discovery)
        # Preserve the raw two-process census and the complete redacted proof
        # even when it legitimately normalizes to one logical owner.  This is
        # write-once because later cleanup censuses are not evidence of the
        # assertion-time process topology.
        if list(owners) != expected_owner:
            if logical_owners == expected_owner and normalization.get("status") == "collapsed":
                snapshot = _write_owner_failure_snapshot(
                    evidence, discovery, owners,
                    logical_owner_pids=logical_owners,
                    normalization=normalization)
            else:
                snapshot = _write_owner_failure_snapshot(evidence, discovery, owners)
    if logical_owners != expected_owner:
        # The raw process file can be replaced by cleanup censuses. Keep a
        # bounded, redacted assertion-time snapshot that cleanup cannot replace.
        details = getattr(owners, "details", [])
        diagnostic = json.dumps(details, sort_keys=True, separators=(",", ":"))
        if snapshot == "not-requested":
            snapshot = _write_owner_failure_snapshot(evidence, discovery, owners)
        raise RuntimeError(
            f"Expected exactly the selected Runtime owner {discovery['pid']}, found {owners}; "
            f"logical_owners={logical_owners}; bounded_owner_census={diagnostic}; "
            f"owner_census_snapshot={snapshot}"
        )


def wait_ready(data: Path, *, timeout: int = 900) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            healthy(data)
            return
        except (OSError, ValueError, KeyError, RuntimeError):
            if time.monotonic() >= deadline:
                raise RuntimeError("Installed Runtime did not become healthy")
            time.sleep(5)


def task(version: dict, evidence: Path, project: str, *, create_project: bool = True) -> str:
    """Exercise the installed SDK through a settled, materialized SFX output."""
    os.environ["ASTRID_HOST_EXECUTION_PROFILE"] = "installed"
    if create_project:
        created = json.loads(installed_cli(version, ["projects", "create", project, "--name", project,
                                                   "--json"], evidence, "project.json"))
        if created.get("ok") is not True:
            raise RuntimeError("Installed project creation failed")
    request = {"capability_id": "chiptune.sfx", "kind": "executor", "project": project,
               "inputs": {"kind": "blip", "variant": 1, "pitch": 0, "duration_s": 0.1}}
    invocation = json.loads(installed_cli(version, [json.dumps(request)], evidence,
                                          "sdk-invocation.json", module="astrid.omp_invoke"))
    refs = invocation.get("details", {}).get("astridTaskRefs", [])
    if len(refs) != 1 or not refs[0].get("task_id"):
        raise RuntimeError("Installed SDK did not admit exactly one task")
    task_id = refs[0]["task_id"]
    try:
        installed_cli(version, ["tasks", "follow", task_id, "--project", project,
                                "--poll-seconds", "2", "--timeout-seconds", "900", "--json"],
                      evidence, "task-follow.jsonl", timeout=960)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        state = json.loads(installed_cli(version, ["tasks", "show", task_id, "--project", project,
                                                 "--json"], evidence, "task-follow-failure.json"))
        if (state.get("data") or {}).get("state") not in TERMINAL:
            installed_cli(version, ["tasks", "cancel", task_id, "--project", project, "--json"],
                          evidence, "task-cancel.json")
            try:
                installed_cli(version, ["tasks", "follow", task_id, "--project", project,
                                        "--poll-seconds", "2", "--timeout-seconds", "300", "--json"],
                              evidence, "task-cancel-follow.jsonl", timeout=360)
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
        final = json.loads(installed_cli(version, ["tasks", "show", task_id, "--project", project,
                                                 "--json"], evidence, "task-after-cancel.json"))
        if (final.get("data") or {}).get("state") not in TERMINAL:
            raise RuntimeError("Task remained nonterminal after cancellation") from exc
        raise RuntimeError(f"Task follow failed: {exc}") from exc
    settled = json.loads(installed_cli(version, ["tasks", "show", task_id, "--project", project,
                                               "--json"], evidence, "task-settled.json"))
    row = settled.get("data") or {}
    outputs = {item.get("name"): item for item in (row.get("result") or {}).get("outputs", [])}
    if (settled.get("ok") is not True or row.get("state") != "succeeded" or
            not {"sfx", "sfx_meta"} <= outputs.keys() or any(
                not re.fullmatch(r"sha256:[0-9a-f]{64}", outputs[name].get("digest", "")) or
                outputs[name].get("size", 0) <= 0 for name in ("sfx", "sfx_meta"))):
        raise RuntimeError("Task did not settle successfully with materialized outputs")
    return task_id


def workspace_root(data: Path) -> Path:
    discovery = read_json(data / "runtime" / "discovery.json")
    root = Path(discovery["realm_root"])
    if not root.is_dir() or root != root.resolve() or not root.is_relative_to(data.parent):
        raise RuntimeError("Selected workspace is missing")
    return root


def assert_content(version: dict, evidence: Path, project: str, marker: Path,
                   digest: str) -> None:
    if marker.is_symlink():
        raise RuntimeError("Workspace marker cannot be a symlink")
    if hashlib.sha256(marker.read_bytes()).hexdigest() != digest:
        raise RuntimeError("Workspace marker did not survive the install/update leg")
    shown = json.loads(installed_cli(version, ["projects", "show", project, "--json"],
                                    evidence, "project-after-leg.json"))
    if shown.get("ok") is not True:
        raise RuntimeError("Installed project content did not survive")


def candidate_vibecomfy_pin(candidate_agent_checkout: Path, candidate_agent_sha: str) -> str:
    """Read the VibeComfy revision committed by the immutable candidate Agent."""
    if (not isinstance(candidate_agent_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", candidate_agent_sha)):
        raise RuntimeError("Candidate Agent identity is absent or unknown")
    if candidate_agent_checkout.is_symlink() or not candidate_agent_checkout.is_dir():
        raise RuntimeError("Candidate Agent checkout is absent or unknown")
    checkout = candidate_agent_checkout.resolve(strict=True)
    if git_source(checkout)["sha"] != candidate_agent_sha:
        raise RuntimeError("Candidate Agent checkout differs from the selected manifest")
    try:
        pins = json.loads(subprocess.check_output(
            ["git", "-C", str(checkout), "show", f"{candidate_agent_sha}:{VIBECOMFY_PIN_PATH}"],
            text=True,
        ))
        revision = pins["sources"]["vibecomfy"]["revision"]
    except (KeyError, TypeError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("Candidate Agent VibeComfy pin is absent or unknown") from exc
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError("Candidate Agent VibeComfy pin is absent or unknown")
    return revision


def selected_tuple(manifest: dict, version: dict, *, candidate_agent_checkout: Path | None = None) -> dict:
    actual = read_json(Path(version["root"]) / "manifest.json")
    for name in ("agent", "runtime"):
        if actual.get(name) != manifest.get(name):
            raise RuntimeError(f"Installed {name} source differs from selected manifest")
    selected = {"agent": actual["agent"]["sha"], "runtime": actual["runtime"]["sha"],
                "schema_digest": actual["runtime"].get("schema_digest"),
                "tools": {name: row.get("version") for name, row in actual.get("tools", {}).items()
                          if isinstance(row, dict) and row.get("version")}}
    if candidate_agent_checkout is not None:
        expected_vibecomfy = candidate_vibecomfy_pin(candidate_agent_checkout, selected["agent"])
        installed_vibecomfy = actual.get("vibecomfy")
        observed_vibecomfy = (installed_vibecomfy.get("sha")
                              if isinstance(installed_vibecomfy, dict) else None)
        if not isinstance(observed_vibecomfy, str) or not re.fullmatch(r"[0-9a-f]{40}", observed_vibecomfy):
            raise RuntimeError("Installed VibeComfy identity is absent or unknown")
        if observed_vibecomfy != expected_vibecomfy:
            raise RuntimeError("Installed VibeComfy identity differs from the candidate Agent pin")
        selected["vibecomfy"] = observed_vibecomfy
    return selected


def _execute_owned(args: argparse.Namespace, root: Path, data: Path,
                   evidence: Path, sentinel: subprocess.Popen, birth: str) -> dict:
    if args.leg in VERSION_JUMP_LEGS:
        if __package__:
            from . import journey_version_jumps
        else:
            import journey_version_jumps
        return journey_version_jumps.execute_owned(args, root, data, evidence, sentinel, birth)
    evidence.mkdir(parents=True, exist_ok=True)
    result = {"schema_version": 2, "receipt_kind": "release1-installed-lifecycle",
              "job_id": getattr(args, "job_id", None), "mode": getattr(args, "mode", "direct"),
              "scenario": args.leg, "status": "not-run",
              "os_build": platform.platform(), "shell": args.shell,
              **runner_identity(),
              "sources": None,
              "candidate_checkouts": getattr(args, "candidate_checkouts", None),
              "service_manager_prerequisite": getattr(args, "service_manager_prerequisite", None),
              "source_override_test_mode": args.source_override,
              "evidence_class": "hosted-lifecycle",
              "source_unit_evidence": {"status": "not-run", "substitute_for_hosted": False},
              "pinokio_backend": None,
              "retained_for_next_leg": getattr(args, "retain_for_next_leg", False),
              "installed_tuple": None, "import_origins": None,
              "run_roots": {"program": str(root), "data": str(data)},
              "sentinel": {"pid": sentinel.pid, "birth": birth},
              "checks": {"task_settled": "not-run", "workspace_content": "not-run",
                         "one_runtime": "not-run", "unrelated_sentinel": "not-run"}}
    result_path = evidence / "journey-result.json"
    write_json(result_path, result)
    try:
        if getattr(args, "mode", "direct") == "pinokio-backend":
            backend_receipt = execute_pinokio_backend(args, evidence)
            result.update(backend_receipt)
            result["pinokio_backend"] = backend_receipt["pinokio_backend"]
            result["source_unit_evidence"] = backend_receipt["source_unit_evidence"]
            result["historical_compatibility"] = backend_receipt["pinokio_backend"].get(
                "historical_compatibility")
            result["status"] = backend_receipt["status"]
            result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return result
        if getattr(args, "job_id", None) and args.mode != "direct":
            # M4-3a owns the fail-closed driver/receipt boundary.  Fully-local
            # lifecycle ownership belongs to M4-4b; never fall back to direct.
            raise RuntimeError(f"{args.mode} requires its dedicated lifecycle adapter")
        app = args.app_checkout.resolve(strict=True)
        manifest_path, manifest = selected_manifest(
            app, source_override=args.source_override, agent=args.agent_checkout,
            runtime=args.runtime_checkout, evidence=evidence)
        result["sources"] = {"app": git_source(app), "agent": manifest.get("agent"),
                             "runtime": manifest.get("runtime")}
        old_version = old_data = old = None
        project = "windows-journey" if sys.platform == "win32" else "tst-b1-fresh-install"
        if args.leg != "fresh-install":
            old_version, old_data, old = installation(root, data)
            assert_one_runtime(data, evidence)
            if args.leg == "stale-update":
                old_manifest = read_json(Path(old["root"]) / "manifest.json")
                if all(old_manifest.get(name) == manifest.get(name) for name in ("agent", "runtime")):
                    raise RuntimeError("stale-update requires an installed Agent or Runtime distinct from the selected manifest")
            verify("observe", old_version, old_data, evidence)
            marker = workspace_root(old_data) / "tst-b1-workspace-marker.txt"
            if not marker.is_file():
                raise RuntimeError("Pre-existing journey workspace marker is missing")
            digest = hashlib.sha256(marker.read_bytes()).hexdigest()
        else:
            if (root / "current").exists():
                raise RuntimeError("fresh-install requires an uninstalled machine")
            marker = None
            digest = ""
        before_sentinel = sentinel_observation(sentinel, birth)
        result["sentinel"]["before_lifecycle"] = before_sentinel
        if not before_sentinel["alive"]:
            raise RuntimeError("Unrelated sentinel exited before the lifecycle operation")
        if args.leg == "reinstall":
            installed_cli(old, ["install", "--uninstall", "--root", str(root),
                                "--data-root", str(data)], evidence, "uninstall.log", timeout=1800)
        bootstrap(app, manifest_path, args.shell, "update" if args.leg == "stale-update" else "install",
                  evidence, root, data)
        after_sentinel = sentinel_observation(sentinel, birth)
        result["sentinel"]["after_lifecycle"] = after_sentinel
        if not after_sentinel["alive"]:
            raise RuntimeError("Unrelated sentinel did not survive the lifecycle operation")
        version_path, data, version = installation(root, data)
        if old_data is not None and data != old_data:
            raise RuntimeError("Installer changed the selected data root")
        if getattr(args, "job_id", None) and args.mode == "direct":
            result["service_manager"] = assert_direct_service_ownership(root, data)
        if sys.platform == "win32":
            windows.EVIDENCE = evidence
            windows.powershell("Start-ScheduledTask -TaskName 'Astrid Runtime' -TaskPath '\\'",
                               "start.log")
        wait_ready(data)
        result["installed_tuple"] = {"app": result["sources"]["app"]["sha"],
                                     **selected_tuple(manifest, version)}
        verify_mode = "update" if args.leg == "stale-update" else "install"
        try:
            verify(verify_mode, version_path, data, evidence,
                   before=evidence / "verify-observe.json" if args.leg == "stale-update" else None)
        finally:
            receipt_path = evidence / f"verify-{verify_mode}.json"
            if receipt_path.is_file():
                observation = read_json(receipt_path)
                result["import_origins"] = (observation.get("details") or {}).get("origin.imports")
        assert_one_runtime(data, evidence)
        result["checks"]["one_runtime"] = "pass"
        task_project = project if args.leg == "fresh-install" else f"tst-b1-{args.leg}"
        if sys.platform == "win32":
            windows.EVIDENCE = evidence
            windows.wait_for("pack-host is running with its current ready file", windows.host_ready)
            windows.snapshot("processes-host-ready.json")
            windows.task(task_project)
        else:
            task(version, evidence, task_project)
        result["checks"]["task_settled"] = "pass"
        if args.leg == "fresh-install":
            marker = workspace_root(data) / "tst-b1-workspace-marker.txt"
            marker.write_text("TST-B1 workspace content\n", encoding="utf-8")
            digest = hashlib.sha256(marker.read_bytes()).hexdigest()
        assert_content(version, evidence, project, marker, digest)
        result["checks"]["workspace_content"] = "pass"
        settled_sentinel = sentinel_observation(sentinel, birth)
        result["sentinel"]["at_settlement"] = settled_sentinel
        if not settled_sentinel["alive"]:
            raise RuntimeError("Unrelated sentinel exited before journey settlement")
        result["checks"]["unrelated_sentinel"] = "pass"
        result["status"] = "pass"
    except Exception as exc:
        result["status"] = "fail"
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        write_json(result_path, result)
    return result


def execute(args: argparse.Namespace) -> dict:
    diagnostic_start_at = getattr(args, "diagnostic_start_at", None)
    run_root, root, data, evidence = run_scope(
        args.run_root, args.root, args.data_root, args.leg,
        diagnostic_start_at=diagnostic_start_at,
    )
    windows.EVIDENCE = evidence
    identity = runner_identity()
    if diagnostic_start_at is not None:
        # This path deliberately does not enter the normal lifecycle/cleanup
        # wrapper.  It can only exercise its named retained-state block and
        # records an ineligible diagnostic receipt rather than a matrix result.
        assert_runner_identity(identity)
        with isolated_environment(run_root, root, data):
            if __package__:
                from . import journey_version_jumps
            else:
                import journey_version_jumps
            return journey_version_jumps.execute_diagnostic_release0_stage(
                args, root, data, evidence,
            )
    try:
        assert_runner_identity(identity)
        if getattr(args, "job_id", None):
            expected = validate_job(args.job_id, args.mode)
            if expected["runner"] != identity["runner_image"]:
                raise RuntimeError(f"Release-1 job {args.job_id} requires {expected['runner']}, not {identity['runner_image']}")
            if expected["shell"] != args.shell:
                raise RuntimeError(f"Release-1 job {args.job_id} requires shell {expected['shell']}")
            args.candidate_checkouts = candidate_checkouts(args)
            try:
                args.service_manager_prerequisite = release1_manager_prerequisite(args, evidence)
            except Exception as manager_error:
                args.service_manager_prerequisite = {
                    "required": args.mode == "direct", "outcome": "fail",
                    "foreground_fallback": False,
                    "error": f"{type(manager_error).__name__}: {manager_error}",
                }
                raise
        assert_host_available("fresh-install" if args.leg in VERSION_JUMP_LEGS else args.leg, root, data)
    except Exception as exc:
        failure_path = evidence / "journey-result.json"
        recorded = read_json(failure_path) if failure_path.is_file() else {
            "schema_version": 2, "receipt_kind": "release1-installed-lifecycle",
            "job_id": getattr(args, "job_id", None), "mode": getattr(args, "mode", "direct"),
            "scenario": args.leg, "status": "fail",
            "os_build": platform.platform(), "shell": args.shell, **identity,
            "run_roots": {"program": str(root), "data": str(data)},
            "candidate_checkouts": getattr(args, "candidate_checkouts", None),
            "service_manager_prerequisite": getattr(args, "service_manager_prerequisite", None),
            "evidence_class": "hosted-lifecycle",
            "source_unit_evidence": {"status": "not-run", "substitute_for_hosted": False},
            "pinokio_backend": None,
            "installed_tuple": None, "import_origins": None,
            "checks": {"task_settled": "not-run", "workspace_content": "not-run",
                       "one_runtime": "not-run", "unrelated_sentinel": "not-run"},
        }
        recorded.update(status="fail", error=f"{type(exc).__name__}: {exc}")
        write_json(failure_path, recorded)
        print(f"Journey failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        snapshot_failure_evidence(evidence, run_root, data, "before-cleanup")
        raise
    try:
        with isolated_environment(run_root, root, data):
            with sentinel_process(run_root, evidence) as (sentinel, birth):
                primary_error = None
                try:
                    result = _execute_owned(args, root, data, evidence, sentinel, birth)
                except Exception as exc:
                    primary_error = exc
                    print(f"Journey failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                    try:
                        result_path = evidence / "journey-result.json"
                        recorded = read_json(result_path) if result_path.is_file() else {}
                        recorded.update(status="fail", error=f"{type(exc).__name__}: {exc}")
                        write_json(result_path, recorded)
                    except Exception as receipt_error:
                        print(f"Journey failure receipt failed: {type(receipt_error).__name__}: {receipt_error}",
                              file=sys.stderr, flush=True)
                    raise
                finally:
                    snapshot_failure_evidence(evidence, run_root, data, "before-cleanup")
                    if not getattr(args, "retain_for_next_leg", False):
                        result_path = evidence / "journey-result.json"
                        try:
                            if (root / "current").is_file():
                                _, _, installed = installation(root, data)
                                installed_cli(installed, ["install", "--uninstall", "--root", str(root),
                                                          "--data-root", str(data)], evidence,
                                              "final-uninstall.log", timeout=1800)
                                if (root / "current").exists() or runtime_processes(data):
                                    raise RuntimeError("Owned installation remained after final uninstall")
                            cleanup_sentinel = sentinel_observation(sentinel, birth)
                            if result_path.is_file():
                                recorded = read_json(result_path)
                                recorded.setdefault("sentinel", {})["after_cleanup"] = cleanup_sentinel
                                write_json(result_path, recorded)
                            assert_host_available("cleanup", root, data)
                            if not cleanup_sentinel["alive"]:
                                raise RuntimeError("Unrelated sentinel did not survive final uninstall")
                            if result_path.is_file():
                                recorded = read_json(result_path)
                                recorded["final_cleanup"] = "pass"
                                write_json(result_path, recorded)
                            if "result" in locals():
                                result["final_cleanup"] = "pass"
                        except Exception as cleanup_error:
                            print(f"Journey cleanup failed: {type(cleanup_error).__name__}: {cleanup_error}",
                                  file=sys.stderr, flush=True)
                            try:
                                recorded = read_json(result_path) if result_path.is_file() else {
                                    "schema_version": 1, "scenario": args.leg}
                                recorded.update(status="fail", final_cleanup="fail",
                                                cleanup_error=f"{type(cleanup_error).__name__}: {cleanup_error}")
                                write_json(result_path, recorded)
                            except Exception as receipt_error:
                                print(f"Journey cleanup receipt failed: {type(receipt_error).__name__}: {receipt_error}",
                                      file=sys.stderr, flush=True)
                            snapshot_failure_evidence(evidence, run_root, data, "after-cleanup")
                            if primary_error is None:
                                raise
                    else:
                        result_path = evidence / "journey-result.json"
                        if result_path.is_file():
                            recorded = read_json(result_path)
                            recorded["final_cleanup"] = "retained-for-next-leg"
                            write_json(result_path, recorded)
                        if "result" in locals():
                            result["final_cleanup"] = "retained-for-next-leg"
                    snapshot_failure_evidence(evidence, run_root, data, "after-cleanup")
                return result
    except Exception as exc:
        # Context setup can fail before the lifecycle body has produced a receipt.
        result_path = evidence / "journey-result.json"
        recorded = read_json(result_path) if result_path.is_file() else {}
        if "before-cleanup" not in recorded.get("failure_diagnostics", {}) or recorded.get("status") != "fail":
            recorded.update(schema_version=2, receipt_kind="release1-installed-lifecycle",
                            scenario=args.leg, status="fail",
                            error=f"{type(exc).__name__}: {exc}")
            write_json(result_path, recorded)
            print(f"Journey failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            snapshot_failure_evidence(evidence, run_root, data, "before-cleanup")
        raise


def _shared_executor_main(argv: list[str]) -> int:
    """Explicit shared routes for the independently owned executable modules."""
    route = argv[0] if argv else ""
    if route == "m4-4b":
        route_parser = argparse.ArgumentParser(description="M4-4b local-process smoke")
        route_parser.add_argument("--checkout", required=True, type=Path)
        route_parser.add_argument("--state-root", required=True, type=Path)
        route_parser.add_argument("--output", required=True, type=Path)
        values = route_parser.parse_args(argv[1:])
        try:
            if sys.platform not in {"linux", "darwin"}:
                raise ValueError("M4-4b local-process smoke supports POSIX hosts only")
            try:
                from .fully_local_executor import run_execution, validate_execution_receipt
            except ImportError:
                from fully_local_executor import run_execution, validate_execution_receipt
            receipt = run_execution(checkout=values.checkout, state_root=values.state_root,
                                    output=values.output)
            validate_execution_receipt(receipt)
            if receipt.get("evidence_class") != "local_process" or receipt.get("hosted_linux_receipt") is True:
                raise ValueError("M4-4b smoke must remain local_process evidence")
            if receipt.get("hosted_linux_receipt_required") is not True:
                raise ValueError("M4-4b local receipt must retain its separate hosted Linux requirement")
        except Exception as exc:
            print(f"M4-4b shared route failed: {exc}", file=sys.stderr)
            return 1
        return 0
    if route == "m4-5":
        route_parser = argparse.ArgumentParser(description="Run one explicit M4-5 scenario")
        route_parser.add_argument("--scenario-id", required=True)
        route_parser.add_argument("--candidate", required=True, type=Path)
        route_parser.add_argument("--boundary", required=True)
        route_parser.add_argument("--boundary-arg", action="append", default=[])
        route_parser.add_argument("--root", required=True, type=Path)
        route_parser.add_argument("--data-root", required=True, type=Path)
        route_parser.add_argument("--receipt", required=True, type=Path)
        route_parser.add_argument("--evidence-kind", required=True,
                                  choices=("disposable-executable", "hosted-lifecycle"))
        values = route_parser.parse_args(argv[1:])
        try:
            try:
                from . import journey_release2 as release2, release2_executor
                from .journey_matrix import validate_m4_5_scenario
            except ImportError:
                import journey_release2 as release2
                import release2_executor
                from journey_matrix import validate_m4_5_scenario
            row = validate_m4_5_scenario(values.scenario_id)
            candidate = read_json(values.candidate)
            try:
                from .shared_journey_driver import JourneyDriver
            except ImportError:
                from shared_journey_driver import JourneyDriver
            command = JourneyDriver.boundary_command(
                values.boundary,
                values.boundary_arg,
                values.evidence_kind,
            )
            if sys.platform == "win32":
                command = windows.powershell5_command(command[0], command[1:])
            receipt = release2_executor.execute_journey(
                scenario_id=row["scenario_id"], candidate=candidate,
                stand_in_marker=release2.STAND_IN_MARKER, channel=row["channel"],
                mode=row["mode"], os_name=row["os"], job_id=row["job_id"],
                runner=row["runner"], shell=row["shell"],
                stand_in_manifest=release2.STAND_IN_MANIFEST,
                stand_in_descriptor=release2.STAND_IN_DESCRIPTOR,
                fixture_sums=release2.FIXTURE_SUMS, boundary_command=command,
                root=values.root.resolve(), data_root=values.data_root.resolve(),
                receipt_path=values.receipt, evidence_kind=values.evidence_kind,
            )
            if receipt.get("evidence_kind") == "hosted-lifecycle":
                release2.validate_receipt(receipt)
        except Exception as exc:
            print(f"M4-5 shared route failed: {exc}", file=sys.stderr)
            return 1
        return 0
    if route == "m4-6-hosted":
        route_parser = argparse.ArgumentParser(
            description="Run one M4-6 hosted native-service journey"
        )
        route_parser.add_argument("--slot-id", required=True)
        route_parser.add_argument("--candidate", required=True, type=Path)
        route_parser.add_argument("--fixture", required=True, type=Path)
        route_parser.add_argument("--output", required=True, type=Path)
        route_parser.add_argument("--work-root", required=True, type=Path)
        route_parser.add_argument("--app-repo", required=True, type=Path)
        route_parser.add_argument("--agent-repo", required=True, type=Path)
        route_parser.add_argument("--runtime-repo", required=True, type=Path)
        route_parser.add_argument("--pinokio-repo", required=True, type=Path)
        route_parser.add_argument("--journey-driver-config", required=True, type=Path)
        values = route_parser.parse_args(argv[1:])
        try:
            try:
                from . import native_service_adapter as native
                from .shared_journey_driver import journey_driver_factory
            except ImportError:
                import native_service_adapter as native
                from shared_journey_driver import journey_driver_factory
            if native.JOURNEY_DRIVER_FACTORY is not None:
                raise RuntimeError("native journey driver is already registered")
            native.register_journey_driver(journey_driver_factory(values.journey_driver_config))
            native_args = [
                "--slot-id",
                values.slot_id,
                "--candidate",
                str(values.candidate),
                "--fixture",
                str(values.fixture),
                "--work-root",
                str(values.work_root),
                "--app-repo",
                str(values.app_repo),
                "--agent-repo",
                str(values.agent_repo),
                "--runtime-repo",
                str(values.runtime_repo),
                "--pinokio-repo",
                str(values.pinokio_repo),
                "--output",
                str(values.output),
            ]
            if native.main(native_args) != 0:
                raise RuntimeError("native hosted journey failed closed")
        except Exception as exc:
            print(f"M4-6 hosted shared route failed: {exc}", file=sys.stderr)
            return 1
        return 0
    if route in {"m4-6", "m4-6-inner"}:
        route_parser = argparse.ArgumentParser(description="Run one M4-6 local executable boundary")
        route_parser.add_argument("--slot-id", required=True)
        route_parser.add_argument("--candidate", required=True, type=Path)
        route_parser.add_argument("--fixture", required=True, type=Path)
        route_parser.add_argument("--output", required=True, type=Path)
        route_parser.add_argument("--work-root", required=True, type=Path)
        route_parser.add_argument("--agent-repo", required=True, type=Path)
        route_parser.add_argument("--release0-runtime-repo", required=True, type=Path)
        values = route_parser.parse_args(argv[1:])
        if route == "m4-6" and sys.platform == "win32":
            command = windows.powershell5_command(
                sys.executable,
                [str(Path(__file__).resolve()), "--shared-executor", "m4-6-inner", *argv[1:]],
            )
            return subprocess.run(command, check=False).returncode
        try:
            try:
                from . import failure_mode_executor, journey_failure_modes as failure
                from .journey_matrix import validate_m4_6_slot
            except ImportError:
                import failure_mode_executor
                import journey_failure_modes as failure
                from journey_matrix import validate_m4_6_slot
            slot = validate_m4_6_slot(values.slot_id)
            candidate = failure.read_json(values.candidate)
            if failure.candidate_errors(candidate):
                raise ValueError("M4-6 candidate tuple is incomplete")
            observation = failure_mode_executor.execute(
                slot["job_id"], candidate, values.fixture, values.work_root,
                agent_repo=values.agent_repo, runtime_repo=values.release0_runtime_repo,
            )
            executor_errors = failure_mode_executor.execution_observation_errors(observation)
            if executor_errors:
                raise ValueError("M4-6 local executor observations failed: " + "; ".join(executor_errors))
            if observation.get("execution_scope") != "local-disposable":
                raise ValueError("M4-6 executable boundary must remain local-disposable")
            evidence_source = values.work_root / "evidence"
            values.output.parent.mkdir(parents=True, exist_ok=True)
            if evidence_source.is_dir():
                shutil.copytree(evidence_source, values.output.parent / "evidence")
            receipt = {
                "schema_version": 1,
                "receipt_kind": "m4-6-local-disposable-execution",
                "status": "exercised",
                "slot_id": slot["job_id"],
                "execution_scope": "local-disposable",
                "source_override_test_mode": False,
                "candidate": candidate,
                "evidence_files": observation.get("evidence_files", []),
                "observation": observation,
            }
            failure.write_json(values.output, receipt)
            if (observation.get("runner_provenance") or {}).get("ImageVersion") != "local-disposable":
                raise ValueError("M4-6 local execution scope was not retained in its receipt")
        except Exception as exc:
            print(f"M4-6 shared route failed: {exc}", file=sys.stderr)
            return 1
        return 0
    print(f"Unknown shared executor route: {route!r}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv[:1] == ["--shared-executor"]:
        return _shared_executor_main(raw_argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leg", choices=("fresh-install", "stale-update", "reinstall", *VERSION_JUMP_LEGS), required=True)
    parser.add_argument("--old-declaration", choices=("bare", "pruned"),
                        help="required for update-over-old-declaration; corrupt only the run-owned chat command")
    parser.add_argument("--app-checkout", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True,
                        help="fresh run-owned directory under the runner temporary directory")
    parser.add_argument("--root", type=Path, required=True, help="RUN_ROOT/program")
    parser.add_argument("--data-root", type=Path, required=True, help="RUN_ROOT/data")
    parser.add_argument("--shell", default="powershell.exe" if sys.platform == "win32" else "sh")
    parser.add_argument("--job-id", help="fixed Release-1 matrix job id; enables the fail-closed receipt contract")
    parser.add_argument("--mode", choices=RELEASE1_MODES, default="direct",
                        help="declared lifecycle owner; direct never permits foreground fallback")
    parser.add_argument("--candidate", type=Path,
                        help="frozen four-ref candidate JSON used to verify exact checkouts")
    parser.add_argument("--candidate-agent-checkout", type=Path)
    parser.add_argument("--candidate-runtime-checkout", type=Path)
    parser.add_argument("--pinokio-checkout", type=Path)
    parser.add_argument("--old-launcher-ref",
                        help="required full SHA for the approved pre-self-pull Pinokio action")
    parser.add_argument("--stopped-path-agent-ref",
                        help="required full candidate Agent SHA carrying the integrated stopped path")
    parser.add_argument("--pinokio-backend-config", type=Path,
                        help="explicit JSON inputs for the real M4-4a lifecycle executor")
    parser.add_argument("--release0-fixture", type=Path,
                        help="the checked-in frozen release-0 fixture directory for the direct 0-to-1 leg")
    parser.add_argument("--allow-release0-to-release1", action="store_true",
                        help="admit only the exact frozen release-0 direct journey to release 1")
    parser.add_argument("--source-override", action="store_true", help="TEST MODE: install Agent and Runtime from local Git commits")
    parser.add_argument("--agent-checkout", type=Path)
    parser.add_argument("--runtime-checkout", type=Path)
    parser.add_argument("--retain-for-next-leg", action="store_true",
                        help="keep only this run's installed service for the next named leg")
    parser.add_argument("--diagnostic-start-at", choices=("release0-api-seed",),
                        help="DIAGNOSTIC ONLY: run one retained release-0 stage, never qualification")
    parser.add_argument("--diagnostic-state", type=Path,
                        help="exact run-owned precondition checkpoint for --diagnostic-start-at")
    args = parser.parse_args(argv)
    diagnostic = args.diagnostic_start_at is not None
    if (args.leg == "update-over-old-declaration") != (args.old_declaration is not None):
        parser.error("--old-declaration is required only for update-over-old-declaration")
    if bool(args.diagnostic_state) != diagnostic:
        parser.error("--diagnostic-state is required only with --diagnostic-start-at")
    if diagnostic:
        if args.job_id:
            parser.error("diagnostic stage starts cannot use a qualification job id")
        if args.mode != "direct" or args.leg != "release0-to-release1":
            parser.error("diagnostic stage starts are limited to the direct release0-to-release1 leg")
        if args.source_override:
            parser.error("diagnostic stage starts never admit --source-override")
        if args.candidate is None or not all((args.candidate_agent_checkout,
                                              args.candidate_runtime_checkout,
                                              args.pinokio_checkout)):
            parser.error("diagnostic stage starts require frozen candidate and all candidate checkouts")
        if args.retain_for_next_leg:
            parser.error("diagnostic stage starts retain their validated state automatically")
    elif bool(args.job_id) != bool(args.candidate):
        parser.error("--job-id and --candidate are required together for Release-1 receipts")
    if args.job_id:
        try:
            validate_job(args.job_id, args.mode)
        except ValueError as exc:
            parser.error(str(exc))
        if not all((args.candidate_agent_checkout, args.candidate_runtime_checkout, args.pinokio_checkout)):
            parser.error("Release-1 receipts require candidate Agent, Runtime and Pinokio checkouts")
        if args.mode == "pinokio-backend":
            if not args.old_launcher_ref:
                parser.error("pinokio-backend requires --old-launcher-ref")
            if not args.stopped_path_agent_ref:
                parser.error("pinokio-backend requires --stopped-path-agent-ref")
            if not args.pinokio_backend_config:
                parser.error("pinokio-backend requires --pinokio-backend-config")
        elif args.old_launcher_ref or args.stopped_path_agent_ref:
            parser.error("Pinokio backend refs require a pinokio-backend job")
        if args.mode != "pinokio-backend" and args.pinokio_backend_config:
            parser.error("--pinokio-backend-config is only valid for pinokio-backend")
    elif args.old_launcher_ref or args.stopped_path_agent_ref:
        parser.error("Pinokio backend refs require a Release-1 Pinokio backend job")
    if args.leg == "release0-to-release1":
        if not ((diagnostic or args.job_id) and args.mode == "direct" and args.candidate):
            parser.error("release0-to-release1 requires a frozen direct Release-1 receipt")
        if args.release0_fixture is None or not args.allow_release0_to_release1:
            parser.error("release0-to-release1 requires --release0-fixture and --allow-release0-to-release1")
    elif args.release0_fixture is not None or args.allow_release0_to_release1:
        parser.error("release-0 fixture admission is only valid for release0-to-release1")
    if sys.platform not in {"linux", "darwin", "win32"}:
        parser.error("unsupported journey operating system")
    if sys.platform == "win32" and args.shell != "powershell.exe":
        parser.error("Windows hosted journeys require Windows PowerShell 5.1 (powershell.exe)")
    if sys.platform != "win32" and args.shell != "sh":
        parser.error("Linux/macOS require sh")
    try:
        with without_provider_credentials():
            execute(args)
    except Exception as exc:
        print(f"Journey failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
