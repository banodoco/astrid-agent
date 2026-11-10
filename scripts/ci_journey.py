"""Named, installed-product install/update journeys for Linux, macOS and Windows.

The App checkout supplies the shipped manifest verbatim.  ``--source-override``
is an explicit test mode for candidate Agent and Runtime Git commits.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import ntpath
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path

if __package__:
    from . import ci_windows_journey as windows
    from . import journey_live_workspace as live_workspace
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
    import journey_live_workspace as live_workspace
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
# Packs the journeys need that no longer ship in the Agent: name -> (source, pinned commit).
JOURNEY_PACKS = {"chiptune": ("https://github.com/banodoco/poms_packs", "2379ab54aca522019ff4f869cfdf9375bdb56dfc")}
OWNER_FILE = "tst-b1-owner.json"
VERSION_JUMP_LEGS = ("update-from-served", "update-from-release:pre-updater",
                     "update-over-old-declaration", "old-runtime-busy",
                     "release9-then-install", "r0-handoff-then-update", "release11-then-update",
                     "fresh-install-autostart-off", "windows-upgrade-refusal")
RUNNER_FAMILY = {"ubuntu-24.04": "linux", "ubuntu-22.04": "linux",
                 "windows-2022": "win32", "macos-14": "darwin", "macos-15": "darwin"}
GITHUB_RUNNER_OS = {"Linux": "linux", "Windows": "win32", "macOS": "darwin"}

JOURNEY_EVIDENCE_NAMES = frozenset({
    "windows-schema-acl.json", "journey-result.json", "keepgoing-report.json", "keepgoing-report.md", "install.log", "uninstall.log", "final-uninstall.log",
    "live-release1-snapshot-failure.json",
    "release1-task-sdk-invocation.json", "release1-task-installed-source.json",
    "install-bootstrap.log", "update-bootstrap.log", "release0-bootstrap.log", "start.log",
    "install-bootstrap-process.json", "install-bootstrap-stdout.log", "install-bootstrap-stderr.log",
    "update-bootstrap-process.json", "update-bootstrap-stdout.log", "update-bootstrap-stderr.log",
    "verify-install.json", "verify-install.log", "verify-update.json", "verify-update.log",
    "verify-observe.json", "verify-observe.log", "windows-update-manifest.json",
    "windows-update-SHA256SUMS", "windows-update-scheduler-events.json", "windows-update-process-capture.json", "windows-update-wait.jsonl", "windows-update-job.log", "windows-update-wrapper.log", "windows-update-stop-task-state.json", "update-task-labels.log", "update-task-state.log", "update-service-labels.log",
    "service-manager-prerequisite.log", "direct-service-ownership.log",
    "preflight-scheduled-task.txt", "refusal-update-bootstrap.log",
    "update-job-journal.log", "update-job-record.json", "update-transaction.json",
    "update-button.json", "update-button.png",
    "update-transaction-status.json", "runtime-data-runtime.log", "runtime-real-home-runtime.log", "macos-file-listing.txt",
    "scheduled-task-before.xml", "scheduled-task-after.xml",
    "runtime-owner-processes.json", "runtime-owner-selection.json", "runtime-owner-selection-context.json",
    "runtime-owner-workspace.json",
    "runtime-service-runtime.log",
    "runtime-service-native.log",
    # The Windows activation error names this diagnostic directly. Keep its
    # native basename so the receipt contains the path operators are told to inspect.
    "service-pack-host.log",
    "windows-service-log-status.json",
    "manager-state-before-cleanup.json", "manager-state-after-cleanup.json",
    "manager-state-before-cleanup.log", "manager-state-after-cleanup.log",
    "windows-real-profile-pack-host-before-cleanup.json",
    "windows-real-profile-pack-host-after-cleanup.json",
    "windows-pack-host-update-before.json", "windows-pack-host-update-after.json",
    "unit-file-before-cleanup.txt", "unit-file-after-cleanup.txt",
    # install_journey_packs: `packs list` output; per-pack `packs add` logs match JOURNEY_PACK_LOG.
    "journey-packs-list.json",
})
JOURNEY_PACK_LOG = re.compile(r"journey-pack-[A-Za-z0-9_-]+\.log")
EVIDENCE_LOG_LIMIT = 1024 * 1024
WINDOWS_PROFILE_SERVICE_LOG_LIMIT = 64 * 1024
WINDOWS_PACK_HOST_STATE_LIMIT = 64 * 1024
_WINDOWS_ABSOLUTE_PATH = re.compile(
    # Once a path starts inside diagnostic prose, consume the remainder of
    # that line so spaces in Windows path components cannot leak their suffix.
    r"(?i)(?:[a-z]:\\|\\\\[^\\\s]+\\[^\\\s]+\\|/(?:[^/\s]+/)+)[^\r\n]*"
)
SDK_CAPTURE_LIMIT = 128 * 1024
SDK_CAPTURE_STATUS_NAMES = ("project-create-capture-status.json", "sdk-capture-status.json")
SDK_CAPTURE_RECEIPTS: dict[Path, dict[str, dict]] = {}


def journey_os_label() -> str:
    return {"darwin": "macos", "linux": "linux", "win32": "windows"}.get(sys.platform, sys.platform)


def capture_failed_windows_fresh_install(root: Path, evidence: Path) -> None:
    """Best effort after failed bootstrap; never change the primary failure."""
    if sys.platform != "win32":
        return
    try:
        if __package__:
            from . import journey_windows_acl_capture as capture
        else:
            import journey_windows_acl_capture as capture
        capture.capture(root, root.parent, evidence)
    except Exception:
        pass


def capture_failed_release1_task(evidence: Path, version: dict) -> None:
    """Best-effort bounded SDK output and source identity for a failed fresh R1 task."""
    if sys.platform not in {"linux", "darwin"}:
        return
    try:
        invocation = evidence / "release1-working-task" / "sdk-invocation.json"
        if invocation.is_file() and not invocation.is_symlink():
            size = invocation.stat().st_size
            if size <= SDK_CAPTURE_LIMIT:
                (evidence / "release1-task-sdk-invocation.json").write_bytes(invocation.read_bytes())
        manifest_path = Path(version["root"]) / "manifest.json"
        if (manifest_path.is_file() and not manifest_path.is_symlink()
                and manifest_path.stat().st_size <= 65536):
            raw = manifest_path.read_bytes()
            manifest = json.loads(raw)
            agent = manifest.get("agent") if isinstance(manifest, dict) else None
            runtime = manifest.get("runtime") if isinstance(manifest, dict) else None
            agent_sha = agent.get("sha") if isinstance(agent, dict) else None
            runtime_sha = runtime.get("sha") if isinstance(runtime, dict) else None
            if (manifest.get("release") == 1 and isinstance(agent_sha, str)
                    and re.fullmatch(r"[0-9a-f]{40}", agent_sha)
                    and isinstance(runtime_sha, str) and re.fullmatch(r"[0-9a-f]{40}", runtime_sha)):
                write_json(evidence / "release1-task-installed-source.json", {
                    "schema": "astrid.failed-release1-task-source.v1",
                    "release": 1,
                    "agent_sha": agent_sha,
                    "runtime_sha": runtime_sha,
                    "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                })
    except Exception as exc:
        print(f"Failed release-1 task evidence capture unavailable ({type(exc).__name__})",
              file=sys.stderr, flush=True)


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
            unit_dir = data.parent / "home" / ".config" / "systemd" / "user"
            r1_unit = unit_dir / "astrid-r1.service"
            unit = r1_unit if r1_unit.is_file() else unit_dir / "astrid.service"
            command = ["systemctl", "--user", "show", unit.name,
                       "--property=LoadState,FragmentPath,ActiveState,SubState"]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
            manager.update(command=command, returncode=completed.returncode,
                           stdout=completed.stdout, stderr=completed.stderr)
        elif sys.platform == "darwin":
            unit_dir = data.parent / "home" / "Library" / "LaunchAgents"
            r1_unit = unit_dir / "haus.astrid.runtime.r1.plist"
            unit = r1_unit if r1_unit.is_file() else unit_dir / "haus.astrid.runtime.plist"
            command = ["launchctl", "print", f"gui/{os.getuid()}/{unit.stem}"]
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
                               and completed.stderr.strip() in {
                                   f'Could not find service "{unit.stem}" in domain for user gui: {os.getuid()}',
                                   f'Bad request.\nCould not find service "{unit.stem}" in domain for user gui: {os.getuid()}',
                               })
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
            "linux": data.parent / "home" / ".config" / "systemd" / "user",
            "darwin": data.parent / "home" / "Library" / "LaunchAgents",
        }.get(sys.platform)
        try:
            if possible_unit is not None:
                if sys.platform == "linux":
                    r1_name, r0_name = "astrid-r1.service", "astrid.service"
                else:
                    r1_name = "haus.astrid.runtime.r1.plist"
                    r0_name = "haus.astrid.runtime.plist"
                r1 = possible_unit / r1_name
                possible_unit = r1 if r1.is_file() else possible_unit / r0_name
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


def _regular_file_info(path: Path):
    """lstat of a regular, non-link file, or None."""
    try:
        info = path.lstat()
    except OSError:
        return None
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        return None
    return info


def capture_update_transaction_status(evidence: Path, run_root: Path, phase: str) -> None:
    """Say whether the update journal exists (path and size) or is explicitly absent."""
    path = run_root / "program" / "update-transaction.json"
    record = {"schema_version": 1, "phase": phase, "path": str(path)}
    try:
        info = path.lstat()
    except FileNotFoundError:
        record["status"] = "absent"
    except OSError as exc:
        record.update(status="error", error=type(exc).__name__)
    else:
        if _regular_file_info(path) is None:
            record["status"] = "unsafe_type"
        else:
            record.update(status="present", bytes=info.st_size)
    try:
        write_json(evidence / "update-transaction-status.json", record)
    except Exception as exc:
        print(f"Journey update-transaction status ({phase}) failed: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)


LISTING_LIMIT = 2000


def _listing(root: Path) -> list[str]:
    """Names and sizes only, links not followed, at most LISTING_LIMIT entries."""
    limit, lines = LISTING_LIMIT, []
    if not root.is_dir() or root.is_symlink():
        return [f"{root}: " + ("link, not followed" if root.is_symlink() else "absent")]
    for directory, names, files in os.walk(root, followlinks=False):
        for name in sorted(names) + sorted(files):
            path = Path(directory) / name
            if len(lines) >= limit:
                return lines + [f"... truncated at {limit} entries"]
            try:
                info = path.lstat()
            except OSError as exc:
                lines.append(f"{path}\t{type(exc).__name__}")
                continue
            kind = ("link" if stat.S_ISLNK(info.st_mode) else "dir" if stat.S_ISDIR(info.st_mode)
                    else "file" if stat.S_ISREG(info.st_mode) else "other")
            lines.append(f"{path}\t{kind}\t{info.st_size if kind == 'file' else '-'}")
        names.sort()
    return lines


def _account_home() -> Path:
    """The account's real home directory, whatever HOME this process was given."""
    if sys.platform == "darwin":
        import pwd
        try:
            return Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (KeyError, OSError):
            pass  # evidence capture must not fail on an account without a passwd entry
    return Path.home()


def capture_macos_listing(evidence: Path, run_root: Path, phase: str, home: Path | None = None) -> None:
    """Show where the Runtime's files really are on macOS, and say which expected logs were found."""
    home = home if home is not None else _account_home()
    data = run_root / "data"
    log_dirs = sorted({*(run_root / "home" / "Library" / "Logs").glob("Astrid*"),
                       *(home / "Library" / "Logs").glob("Astrid*"),
                       *(home / "Library" / "Application Support").glob("Astrid*")})
    # The harness sets HOME to its isolated home, so the account's real home is listed separately.
    expected = [data / "runtime" / "runtime.log",
                *(run_root / "home" / "Library" / "Logs" / "Astrid Runtime" / name
                  for name in ("runtime.log", "runtime-service.log", "service-pack-host.log", "install.log")),
                *(home / "Library" / "Logs" / "Astrid Runtime" / name
                  for name in ("runtime.log", "runtime-service.log"))]
    out = [f"phase={phase}", "# expected logs"]
    for path in expected:
        info = _regular_file_info(path)
        result = (f"found, {info.st_size} bytes" if info is not None
                  else "missing" if not os.path.lexists(path) else "present but not a regular file")
        out.append(f"{path}\t{result}")
        print(f"Journey expected macOS log {path}: {result}", file=sys.stderr, flush=True)
    for title, root, top_only in ((f"data root top level {data}", data, True),
                                  (f"data root runtime {data / 'runtime'}", data / "runtime", False),
                                  *((f"log directory {d}", d, False) for d in log_dirs)):
        out.append(f"# {title}")
        if top_only:
            try:
                out.extend(f"{c}\t{'dir' if c.is_dir() and not c.is_symlink() else 'link' if c.is_symlink() else 'file'}\t"
                           f"{c.lstat().st_size if c.is_file() and not c.is_symlink() else '-'}"
                           for c in sorted(root.iterdir())[:LISTING_LIMIT])
            except OSError as exc:
                out.append(f"{root}: {type(exc).__name__}")
        else:
            out.extend(_listing(root))
    if not log_dirs:
        out.append("# no Astrid* log directory under the profile or the real home")
    try:
        (evidence / "macos-file-listing.txt").write_text("\n".join(out) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"Journey macOS listing ({phase}) failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def capture_runtime_service_logs(evidence: Path, run_root: Path, phase: str) -> None:
    """Retain bounded tails of Runtime logs from the active journey profile."""
    capture_update_transaction_status(evidence, run_root, phase)
    if sys.platform == "darwin":
        capture_macos_listing(evidence, run_root, phase)
        # The Runtime's own log lives in the data root on macOS, beside its state.
        source = run_root / "data" / "runtime" / "runtime.log"
        if _regular_file_info(source) is not None:
            try:
                with source.open("rb") as stream:
                    stream.seek(max(0, source.stat().st_size - EVIDENCE_LOG_LIMIT))
                    (evidence / "runtime-data-runtime.log").write_bytes(stream.read(EVIDENCE_LOG_LIMIT))
            except Exception as exc:
                print(f"Journey Runtime data log capture ({phase}) failed: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        # A launchd-run Runtime writes under the account's real home, which the harness HOME does not reach.
        real = _account_home() / "Library" / "Logs" / "Astrid Runtime" / "runtime.log"
        if _regular_file_info(real) is not None:
            try:
                with real.open("rb") as stream:
                    stream.seek(max(0, real.stat().st_size - EVIDENCE_LOG_LIMIT))
                    (evidence / "runtime-real-home-runtime.log").write_bytes(stream.read(EVIDENCE_LOG_LIMIT))
            except Exception as exc:
                print(f"Journey real-home Runtime log capture ({phase}) failed: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    if sys.platform == "darwin":
        log_dir = run_root / "home" / "Library" / "Logs" / "Astrid Runtime"
    elif sys.platform == "win32":
        log_dir = (_windows_local_application_data() / "Astrid Runtime" / "logs"
                   if hosted_windows_runner() else
                   run_root / "localappdata" / "Astrid Runtime" / "logs")
    else:
        log_dir = run_root / "home" / ".local" / "state" / "astrid-runtime" / "logs"
    paths = sorted(log_dir.glob("*.log"))
    for source in paths:
        if source.is_symlink() or not source.is_file():
            continue
        name = ("install.log" if source.name == "install.log" else
                "runtime-service-runtime.log" if source.name == "runtime.log" else
                "runtime-service-native.log" if source.name == "runtime-service.log" else
                "service-pack-host.log" if source.name == "service-pack-host.log" else
                f"runtime-service-{source.name}")
        target = evidence / name
        try:
            with source.open("rb") as stream:
                stream.seek(max(0, source.stat().st_size - EVIDENCE_LOG_LIMIT))
                target.write_bytes(stream.read(EVIDENCE_LOG_LIMIT))
        except Exception as exc:
            print(f"Journey Runtime log capture ({phase}) failed for {source.name}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    if sys.platform == "win32":
        _capture_windows_profile_pack_host_log(evidence, run_root, phase)
        # These two exact files are the useful Windows service outputs: the
        # pack-host log named by installer failures and the native Runtime log.
        # Record absence explicitly so an omitted artifact is not ambiguous.
        status = {"schema_version": 1, "phase": phase,
                  "captured_at_utc": datetime.now(timezone.utc).isoformat(), "logs": {}}
        for source_name, evidence_name in (
                ("service-pack-host.log", "service-pack-host.log"),
                ("runtime-service.log", "runtime-service-native.log")):
            source = log_dir / source_name
            item = {"evidence_name": evidence_name}
            try:
                info = source.lstat()
                if (stat.S_ISLNK(info.st_mode) or
                        getattr(info, "st_file_attributes", 0) &
                        getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or
                        not stat.S_ISREG(info.st_mode)):
                    item["status"] = "unsafe_type"
                else:
                    target = evidence / evidence_name
                    if target.is_file() and not target.is_symlink():
                        item.update(status="captured", source_bytes=info.st_size,
                                    captured_bytes=target.stat().st_size)
                    else:
                        item.update(status="error", error="capture_unavailable")
            except FileNotFoundError:
                item["status"] = "missing"
            except OSError as exc:
                item.update(status="error", error=type(exc).__name__)
            status["logs"][source_name] = item
        try:
            write_json(evidence / "windows-service-log-status.json", status)
        except Exception as exc:
            print(f"Journey Windows service-log status capture ({phase}) failed: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def _profile_probe_text(value: str, run_root: str, data_root: str, *, arguments: bool) -> str:
    """Expose only run-owned paths and known launch syntax from task actions."""
    if not isinstance(value, str):
        return "<unavailable>"
    # Values from other tasks and embedded Python can contain profile data or
    # credentials. Retain only known syntax and path markers in the receipt.
    for path, marker in sorted(((data_root, "<data-root>"), (run_root, "<run-root>")),
                               key=lambda pair: len(pair[0]), reverse=True):
        value = re.sub(re.escape(path), lambda _: marker, value, flags=re.IGNORECASE)
    if not arguments:
        if value.casefold().startswith("<run-root>\\"):
            # Relative components belong to this run, but do not expose arbitrary names.
            name = ntpath.basename(value).casefold()
            return "<run-root>\\...\\" + (name if name in {"python.exe", "pythonw.exe"} else "<executable>")
        return "<outside-run-root>" if value else "<empty>"
    allowed = {"-x", "utf8", "-i", "-m", "-c", "astrid_runtime", "run", "--profile", "astrid",
               "--data-root", "--source-manifest", "<data-root>", "<run-root>"}
    parts = re.findall(r"<data-root>|<run-root>|[A-Za-z0-9_-]+|[^A-Za-z0-9_<>-]+", value)
    return "".join(part if part.casefold() in allowed or re.fullmatch(r"[\s\"']+", part)
                   else "<redacted>" for part in parts)[:2048]


def _windows_task_path_forms(path: str) -> tuple[str, ...]:
    """Return the raw and Python-source-escaped forms of a Windows task path."""
    return (path, path.replace("\\", "\\\\"))


def _capture_windows_profile_pack_host_log(evidence: Path, run_root: Path, phase: str) -> None:
    """Capture only this run's scheduled-task pack-host log from the user profile."""
    status = {"schema_version": 1, "phase": phase, "status": "unavailable",
              "probe": {"exit_code": None, "error": None}}
    probe = status["probe"]
    probe_complete = False
    try:
        expected_root = str(run_root.resolve())
        expected_data = str((run_root / "data").resolve())
        expected_data_forms = ", ".join(
            windows._powershell_quote(value) for value in _windows_task_path_forms(expected_data))
        script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
try {
    $task = Get-ScheduledTask -TaskName 'Astrid Runtime' -TaskPath '\' -ErrorAction Stop
} catch {
    if ($_.FullyQualifiedErrorId -eq 'CmdletizationQuery_NotFound_TaskName,Get-ScheduledTask' -and
        $_.CategoryInfo.Category -eq [System.Management.Automation.ErrorCategory]::ObjectNotFound) {
        @{ task_found = $false; task_path = $null; action_count = 0; actions = @(); owned_count = 0;
           profile_local = [Environment]::GetFolderPath('LocalApplicationData') } | ConvertTo-Json -Depth 5 -Compress
        exit 0
    }
    exit 1
}
$root = [IO.Path]::GetFullPath(__RUN_ROOT__).TrimEnd('\') + '\'
$dataForms = @(__DATA_PATH_FORMS__)
$actions = @($task.Actions | ForEach-Object {
    $execute = [string]$_.Execute
    $arguments = [string]$_.Arguments
    $rootMatch = $null -ne $_.Execute -and $execute.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)
    $dataMatch = $false
    if ($_.Arguments) {
        foreach ($dataForm in $dataForms) {
            if ($arguments.IndexOf($dataForm, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $dataMatch = $true
                break
            }
        }
    }
    @{ executable = $execute; arguments = $arguments; run_root_match = $rootMatch; data_root_match = $dataMatch }
})
$owned = @($actions | Where-Object { $_.run_root_match -and $_.data_root_match })
# Keep the transport bounded, while computing ownership on the complete action values.
$bounded = @($actions | Select-Object -First 8 | ForEach-Object {
    $a = $_.arguments
    if ($a.Length -gt 4096) { $a = $a.Substring(0, 2048) + ' <omitted> ' + $a.Substring($a.Length - 2048) }
    $e = $_.executable
    if ($e.Length -gt 2048) { $e = $e.Substring(0, 2048) }
    @{ executable = $e; arguments = $a; run_root_match = $_.run_root_match; data_root_match = $_.data_root_match }
})
$result = @{ task_found = $true; task_path = [string]$task.TaskPath; action_count = $actions.Count;
             actions = $bounded; owned_count = $owned.Count; profile_local = $null }
if ($owned.Count -eq 1 -or __HOSTED__) { $result.profile_local = [Environment]::GetFolderPath('LocalApplicationData') }
$result | ConvertTo-Json -Depth 5 -Compress
""".replace("__RUN_ROOT__", windows._powershell_quote(expected_root)).replace(
            "__DATA_PATH_FORMS__", expected_data_forms).replace(
            "__HOSTED__", "$true" if hosted_windows_runner() else "$false")
        env = {key: value for key, value in os.environ.items() if key.casefold() != "psmodulepath"}
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, timeout=30, check=False, env=env)
        probe["exit_code"] = result.returncode
        if result.returncode:
            probe["error"] = "command_failed"
            status["status"] = "error"
        else:
            observed = json.loads(result.stdout.decode("utf-8-sig"))
            if (not isinstance(observed, dict) or type(observed.get("task_found")) is not bool or
                    type(observed.get("action_count")) is not int or
                    type(observed.get("owned_count")) is not int or
                    not isinstance(observed.get("actions"), list)):
                raise ValueError("invalid probe output")
            probe.update(task_found=observed["task_found"],
                         task_path=("\\" if observed.get("task_path") == "\\" else
                                    "<other>" if observed["task_found"] else None),
                         action_count=observed["action_count"], owned_count=observed["owned_count"],
                         actions_truncated=observed["action_count"] > 8, actions=[])
            for action in observed["actions"][:8]:
                if (not isinstance(action, dict) or type(action.get("run_root_match")) is not bool or
                        type(action.get("data_root_match")) is not bool):
                    raise ValueError("invalid action")
                probe["actions"].append({
                    "executable": _profile_probe_text(action.get("executable"), expected_root, expected_data, arguments=False),
                    "arguments": _profile_probe_text(action.get("arguments"), expected_root, expected_data, arguments=True),
                    "run_root_match": action["run_root_match"], "data_root_match": action["data_root_match"],
                })
            probe_complete = True
            profile_local = (observed.get("profile_local") if hosted_windows_runner() else
                             observed.get("profile_local") if observed["task_found"] and
                             observed["owned_count"] == 1 else None)
            if not isinstance(profile_local, str) or not profile_local:
                status["status"] = "not_owned"
            else:
                source = Path(profile_local) / "Astrid Runtime" / "logs" / "service-pack-host.log"
                info = source.lstat()
                reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                unsafe_parent = any(
                    parent.is_symlink() or
                    getattr(parent.lstat(), "st_file_attributes", 0) & reparse
                    for parent in source.parents
                )
                if (stat.S_ISLNK(info.st_mode) or
                        getattr(info, "st_file_attributes", 0) & reparse or
                        not stat.S_ISREG(info.st_mode) or
                        unsafe_parent):
                    status["status"] = "unsafe_type"
                else:
                    with source.open("rb") as stream:
                        size = os.fstat(stream.fileno()).st_size
                        stream.seek(max(0, size - WINDOWS_PROFILE_SERVICE_LOG_LIMIT))
                        raw = stream.read(WINDOWS_PROFILE_SERVICE_LOG_LIMIT)
                    decoded = raw.decode("utf-8", errors="replace")
                    safe = windows._diagnostic_text(decoded)
                    safe = _WINDOWS_ABSOLUTE_PATH.sub("<path>", safe)
                    encoded = safe.encode("utf-8")[-WINDOWS_PROFILE_SERVICE_LOG_LIMIT:]
                    evidence_name = f"runtime-service-service-pack-host-real-profile-{phase}.log"
                    evidence.mkdir(parents=True, exist_ok=True)
                    (evidence / evidence_name).write_bytes(encoded)
                    status.update(status="captured", source_bytes=size, captured_bytes=len(encoded),
                                  evidence=evidence_name,
                                  truncated=size > len(raw) or len(safe.encode("utf-8")) > len(encoded))
    except FileNotFoundError as exc:
        if probe_complete:
            status["status"] = "missing"
        else:
            status.update(status="error", error=type(exc).__name__)
            probe["error"] = type(exc).__name__
    except Exception as exc:
        status.update(status="error", error=type(exc).__name__)
        if not probe_complete:
            probe["error"] = type(exc).__name__
    try:
        write_json(evidence / f"windows-real-profile-pack-host-{phase}.json", status)
    except OSError as exc:
        print(f"Journey Windows profile log status capture ({phase}) failed: {type(exc).__name__}",
              file=sys.stderr, flush=True)


def _capture_windows_pack_host_update_state(evidence: Path, data: Path, phase: str) -> None:
    """Read bounded, scrubbed pack-host registration/Job receipt around update."""
    status = {"schema_version": 1, "phase": phase, "status": "captured", "files": {}}
    support = data / "runtime"
    for name in ("generic-host.json", "generic-host.ready.json", "pack-host-job-cleanup.json"):
        source = support / name
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        unsafe_parent = False
        for parent in reversed(source.parents):
            try:
                attributes = getattr(parent.lstat(), "st_file_attributes", 0)
            except OSError:
                break
            if parent.is_symlink() or attributes & reparse:
                unsafe_parent = True
                break
        if unsafe_parent:
            details, raw = {"status": "unsafe_type"}, None
        else:
            details, raw = windows._read_diagnostic_file(
                source, WINDOWS_PACK_HOST_STATE_LIMIT, False,
            )
        item = {key: value for key, value in details.items() if key in {"status", "bytes", "truncated", "error"}}
        if raw is not None:
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                item["status"] = "malformed"
            else:
                safe = windows._diagnostic_value(value)

                def scrub_paths(observed):
                    if isinstance(observed, str):
                        if ntpath.isabs(observed) or os.path.isabs(observed):
                            return "<path>"
                        return _WINDOWS_ABSOLUTE_PATH.sub("<path>", observed)
                    if isinstance(observed, list):
                        return [scrub_paths(part) for part in observed]
                    if isinstance(observed, dict):
                        result = {}
                        for key, part in observed.items():
                            safe_key = windows._diagnostic_text(str(key))
                            if ntpath.isabs(safe_key) or os.path.isabs(safe_key):
                                safe_key = "<path>"
                            else:
                                safe_key = _WINDOWS_ABSOLUTE_PATH.sub("<path>", safe_key)
                            result[safe_key] = scrub_paths(part)
                        return result
                    return observed

                item["content"] = scrub_paths(safe)
        status["files"][name] = item
    try:
        write_json(evidence / f"windows-pack-host-update-{phase}.json", status)
    except OSError as exc:
        print(f"Journey Windows pack-host update capture ({phase}) failed: {type(exc).__name__}",
              file=sys.stderr, flush=True)


def retain_named_evidence(evidence: Path, phase: str) -> None:
    """Copy only the allowlisted journey evidence, clipping verbose text logs."""
    destination = evidence.parent.parent / "evidence-upload" / phase
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        print(f"Journey evidence staging ({phase}) failed: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return
    # Rebuild capture receipts from this process's current task attempt only.
    for name in SDK_CAPTURE_STATUS_NAMES:
        try:
            target = destination / name
            target.unlink(missing_ok=True)
            value = SDK_CAPTURE_RECEIPTS.get(evidence.absolute(), {}).get(name)
            if valid_sdk_capture_status(value):
                write_json(target, value)
        except (OSError, ValueError, TypeError):
            pass
    for source in sorted(evidence.iterdir(), key=lambda path: path.name):
        name = source.name
        if (name not in JOURNEY_EVIDENCE_NAMES and
                not (name.startswith("runtime-service-service-") and name.endswith(".log")) and
                not re.fullmatch(r"keepgoing-stage-[0-9]{2}\.log", name) and
                not JOURNEY_PACK_LOG.fullmatch(name)):
            continue
        if not source.is_file() or source.is_symlink():
            continue
        target = destination / name
        try:
            if source.suffix == ".log":
                with source.open("rb") as stream:
                    stream.seek(max(0, source.stat().st_size - EVIDENCE_LOG_LIMIT))
                    target.write_bytes(stream.read(EVIDENCE_LOG_LIMIT))
            else:
                shutil.copyfile(source, target)
        except Exception as exc:
            print(f"Journey evidence copy ({phase}) failed for {name}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    # Readiness captures live below the R0 seed task. Promote only these fixed
    # names through the service-log allowlist, preserving bounded payloads.
    readiness_roots = (evidence, evidence / "release0-api-seed" / "release0-task",
                       evidence / "release1-working-task")
    readiness_suffixes = (".json", "-service-pack-host.log", "-runtime.log",
                          "-runtime-service.log", "-generic-host.log")
    for index, directory in enumerate(readiness_roots):
        if not directory.is_dir() or directory.is_symlink():
            continue
        for observation in windows.PACK_HOST_DIAGNOSTIC_OBSERVATIONS:
            for suffix in readiness_suffixes:
                source = directory / f"pack-host-readiness-{observation}{suffix}"
                if source.is_symlink() or not source.is_file():
                    continue
                target = destination / f"runtime-service-service-readiness-{index}-{observation}{suffix}.log"
                try:
                    if source.stat().st_size <= EVIDENCE_LOG_LIMIT:
                        shutil.copyfile(source, target)
                except OSError as exc:
                    print(f"Journey readiness staging failed: {type(exc).__name__}",
                          file=sys.stderr, flush=True)
    if phase == "after-cleanup":
        prior = destination.parent / "before-cleanup"
        for source in sorted(prior.glob("runtime-service-service-readiness-*.log")):
            target = destination / source.name
            if not target.exists() and source.is_file() and not source.is_symlink():
                try:
                    shutil.copyfile(source, target)
                except OSError as exc:
                    print(f"Journey readiness carry-forward failed: {type(exc).__name__}",
                          file=sys.stderr, flush=True)

    # Never upload the helper's raw stdout, traceback, request, or selection.
    for action in ("seed", "snapshot"):
        source = evidence / "release0-api-seed" / f"live-{action}-failure.json"
        target = destination / f"live-{action}-failure.json"
        try:
            target.unlink(missing_ok=True)
            if source.is_file() and not source.is_symlink() and source.stat().st_size <= 4096:
                value = json.loads(source.read_text(encoding="utf-8"))
                if live_workspace.valid_failure_diagnostic(value):
                    write_json(target, value)
        except (OSError, UnicodeError, ValueError, TypeError):
            pass
    # The post-upgrade snapshot has the same basename as the R0 snapshot, so
    # retain its sanitized diagnostic under a distinct artifact name.
    source = evidence / "release1-api-observe" / "live-snapshot-failure.json"
    target = destination / "live-release1-snapshot-failure.json"
    try:
        target.unlink(missing_ok=True)
        if source.is_file() and not source.is_symlink() and source.stat().st_size <= 4096:
            value = json.loads(source.read_text(encoding="utf-8"))
            if live_workspace.valid_failure_diagnostic(value):
                write_json(target, value)
    except (OSError, UnicodeError, ValueError, TypeError):
        pass

    # Keep each R0 workspace census and exact native identities separately
    # from initial selection and from subsequent restart observations.
    workspace_owners = {}
    for action in ("seed", "snapshot"):
        for phase_name in ("before", "after"):
            stage = f"{action}-{phase_name}"
            directory = evidence / "release0-api-seed" / "runtime-owner-workspace" / stage
            observations = {}
            for kind, name in (("census", "runtime-owner-selection.json"),
                               ("context", "runtime-owner-selection-context.json")):
                source = directory / name
                if source.is_file() and not source.is_symlink():
                    try:
                        observations[kind] = json.loads(source.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, ValueError) as exc:
                        observations[kind] = {"error": f"{type(exc).__name__}: {exc}"}
            if observations:
                workspace_owners[stage] = observations
    if workspace_owners:
        write_json(destination / "runtime-owner-workspace.json", workspace_owners)

    # The hosted boundary accepts this legacy filename pattern. Keep only a
    # minimal exit receipt: requests, argv, and process streams can contain
    # user data, paths, or credentials. Numbering preserves distinct calls.
    seed_dirs = sorted(path for path in evidence.glob("**/seed-diagnostics/*")
                       if len(path.name) >= 4 and path.name.isascii() and path.name.isdecimal())
    for index, directory in enumerate(seed_dirs, 1):
        if directory.is_symlink() or not directory.is_dir():
            continue
        source = directory / "exit.json"
        if not source.is_file() or source.is_symlink():
            continue
        target = destination / f"runtime-service-service-seed-{index:04d}-exit.log"
        try:
            value = json.loads(source.read_text(encoding="utf-8"))
            code = value.get("exit_code") if isinstance(value, dict) else None
            # bool is an int subclass, but is not a process exit code.
            receipt = {"exit_code": code if type(code) is int else None}
            write_json(target, receipt)
        except (OSError, UnicodeError, ValueError, TypeError):
            pass


def print_direct_windows_failure_diagnostics(run_root: Path) -> None:
    """Print the row's nested Runtime logs and matching processes before cleanup."""
    log_dir = run_root / "localappdata" / "Astrid Runtime" / "logs"
    code = r"""
$ErrorActionPreference = 'Continue'
$logRoot = __LOG_ROOT__
Write-Output "Direct Windows Runtime logs under: $logRoot"
if (Test-Path -LiteralPath $logRoot -PathType Container) {
    $logs = @(Get-ChildItem -LiteralPath $logRoot -Filter '*.log' -File -Recurse -Force -ErrorAction Continue |
        Sort-Object -Property FullName)
    if ($logs.Count -eq 0) { Write-Output '(no .log files found)' }
    foreach ($log in $logs) {
        Write-Output "=== $($log.FullName) (last 120 lines) ==="
        Get-Content -LiteralPath $log.FullName -Tail 120 -ErrorAction Continue
    }
} else {
    Write-Output '(log directory is unavailable)'
}
Write-Output '=== Win32_Process matching *astrid-r1-direct-windows* ==='
$processes = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Continue |
    Where-Object { $_.CommandLine -like '*astrid-r1-direct-windows*' } |
    Select-Object ProcessId, ParentProcessId, CommandLine)
if ($processes.Count -gt 0) {
    $processes | Format-List ProcessId, ParentProcessId, CommandLine | Out-String -Width 4096 | Write-Output
} else {
    Write-Output '(no matching processes found)'
}
""".replace("__LOG_ROOT__", windows._powershell_quote(log_dir))
    print("Direct Windows failure diagnostics:", flush=True)
    try:
        output = windows.powershell(code, "direct-windows-failure-diagnostics.txt",
                                    show=False, timeout=60)
        print(output, end="" if output.endswith("\n") else "\n", flush=True)
    except Exception as exc:
        # Diagnostics are best effort and must preserve the lifecycle error.
        print(f"Windows failure diagnostics capture failed: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        partial = windows.EVIDENCE / "direct-windows-failure-diagnostics.txt"
        try:
            if partial.is_file():
                print(partial.read_text(encoding="utf-8", errors="replace"), flush=True)
        except Exception as read_error:
            print(f"Windows failure diagnostics partial output unavailable: "
                  f"{type(read_error).__name__}: {read_error}", file=sys.stderr, flush=True)


def snapshot_failure_evidence(evidence: Path, run_root: Path, data: Path, phase: str) -> None:
    """Stage named diagnostics and bind their availability to the journey receipt."""
    result_path = evidence / "journey-result.json"
    try:
        capture_runtime_service_logs(evidence, run_root, phase)
        capture_manager_state(evidence, data, phase)
        retain_named_evidence(evidence, phase)
        destination = evidence.parent.parent / "evidence-upload" / phase
        recorded = read_json(result_path) if result_path.is_file() else {}
        if (phase == "before-cleanup" and sys.platform == "win32" and
                recorded.get("job_id") == "direct-windows" and recorded.get("mode") == "direct" and
                recorded.get("status") == "fail"):
            print_direct_windows_failure_diagnostics(run_root)
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
            status_path = destination / "windows-service-log-status.json"
            if status_path.is_file():
                status = read_json(status_path)
                diagnostic["windows_service_logs"] = status.get("logs", {})
            bootstrap_processes = {}
            for mode in ("install", "update"):
                name = f"{mode}-bootstrap-process.json"
                path = destination / name
                if path.is_file():
                    bootstrap_processes[mode] = {
                        "status": "captured", "path": path.relative_to(run_root).as_posix(),
                        "receipt": read_json(path),
                    }
            if bootstrap_processes:
                diagnostic["bootstrap_processes"] = bootstrap_processes
        if sys.platform == "win32" and recorded.get("scenario") == "release9-then-install":
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
    changes = {"TMP": str(run_root / "tmp"),
               "TEMP": str(run_root / "tmp"), "TMPDIR": str(run_root / "tmp"),
               "XDG_DATA_HOME": str(home / ".local" / "share"),
               "XDG_STATE_HOME": str(home / ".local" / "state"),
               "XDG_CONFIG_HOME": str(home / ".config"),
               "ASTRID_RUNTIME_DATA_ROOT": str(data),
               "ASTRID_CI_PROGRAM_ROOT": str(root), "ASTRID_CI_DATA_ROOT": str(data)}
    if not hosted_windows_runner():
        changes.update({"HOME": str(home), "USERPROFILE": str(home),
                        "LOCALAPPDATA": str(local), "APPDATA": str(run_root / "appdata")})
    if sys.platform.startswith("linux") and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
        # `playwright install` caches Chromium under the runner's real HOME. Point the
        # run-owned HOME at that cache so the browser that clicks Update now is found.
        changes["PLAYWRIGHT_BROWSERS_PATH"] = str(Path.home() / ".cache" / "ms-playwright")
    if sys.platform == "win32":
        native_local = str(_windows_local_application_data()) if hosted_windows_runner() else os.environ.get("LOCALAPPDATA")
        changes[windows.NATIVE_RUNTIME_LOG_DIR_ENV] = (
            str(Path(native_local) / "Astrid Runtime" / "logs") if native_local else "")
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


def hosted_windows_runner() -> bool:
    """Identify the hosted Windows Actions image; self-hosted/local stays isolated."""
    return (sys.platform == "win32"
            and os.environ.get("GITHUB_ACTIONS", "").casefold() == "true"
            and os.environ.get("ASTRID_CI_EXPECTED_RUNNER") == "windows-2022"
            and bool(os.environ.get("ImageOS"))
            # The upgrade-refusal leg runs after the fresh leg on the same runner; it keeps
            # the isolated sandbox profile so the fresh leg's real-profile state can't leak in.
            and os.environ.get("ASTRID_CI_SANDBOX_PROFILE") != "1")


def _windows_local_application_data() -> Path:
    """Ask Windows for the current account's real LocalApplicationData folder."""
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "[Environment]::GetFolderPath('LocalApplicationData')"],
        capture_output=True, text=True, timeout=15, check=True,
    )
    value = completed.stdout.strip()
    if not value or not Path(value).is_absolute():
        raise RuntimeError("Windows LocalApplicationData is unavailable")
    return Path(value)


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


def selection_path() -> Path:
    """Return the installer's per-user selection record, beside its lock.

    Windows keeps it in the private ``Astrid Installer`` directory. An older
    release under test may still have its record in LOCALAPPDATA until the
    candidate installer migrates it.
    """
    parent = program_root().parent
    if sys.platform != "win32":
        return parent / "astrid-install.json"
    private = parent / "Astrid Installer" / "astrid-install.json"
    legacy = parent / "astrid-install.json"
    return legacy if not private.exists() and legacy.exists() else private


def windows_task_owned(output: str, root: Path) -> bool:
    lines = [line.strip().strip('"') for line in output.splitlines() if line.strip()]
    if not lines or lines[0] != "present" or len(lines) != 2:
        return False
    root_prefix = ntpath.normcase(str(root).rstrip("\\/") + "\\")
    return ntpath.normcase(lines[1].replace("/", "\\")).startswith(root_prefix)


def windows_login_selection_diagnostic(root: Path, data: Path) -> dict:
    """Describe Windows login-install selection state without recording paths or values."""
    default = program_root()
    record_path = selection_path()
    private = default.parent / "Astrid Installer" / "astrid-install.json"
    legacy = default.parent / "astrid-install.json"
    present = record_path.exists()
    selection = None
    readable = None
    if present:
        try:
            selection = read_json(record_path)
            readable = True
        except Exception:
            readable = False
    is_object = isinstance(selection, dict)
    return {
        "default_install_marker_present": (default / "install.json").exists(),
        "default_current_marker_present": (default / "current").exists(),
        "private_selection_present": private.exists(),
        "legacy_selection_present": legacy.exists(),
        "selection_location": ("private" if record_path == private else "legacy") if present else None,
        "selection_present": present,
        "selection_readable": readable,
        "selection_is_object": is_object,
        "selection_owner_matches": (
            selection.get("owner") == "haus.astrid.runtime" if is_object else None
        ),
        "selection_program_root_matches_journey": (
            selection.get("program_root") == str(root) if is_object else None
        ),
        "selection_data_root_matches_journey": (
            selection.get("data_root") == str(data) if is_object else None
        ),
        "selection_uninstalled": selection.get("uninstalled") is True if is_object else None,
        "selection_foreground_present": bool(selection.get("foreground")) if is_object else None,
    }


def _same_path(a, b) -> bool:
    """Compare two filesystem paths by normalized identity, not by spelling."""
    def usable(value) -> bool:
        return isinstance(value, (str, Path)) and str(value) != ""

    if not usable(a) or not usable(b):
        return False

    def normalized(value) -> str:
        return os.path.normcase(os.path.normpath(os.path.abspath(str(value))))

    if normalized(a) == normalized(b):
        return True
    try:
        return os.path.exists(a) and os.path.exists(b) and os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def assert_host_available(leg: str, root: Path, data: Path, *, skip_process_check: bool = False,
                          release: int = 0,
                          related_uninstalled_selection: tuple[Path, Path] | None = None) -> None:
    """Reject an existing real login install before changing any service."""
    if release not in {0, 1}:
        raise ValueError("Host availability supports only release 0 or release 1")
    r1_cleanup = leg == "cleanup" and release == 1
    default = program_root()
    record_path = selection_path()
    selection = read_json(record_path) if record_path.exists() else None
    owned_uninstalled = (leg == "cleanup" and isinstance(selection, dict)
                         and selection.get("owner") == "haus.astrid.runtime"
                         and _same_path(selection.get("program_root"), root)
                         and _same_path(selection.get("data_root"), data)
                         and selection.get("uninstalled") is True
                         and not selection.get("foreground"))
    related_uninstalled = (related_uninstalled_selection is not None
                           and isinstance(selection, dict)
                           and selection.get("owner") == "haus.astrid.runtime"
                           and _same_path(selection.get("program_root"), related_uninstalled_selection[0])
                           and _same_path(selection.get("data_root"), related_uninstalled_selection[1])
                           and selection.get("uninstalled") is True
                           and not selection.get("foreground"))
    owned_r1_cleanup = False
    if r1_cleanup and isinstance(selection, dict):
        try:
            marker = read_json(root / "install.json")
            _, selected_data, selected = installation(root, data)
            manifest = read_json(Path(selected["root"]) / "manifest.json")
            # Before UR-15 the handoff's records name R0 as data_root and carry
            # r1_data_root; after normalisation R1 is data_root and R0 is kept
            # as retained_r0_data_root.
            normalised = marker.get("retained_r0_data_root") == str(data)
            r1_data = marker.get("r1_data_root") or (marker.get("data_root") if normalised else None)
            owned_r1_cleanup = (
                selection.get("owner") == "haus.astrid.runtime"
                and selection.get("program_root") == str(root)
                and (selection.get("data_root") == str(data)
                     or (normalised and selection.get("retained_r0_data_root") == str(data)
                         and selection.get("data_root") == str(Path(str(data) + "-r1"))))
                and selection.get("uninstalled") is not True
                and not selection.get("foreground")
                and isinstance(selection.get("identity"), dict)
                and selection.get("identity") == selected.get("identity")
                and selected_data == data
                and type(manifest.get("release")) is int and manifest["release"] >= 1
                and isinstance(r1_data, str)
                and Path(r1_data) == Path(str(data) + "-r1")
                and Path(r1_data).is_dir()
            )
        except (OSError, RuntimeError, TypeError, KeyError, ValueError):
            owned_r1_cleanup = False
    if ((default / "install.json").exists() or (default / "current").exists()
            or (selection is not None and not owned_uninstalled and not related_uninstalled
                and not owned_r1_cleanup)):
        raise RuntimeError("A non-journey Astrid login installation is present")
    if r1_cleanup:
        if not owned_r1_cleanup:
            raise RuntimeError("Release-1 cleanup selection is not owned by this journey")
        # R1 deliberately keeps its service active after the documented
        # uninstall refusal; this check verifies its R1 action and R0 handoff.
        assert_direct_service_ownership(root, data, release=1)
        return
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
    if fresh and not skip_process_check and runtime_processes(data):
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


def launchd_service_is_disabled(output: str, service: str) -> bool:
    """Match launchctl print-disabled's disabled state for one exact service."""
    return re.search(
        rf'(?m)^[ \t]*"{re.escape(service)}"[ \t]*=>[ \t]*(?:true|disabled)[ \t]*$',
        output,
    ) is not None


def assert_direct_service_ownership(root: Path, data: Path, *, release: int = 0) -> dict:
    """Prove the selected direct service matches the lifecycle release phase."""
    if release not in {0, 1}:
        raise ValueError("Direct service ownership supports only release 0 or release 1")
    record_path = selection_path()
    if record_path.is_file() and read_json(record_path).get("foreground"):
        raise RuntimeError("Direct journey selected a forbidden foreground fallback")
    if sys.platform == "linux":
        unit_dir = data.parent / "home" / ".config" / "systemd" / "user"
        unit_name = "astrid-r1.service" if release == 1 else "astrid.service"
        unit = unit_dir / unit_name
        if not unit.is_file() or unit.is_symlink():
            raise RuntimeError(f"Direct journey did not install the owned {unit_name} unit")
        properties = "LoadState,FragmentPath,ActiveState,UnitFileState,DropInPaths" if release == 1 else (
            "LoadState,FragmentPath,ActiveState")
        completed = subprocess.run(["systemctl", "--user", "show", unit_name,
                                    f"--property={properties}"], capture_output=True,
                                   text=True, timeout=30, check=False)
        fields = dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)
        if completed.returncode or fields.get("LoadState") != "loaded" or fields.get("FragmentPath") != str(unit):
            raise RuntimeError("Direct journey service is not owned by the expected systemd user unit")
        if fields.get("ActiveState") != "active":
            state = fields.get("ActiveState", "unknown")
            raise RuntimeError(f"Direct journey systemd user service is inactive (ActiveState={state})")
        if release == 1:
            if fields.get("DropInPaths") != "":
                raise RuntimeError("Release-1 systemd service has unknown or overridden configuration")
            # Query the loaded D-Bus action, not just the unit file on disk.
            object_path = "/org/freedesktop/systemd1/unit/astrid_2dr1_2eservice"
            action = subprocess.run(["busctl", "--user", "--no-pager", "get-property",
                                     "org.freedesktop.systemd1", object_path,
                                     "org.freedesktop.systemd1.Service", "ExecStart"],
                                    capture_output=True, text=True, timeout=30, check=False)
            expected = [str(root / "bin" / "runtime"), "run", "--profile", "astrid",
                        "--data-root", str(Path(str(data) + "-r1"))]
            try:
                parts = shlex.split(action.stdout.strip())
                signature, count, executable, argc, *tail = parts
                number = int(argc)
                command, rest = tail[:number], tail[number:]
                valid = (action.returncode == 0 and signature == "a(sasbttttuii)" and count == "1"
                         and number == len(expected) and executable == expected[0] and command == expected
                         and len(rest) == 8 and rest[0] in {"true", "false"}
                         and all(value.isdecimal() for value in rest[1:]))
            except (ValueError, TypeError):
                valid = False
            if not valid:
                raise RuntimeError("Release-1 systemd service effective action is not the owned launcher")
            r0_unit = unit_dir / "astrid.service"
            if not r0_unit.is_file() or r0_unit.is_symlink():
                raise RuntimeError("Release-1 journey did not retain the owned R0 systemd user unit")
            old = subprocess.run(["systemctl", "--user", "show", "astrid.service",
                                  "--property=LoadState,FragmentPath,ActiveState,UnitFileState"],
                                 capture_output=True, text=True, timeout=30, check=False)
            old_fields = dict(line.split("=", 1) for line in old.stdout.splitlines() if "=" in line)
            if (old.returncode or old_fields.get("LoadState") != "loaded"
                    or old_fields.get("FragmentPath") != str(r0_unit)
                    or old_fields.get("UnitFileState") != "disabled"
                    or old_fields.get("ActiveState") not in {"inactive", "failed"}):
                raise RuntimeError("Release-1 journey R0 service is not owned, disabled and inactive")

        return {"manager": "systemd-user", "service_path": str(unit), "foreground_fallback": False}
    if sys.platform == "darwin":
        label = "haus.astrid.runtime.r1" if release == 1 else "haus.astrid.runtime"
        plist = data.parent / "home" / "Library" / "LaunchAgents" / f"{label}.plist"
        if not plist.is_file() or plist.is_symlink():
            raise RuntimeError(f"Direct journey did not install the owned {label} LaunchAgent")
        domain = f"gui/{os.getuid()}"
        completed = subprocess.run(["launchctl", "print", f"{domain}/{label}"],
                                   capture_output=True, text=True, timeout=30, check=False)
        if completed.returncode:
            raise RuntimeError(f"Direct journey {label} LaunchAgent query failed")
        if release == 1:
            output = completed.stdout
            target = f"{domain}/{label}"
            paths = re.findall(r"(?m)^[ \t]*path = ([^\r\n]+?)[ \t]*$", output)
            states = re.findall(r"(?m)^[ \t]*state = ([^\r\n]+?)[ \t]*$", output)
            programs = re.findall(r"(?m)^[ \t]*program[ \t]*=([^\r\n]*)$", output)
            blocks = re.findall(r"(?ms)^[ \t]*arguments = \{[ \t]*\n(.*?)^[ \t]*\}[ \t]*$", output)
            expected = [str(root / "bin" / "runtime"), "run", "--profile", "astrid",
                        "--data-root", str(Path(str(data) + "-r1"))]
            arguments = [line.strip() for line in blocks[0].splitlines()] if len(blocks) == 1 else []
            if (not output.startswith(target + " = {\n") or paths != [str(plist)]
                    or states != ["running"] or len(programs) != 1
                    or programs[0].strip() != expected[0] or arguments != expected):
                raise RuntimeError(f"Direct journey {label} LaunchAgent is not active from its owned plist and action")
        if release == 1:
            r0_plist = plist.with_name("haus.astrid.runtime.plist")
            if not r0_plist.is_file() or r0_plist.is_symlink():
                raise RuntimeError("Release-1 journey did not retain the owned R0 LaunchAgent")
            old_target = f"{domain}/haus.astrid.runtime"
            old = subprocess.run(["launchctl", "print", old_target], capture_output=True,
                                 text=True, timeout=30, check=False)
            disabled = subprocess.run(["launchctl", "print-disabled", domain], capture_output=True,
                                      text=True, timeout=30, check=False)
            if (disabled.returncode or old.returncode == 0
                    or "Could not find service" not in (old.stderr + old.stdout)
                    or not launchd_service_is_disabled(disabled.stdout, "haus.astrid.runtime")):
                raise RuntimeError("Release-1 journey R0 LaunchAgent is not disabled and unloaded")
        return {"manager": "launchd", "service_path": str(plist), "foreground_fallback": False}
    if sys.platform == "win32":
        if release != 0:
            raise RuntimeError("Release-1 direct service ownership is unsupported on Windows")
        existing = windows.powershell(
            "$t=Get-ScheduledTask -TaskName 'Astrid Runtime' -ErrorAction Stop; "
            "'present'; $t.Actions | ForEach-Object { $_.Execute }",
            "direct-service-ownership.log", show=False, timeout=30)
        if not windows_task_owned(existing, root):
            raise RuntimeError("Direct journey Task Scheduler action is not owned by the run root")
        return {"manager": "task-scheduler", "foreground_fallback": False}
    raise RuntimeError(f"Unsupported service-manager platform: {sys.platform}")


def assert_cleanup_service_ownership(root: Path, data: Path, *, release: int,
                                    fresh_install: bool = False) -> dict:
    """Check the service contract supported by this platform during cleanup.

    Fresh installs and in-place plain-install upgrades of a newer manifest use
    the default service identity (astrid.service / haus.astrid.runtime). The
    release-qualified identity is reserved for an R0-to-R1 update. Windows
    likewise has no distinct Release-1 direct-service ownership contract.
    """
    checked_release = 0 if fresh_install or release not in {0, 1} or sys.platform == "win32" else release
    return assert_direct_service_ownership(root, data, release=checked_release)


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
    retained = marker.get("retained_r0_data_root")
    if retained is not None and Path(retained) == expected_data and data == Path(str(expected_data) + "-r1"):
        # A normalised storage move (UR-15): R1 is the data root and the
        # journey's R0 root is kept as provenance; the journey names it by R0.
        data = expected_data
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


def run(argv: list[str | Path], log: Path, *, env: dict | None = None, timeout: int = 3600) -> str:
    log.parent.mkdir(parents=True, exist_ok=True)
    parts = [str(part) for part in argv]
    if (log.name == "seed.json" and len(parts) >= 4 and
            Path(parts[2]).name == "journey_workspace.py" and parts[3] == "seed"):
        return run_seed(parts, log, env=env, timeout=timeout)
    with log.open("w", encoding="utf-8") as stream:
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


def installer_has_consent_gate(agent: Path | None, manifest_path: Path) -> bool:
    """Whether the Agent this manifest pins declares --yes on its `install` command.

    The App's scripts forward their arguments to that Agent, so the question is asked of the
    Agent that will actually run the install: its CLI source at the pinned commit, read with
    `git show` (nothing is executed, so an old installer can never take a probe for an install).
    No checkout, an unknown pin or an unreadable manifest all mean no gate.
    """
    if agent is None:
        return False
    try:
        pinned = (read_json(manifest_path).get("agent") or {}).get("sha")
        if not isinstance(pinned, str) or not re.fullmatch(r"[0-9a-f]{40}", pinned):
            return False
        result = subprocess.run(["git", "-C", str(agent), "show", f"{pinned}:astrid/installer/cli.py"],
                                capture_output=True, text=True, timeout=60, check=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and re.search(r"""add_argument\(\s*["']--yes["']""", result.stdout) is not None


def bootstrap_flags(manifest_path: Path, mode: str, root: Path, data: Path,
                    *, consent_gate: bool = False) -> list[str | Path]:
    flags: list[str | Path] = ["--manifest", manifest_path, "--root", root,
                               "--data-root", data]
    if mode == "update":
        flags.append("--update")
    elif mode == "install" and consent_gate:
        # A fresh install asks for setup consent and the journey has no terminal, so it
        # accepts the recommended choices as a person would. Updates and repairs of an
        # existing install ask nothing and are never given --yes.
        flags.append("--yes")
    return flags


def bootstrap(app: Path, manifest_path: Path, shell: str, mode: str,
              evidence: Path, root: Path, data: Path, agent: Path | None = None) -> None:
    # Only a fresh install can be asked for consent, so only it probes the candidate Agent CLI.
    flags = bootstrap_flags(manifest_path, mode, root, data,
                            consent_gate=mode == "install" and installer_has_consent_gate(agent, manifest_path))
    env = dict(os.environ)
    env["BROWSER"] = f'"{sys.executable}" -c "pass" %s'
    if sys.platform == "win32":
        # Avoid passing hosted pwsh's module paths to Windows PowerShell 5.1.
        env = {key: value for key, value in env.items() if key.casefold() != "psmodulepath"}
        # uv hard-links cached wheels on Windows by default, which also carries
        # the cache file's ACL into the protected installed version. Copy into
        # the destination so installed files inherit the version directory ACL.
        env["UV_LINK_MODE"] = "copy"
        argv = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", app / "public" / "install.ps1", *flags]
        windows.EVIDENCE = evidence
        windows.run_bootstrap_process(argv, evidence / f"{mode}-bootstrap.log", mode, env=env)
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


# The `astrid` console entry point (pyproject: astrid.console:main). Only it routes
# `packs`; `python -m astrid` goes to the gateway, which has no `packs` route.
CONSOLE_ENTRY = "import sys; from astrid.console import main; sys.exit(main(sys.argv[1:]))"


def installed_cli(version: dict, argv: list[str], evidence: Path, name: str,
                  *, module: str = "astrid", console: bool = False, timeout: int = 300) -> str:
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(version["env"])
    launcher = ["-c", CONSOLE_ENTRY] if console else ["-m", module]
    return run([version["python"], "-I", *launcher, *argv], evidence / name,
               env=env, timeout=timeout)


UPGRADE_UNINSTALL_REFUSAL = (
    "Astrid was upgraded from an earlier version; automatic uninstall after an upgrade isn't supported yet. "
    "Both data folders were kept."
)


def assert_release1_upgrade_uninstall_refusal(installed: dict, root: Path, data: Path,
                                               evidence: Path) -> dict:
    """Prove #222's R0-to-R1 uninstall refusal leaves the upgrade intact."""
    log = evidence / "final-uninstall.log"
    try:
        installed_cli(installed, ["install", "--uninstall", "--root", str(root),
                                  "--data-root", str(data)], evidence,
                      "final-uninstall.log", timeout=1800)
    except RuntimeError as exc:
        refused = re.search(r"final-uninstall\.log: exit ([1-9][0-9]*); see ", str(exc))
        if not refused:
            raise RuntimeError("Release-1 uninstall did not exit with a refusal") from exc
    else:
        raise RuntimeError("Release-1 uninstall unexpectedly succeeded")

    try:
        output = log.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError("Release-1 uninstall refusal log is unavailable") from exc
    if output.splitlines().count(UPGRADE_UNINSTALL_REFUSAL) != 1:
        raise RuntimeError("Release-1 uninstall did not emit the required upgrade refusal")
    current = root / "current"
    if not current.is_file():
        raise RuntimeError("Release-1 uninstall refusal removed the selected version")
    version_path, selected_data, selected = installation(root, data)
    release = read_json(Path(selected["root"]) / "manifest.json").get("release")
    if (version_path != Path(installed["root"]) / "version.json" or selected_data != data
            or type(release) is not int or release < 1):
        raise RuntimeError("Release-1 uninstall refusal changed the selected installation")
    marker = read_json(root / "install.json")
    r1_data = marker.get("r1_data_root") or (marker.get("data_root") if marker.get("retained_r0_data_root") else None)
    if not isinstance(r1_data, str) or not Path(r1_data).is_dir() or not data.is_dir():
        raise RuntimeError("Release-1 uninstall refusal did not retain both data folders")
    service = assert_direct_service_ownership(root, data, release=1)
    return {"exit_code": int(refused.group(1)), "message": UPGRADE_UNINSTALL_REFUSAL,
            "r0_data_root": str(data), "r1_data_root": r1_data,
            "service_manager": service}


def assert_macos_services_removed(data: Path) -> None:
    """Require both journey LaunchAgent labels and their plists to be absent."""
    unit_dir = data.parent / "home" / "Library" / "LaunchAgents"
    uid = os.getuid()
    for label in ("haus.astrid.runtime", "haus.astrid.runtime.r1"):
        plist = unit_dir / f"{label}.plist"
        if plist.exists() or plist.is_symlink():
            raise RuntimeError(f"LaunchAgent plist remained after final uninstall: {plist}")
        completed = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"],
                                   capture_output=True, text=True, timeout=30, check=False)
        missing = f'Could not find service "{label}" in domain for user gui: {uid}'
        if (completed.returncode != 113 or completed.stderr.strip() not in
                {missing, f"Bad request.\n{missing}"}):
            raise RuntimeError(f"LaunchAgent absence not confirmed after final uninstall: {label}")


def valid_sdk_capture_status(value: object) -> bool:
    return (isinstance(value, dict) and set(value) == {
        "schema", "exit_condition", "response_size", "json_object", "ok", "task_refs"
    } and value["schema"] == "astrid.sdk-capture-status.v1"
        and value["exit_condition"] in ("success", "failure", "timeout")
        and value["response_size"] in ("empty", "within_limit", "over_limit", "unavailable")
        and (value["json_object"] is None or type(value["json_object"]) is bool)
        and (value["ok"] is None or type(value["ok"]) is bool)
        and value["task_refs"] in ("zero", "one", "multiple", "unknown"))


def installed_cli_with_capture(version: dict, argv: list[str], evidence: Path, name: str,
                               *, status_name: str, module: str = "astrid",
                               timeout: int = 300) -> str:
    """Bound combined output locally; publish only a closed status observation.

    Project and task status names identify the call, not a Runtime owner.
    Size classifies combined stdout/stderr; timeout sizes are only bytes observed
    before termination. Never parse timeout output or a stream without EOF.
    """
    status = {"schema": "astrid.sdk-capture-status.v1", "exit_condition": "failure",
              "response_size": "unavailable", "json_object": None,
              "ok": None, "task_refs": "unknown"}
    try:
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / name).unlink(missing_ok=True)
        env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
        env.update(version["env"])
        command = [str(version["python"]), "-I", "-m", module, *argv]
        reader = None
        reader_joined = False
        process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            retained = bytearray()
            overflow = False
            reader_failed = False

            def drain() -> None:
                nonlocal overflow, reader_failed
                try:
                    while True:
                        chunk = process.stdout.read(8192)
                        if not chunk:
                            break
                        remaining = SDK_CAPTURE_LIMIT - len(retained)
                        retained.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            overflow = True
                except (OSError, ValueError):
                    reader_failed = True

            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as timeout_error:
                timed_out = True
                status["exit_condition"] = "timeout"
                try:
                    process.kill()
                    process.wait()
                except BaseException as cleanup_error:
                    timeout_error.add_note(f"Child timeout cleanup failed: {type(cleanup_error).__name__}: {cleanup_error}")
                    raise timeout_error
            reader.join(timeout=2)
            reader_joined = True
            incomplete = reader.is_alive() or reader_failed
            if not reader.is_alive():
                process.stdout.close()
            status["exit_condition"] = "timeout" if timed_out else "success" if process.returncode == 0 else "failure"
            if not incomplete:
                raw = bytes(retained)
                status["response_size"] = ("over_limit" if overflow else "within_limit" if raw else "empty")
                if not timed_out and not overflow:
                    try:
                        value = json.loads(raw.decode("utf-8-sig", errors="replace"))
                        status["json_object"] = isinstance(value, dict)
                        if isinstance(value, dict):
                            if type(value.get("ok")) is bool:
                                status["ok"] = value["ok"]
                            details = value.get("details")
                            refs = details.get("astridTaskRefs") if isinstance(details, dict) else None
                            if isinstance(refs, list):
                                status["task_refs"] = "zero" if not refs else "one" if len(refs) == 1 else "multiple"
                    except (ValueError, UnicodeError, RecursionError):
                        status["json_object"] = False
                try:
                    (evidence / name).write_bytes(raw)
                except OSError:
                    if not timed_out and not process.returncode:
                        raise
            if timed_out:
                raise subprocess.TimeoutExpired(command, timeout)
            if process.returncode:
                raise RuntimeError(f"{name}: exit {process.returncode}; see {evidence / name}")
            if incomplete or overflow:
                raise RuntimeError(f"{name}: response capture incomplete or over limit")
            return raw.decode("utf-8-sig", errors="replace")
        except BaseException as primary:
            # Only the process we launched is ours to terminate.  In particular,
            # never close its buffered pipe while a reader may be blocked on an
            # inherited writer: close() could otherwise wait forever for its lock.
            try:
                process.kill()
            except BaseException as cleanup_error:
                primary.add_note(f"Child termination failed: {type(cleanup_error).__name__}: {cleanup_error}")
            else:
                try:
                    process.wait()
                except BaseException as cleanup_error:
                    primary.add_note(f"Child reap failed: {type(cleanup_error).__name__}: {cleanup_error}")
            try:
                if not reader_joined and reader is not None and reader.ident is not None:
                    reader.join(timeout=2)
                if reader is None or reader.ident is None or not reader.is_alive():
                    process.stdout.close()
            except BaseException as cleanup_error:
                primary.add_note(f"Reader cleanup failed: {type(cleanup_error).__name__}: {cleanup_error}")
            raise
    finally:
        try:
            SDK_CAPTURE_RECEIPTS.setdefault(evidence.absolute(), {})[status_name] = status
        except (OSError, ValueError, TypeError, RecursionError):
            # Diagnostics cannot replace the original result or exception.
            pass


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


def runtime_processes(data: Path) -> list[int]:
    """Find matching Runtime processes for admission, stop waits, and cleanup."""
    if sys.platform == "win32":
        rows = json.loads(windows.powershell(
            "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
            "runtime-owner-processes.json", show=False, timeout=30))
        rows = rows if isinstance(rows, list) else [rows]
        return [int(row["ProcessId"]) for row in rows if
                _runtime_owner_command(str(row.get("CommandLine") or ""))]
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


def wait_ready(data: Path, *, timeout: int = 900) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while True:
        try:
            healthy(data)
            return
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            last_error = exc
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Installed Runtime did not become healthy; "
                    f"last health check failed: {type(last_error).__name__}: {last_error}"
                ) from last_error
            time.sleep(5)


_sdk_result = windows.sdk_result


def _pack_rows(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _pack_rows(child)
    elif isinstance(value, list):
        for child in value:
            yield from _pack_rows(child)


def install_journey_packs(run_cli) -> None:
    """Install each JOURNEY_PACKS pack from its pinned source through the public `packs add` path.

    ``run_cli(argv, name)`` runs the installed ``astrid`` console entry point (the
    only route to ``packs``) and returns its stdout.
    """
    for pack_id, (source, revision) in JOURNEY_PACKS.items():
        listing = json.loads(run_cli(["packs", "list", "--json"], "journey-packs-list.json"))
        if any(row.get("id") == pack_id for row in _pack_rows(listing)):
            continue
        run_cli(["packs", "add", source, "--revision", revision, "--pack-id", pack_id,
                 "--subpath", pack_id, "--yes"], f"journey-pack-{pack_id}.log")


def submit_task(version: dict, evidence: Path, project: str, *, create_project: bool = True,
                duration_s: float = 0.1) -> str:
    """Admit one chiptune SFX task through the installed SDK and return its id, unfollowed."""
    SDK_CAPTURE_RECEIPTS.pop(evidence.absolute(), None)
    for name in ("project.json", "sdk-invocation.json"):
        (evidence / name).unlink(missing_ok=True)
    for phase in ("before-cleanup", "after-cleanup"):
        for name in SDK_CAPTURE_STATUS_NAMES:
            try:
                (evidence.parent.parent / "evidence-upload" / phase / name).unlink(missing_ok=True)
            except OSError:
                pass
    os.environ["ASTRID_HOST_EXECUTION_PROFILE"] = "installed"
    if create_project:
        created = json.loads(installed_cli_with_capture(version, ["projects", "create", project, "--name", project,
                                                   "--json"], evidence, "project.json",
                                                   status_name="project-create-capture-status.json"))
        if created.get("ok") is not True:
            raise RuntimeError("Installed project creation failed")
    install_journey_packs(lambda argv, name: installed_cli(version, argv, evidence, name,
                                                           console=True, timeout=600))
    request = {"capability_id": "chiptune.sfx", "kind": "executor", "project": project,
               "inputs": {"kind": "blip", "variant": 1, "pitch": 0, "duration_s": duration_s}}
    invocation = _sdk_result(installed_cli_with_capture(version, [json.dumps(request)], evidence,
                                          "sdk-invocation.json", module="astrid.omp_invoke",
                                          status_name="sdk-capture-status.json"))
    if invocation.get("ok") is False:
        error = invocation.get("error") or {}
        raise RuntimeError(f"Installed SDK invocation failed: {error.get('code', 'unknown')}")
    refs = invocation.get("details", {}).get("astridTaskRefs", [])
    if len(refs) != 1 or not refs[0].get("task_id"):
        raise RuntimeError("Installed SDK did not admit exactly one task")
    return refs[0]["task_id"]


def task(version: dict, evidence: Path, project: str, *, create_project: bool = True) -> str:
    """Exercise the installed SDK through a settled, materialized SFX output."""
    task_id = submit_task(version, evidence, project, create_project=create_project)
    try:
        installed_cli(version, ["tasks", "follow", task_id, "--project", project,
                                "--poll-seconds", "2", "--timeout-seconds", "900", "--json"],
                      evidence, "task-follow.jsonl", timeout=960)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        state = _sdk_result(installed_cli(version, ["tasks", "show", task_id, "--project", project,
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
        final = _sdk_result(installed_cli(version, ["tasks", "show", task_id, "--project", project,
                                                 "--json"], evidence, "task-after-cancel.json"))
        if (final.get("data") or {}).get("state") not in TERMINAL:
            raise RuntimeError("Task remained nonterminal after cancellation") from exc
        raise RuntimeError(f"Task follow failed: {exc}") from exc
    settled = _sdk_result(installed_cli(version, ["tasks", "show", task_id, "--project", project,
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


def selected_tuple(manifest: dict, version: dict) -> dict:
    actual = read_json(Path(version["root"]) / "manifest.json")
    for name in ("agent", "runtime"):
        if actual.get(name) != manifest.get(name):
            raise RuntimeError(f"Installed {name} source differs from selected manifest")
    return {"agent": actual["agent"]["sha"], "runtime": actual["runtime"]["sha"],
            "schema_digest": actual["runtime"].get("schema_digest"),
            "tools": {name: row.get("version") for name, row in actual.get("tools", {}).items()
                      if isinstance(row, dict) and row.get("version")}}


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
                         "unrelated_sentinel": "not-run"}}
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
            healthy(data)
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
        capture_update_state = sys.platform == "win32" and args.leg == "stale-update"
        if capture_update_state:
            try:
                _capture_windows_pack_host_update_state(evidence, data, "before")
            except Exception as diagnostic_error:
                print(f"Journey Windows pack-host update capture (before) failed: "
                      f"{type(diagnostic_error).__name__}", file=sys.stderr, flush=True)
        try:
            bootstrap(app, manifest_path, args.shell, "update" if args.leg == "stale-update" else "install",
                      evidence, root, data, agent=getattr(args, "candidate_agent_checkout", None))
        except Exception:
            if args.leg == "fresh-install" and getattr(args, "mode", "direct") == "direct":
                capture_failed_windows_fresh_install(root, evidence)
            raise
        finally:
            if capture_update_state:
                try:
                    _capture_windows_pack_host_update_state(evidence, data, "after")
                except Exception as diagnostic_error:
                    print(f"Journey Windows pack-host update capture (after) failed: "
                          f"{type(diagnostic_error).__name__}", file=sys.stderr, flush=True)
        after_sentinel = sentinel_observation(sentinel, birth)
        result["sentinel"]["after_lifecycle"] = after_sentinel
        if not after_sentinel["alive"]:
            raise RuntimeError("Unrelated sentinel did not survive the lifecycle operation")
        version_path, data, version = installation(root, data)
        if old_data is not None and data != old_data:
            raise RuntimeError("Installer changed the selected data root")
        if getattr(args, "job_id", None) and args.mode == "direct":
            release = 1 if args.leg == "stale-update" and sys.platform in {"linux", "darwin"} else 0
            result["service_manager"] = assert_direct_service_ownership(root, data, release=release)
        if sys.platform == "win32":
            windows.EVIDENCE = evidence
            windows.powershell("Start-ScheduledTask -TaskName 'Astrid Runtime' -TaskPath '\\'",
                               "start.log")
        wait_timeout = int(os.environ.get("ASTRID_CI_WAIT_READY_TIMEOUT", "900"))
        wait_ready(data, timeout=wait_timeout)
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
        task_project = project if args.leg == "fresh-install" else f"tst-b1-{args.leg}"
        if sys.platform == "win32":
            windows.EVIDENCE = evidence
            windows.wait_for("pack-host is running with its current ready file", windows.host_ready)
            windows.snapshot("processes-host-ready.json")
            windows.task(task_project)
        else:
            try:
                task(version, evidence, task_project)
            except Exception:
                if (args.leg == "fresh-install" and getattr(args, "mode", "direct") == "direct"):
                    capture_failed_release1_task(evidence, version)
                raise
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
        if (getattr(args, "job_id", None) in {"direct-linux-fresh", "direct-macos-fresh"}
                and args.leg == "fresh-install" and getattr(args, "mode", "direct") == "direct"):
            if __package__:
                from . import journey_fresh_update
            else:
                import journey_fresh_update
            journey_fresh_update.execute(args, root, data, evidence, app, manifest, version_path,
                                         version, project, marker, digest, sentinel, birth, result)
        elif (sys.platform == "win32" and getattr(args, "job_id", None) == "direct-windows"
                and args.leg == "fresh-install" and args.mode == "direct"):
            if __package__:
                from . import windows_update_journey
            else:
                import windows_update_journey
            result["update"] = windows_update_journey.run_update(
                sys.modules[__name__], args, app, manifest, root, data, evidence,
                sentinel, birth, project, marker, digest)
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
    narrow_refusal = args.leg == "windows-upgrade-refusal"

    def stage_evidence(phase):
        if narrow_refusal:
            # Stage the receipt only; generic failure diagnostics inspect live processes.
            retain_named_evidence(evidence, phase)
        else:
            snapshot_failure_evidence(evidence, run_root, data, phase)
    keep_going = getattr(args, "diagnostic_keep_going", False)
    if keep_going:
        if __package__:
            from . import ci_journey_keepgoing as keepgoing
        else:
            import ci_journey_keepgoing as keepgoing
        keepgoing.write(evidence, keepgoing.initial(evidence, args))
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
    host_availability_diagnostic = None
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
        host_leg = "fresh-install" if args.leg in VERSION_JUMP_LEGS else args.leg
        if sys.platform == "win32":
            try:
                host_availability_diagnostic = windows_login_selection_diagnostic(root, data)
            except Exception as diagnostic_error:
                host_availability_diagnostic = {"capture_error": type(diagnostic_error).__name__}
        if narrow_refusal:
            prior_run = run_root.with_name("astrid-r1-direct-windows")
            related_selection = (prior_run / "program", prior_run / "data")
            assert_host_available(host_leg, root, data, skip_process_check=True,
                                  related_uninstalled_selection=related_selection)
        else:
            assert_host_available(host_leg, root, data)
    except Exception as exc:
        if keep_going:
            report = keepgoing.initial(evidence, args)
            report["stages"].append({"stage": "preflight", "result": "fail", "forced_continuation_used": False,
                                     "first_error": f"{type(exc).__name__}: {exc}", "evidence_path": str(evidence)})
            report["stages"].extend({"stage": name, "result": "not-run", "forced_continuation_used": False,
                                     "first_error": "Unsafe or unavailable preflight", "evidence_path": str(evidence)}
                                    for name in (keepgoing.FRESH_STAGES if args.leg == "fresh-install" else keepgoing.STAGES))
            keepgoing.write(evidence, report)
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
                       "unrelated_sentinel": "not-run"},
        }
        recorded.update(status="fail", error=f"{type(exc).__name__}: {exc}")
        if host_availability_diagnostic is not None:
            recorded["windows_login_selection_diagnostic"] = host_availability_diagnostic
        write_json(failure_path, recorded)
        print(f"Journey failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        stage_evidence("before-cleanup")
        raise
    try:
        with isolated_environment(run_root, root, data):
            with (nullcontext((None, None)) if narrow_refusal else sentinel_process(run_root, evidence)) as (sentinel, birth):
                primary_error = None
                try:
                    result = (keepgoing.execute(args, root, data, evidence, sentinel, birth)
                              if keep_going else _execute_owned(args, root, data, evidence, sentinel, birth))
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
                    stage_evidence("before-cleanup")
                    if not getattr(args, "retain_for_next_leg", False):
                        result_path = evidence / "journey-result.json"
                        try:
                            # A handed-off install keeps both data roots and refuses an
                            # automatic uninstall; the refusal is the cleanup contract there.
                            handed_off = False
                            if (root / "current").is_file():
                                _, _, installed = installation(root, data)
                                marker = (read_json(root / "install.json")
                                          if args.leg == "r0-handoff-then-update" else {})
                                handed_off = bool(marker.get("r1_data_root") or marker.get("retained_r0_data_root"))
                                if keep_going:
                                    release = read_json(Path(installed["root"]) / "manifest.json").get("release", 0)
                                    assert_cleanup_service_ownership(
                                        root, data, release=release,
                                        fresh_install=args.leg == "fresh-install")
                                if handed_off:
                                    refusal = assert_release1_upgrade_uninstall_refusal(installed, root, data, evidence)
                                    if result_path.is_file():
                                        recorded = read_json(result_path)
                                        recorded["upgrade_uninstall_refusal"] = refusal
                                        write_json(result_path, recorded)
                                else:
                                    installed_cli(installed, ["install", "--uninstall", "--root", str(root),
                                                              "--data-root", str(data)], evidence,
                                                  "final-uninstall.log", timeout=1800)
                                    if root.joinpath("current").exists():
                                        raise RuntimeError("Owned installation remained after final uninstall")
                            if not handed_off and not narrow_refusal and runtime_processes(data):
                                raise RuntimeError("Owned Runtime processes remained after final uninstall")
                            if not handed_off and sys.platform == "darwin":
                                assert_macos_services_removed(data)
                            cleanup_sentinel = None if narrow_refusal else sentinel_observation(sentinel, birth)
                            if result_path.is_file() and not narrow_refusal:
                                recorded = read_json(result_path)
                                recorded.setdefault("sentinel", {})["after_cleanup"] = cleanup_sentinel
                                write_json(result_path, recorded)
                            if handed_off:
                                assert_host_available("cleanup", root, data, release=1)
                            elif narrow_refusal:
                                assert_host_available("cleanup", root, data, skip_process_check=True)
                            else:
                                assert_host_available("cleanup", root, data)
                            if not narrow_refusal and not cleanup_sentinel["alive"]:
                                raise RuntimeError("Unrelated sentinel did not survive final uninstall")
                            cleanup_outcome = "retained-after-upgrade-refusal" if handed_off else "pass"
                            if result_path.is_file():
                                recorded = read_json(result_path)
                                recorded["final_cleanup"] = cleanup_outcome
                                write_json(result_path, recorded)
                            if "result" in locals():
                                result["final_cleanup"] = cleanup_outcome
                        except Exception as cleanup_error:
                            print(f"Journey cleanup failed: {type(cleanup_error).__name__}: {cleanup_error}",
                                  file=sys.stderr, flush=True)
                            try:
                                recorded = read_json(result_path) if result_path.is_file() else {
                                    "schema_version": 1, "scenario": args.leg}
                                recorded.update(status="fail", final_cleanup="fail",
                                                cleanup_error=f"{type(cleanup_error).__name__}: {cleanup_error}")
                                if sys.platform == "win32":
                                    try:
                                        recorded["cleanup_windows_login_selection_diagnostic"] = (
                                            windows_login_selection_diagnostic(root, data))
                                    except Exception as diagnostic_error:
                                        recorded["cleanup_windows_login_selection_diagnostic"] = {
                                            "capture_error": type(diagnostic_error).__name__}
                                write_json(result_path, recorded)
                            except Exception as receipt_error:
                                print(f"Journey cleanup receipt failed: {type(receipt_error).__name__}: {receipt_error}",
                                      file=sys.stderr, flush=True)
                            stage_evidence("after-cleanup")
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
                    if keep_going and "result" in locals() and result.get("final_cleanup") == "pass":
                        result["stages"].append({"stage": "final-cleanup", "result": "pass",
                                                 "forced_continuation_used": False, "first_error": None,
                                                 "evidence_path": str(evidence / "final-uninstall.log"),
                                                 "preconditions": [], "postconditions": []})
                        keepgoing.write(evidence, result)
                    stage_evidence("after-cleanup")
                return result
    except Exception as exc:
        # Context setup can fail before the lifecycle body has produced a receipt.
        if keep_going:
            report_path = evidence / "keepgoing-report.json"
            report = read_json(report_path) if report_path.is_file() else keepgoing.initial(evidence, args)
            report["stages"].append({"stage": "harness-or-cleanup", "result": "fail",
                                     "forced_continuation_used": False,
                                     "first_error": f"{type(exc).__name__}: {exc}", "evidence_path": str(evidence)})
            seen = {row["stage"] for row in report["stages"]}
            report["stages"].extend({"stage": name, "result": "not-run", "forced_continuation_used": False,
                                     "first_error": "Unsafe or unavailable harness context", "evidence_path": str(evidence)}
                                    for name in (keepgoing.FRESH_STAGES if args.leg == "fresh-install" else keepgoing.STAGES)
                                    if name not in seen)
            # Preserve any failure diagnostics already written to the result.
            saved_path = evidence / "journey-result.json"
            saved = read_json(saved_path) if saved_path.is_file() else {}
            keepgoing.write(evidence, report)
            diagnostic_receipt = read_json(saved_path)
            for key in ("cleanup_error", "final_cleanup", "failure_diagnostics"):
                if key in saved:
                    diagnostic_receipt[key] = saved[key]
            write_json(saved_path, diagnostic_receipt)
        result_path = evidence / "journey-result.json"
        recorded = read_json(result_path) if result_path.is_file() else {}
        if "before-cleanup" not in recorded.get("failure_diagnostics", {}) or recorded.get("status") != "fail":
            recorded.update(scenario=args.leg, status="fail",
                            error=f"{type(exc).__name__}: {exc}")
            if not keep_going:
                recorded.update(schema_version=2, receipt_kind="release1-installed-lifecycle")
            write_json(result_path, recorded)
            print(f"Journey failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            stage_evidence("before-cleanup")
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
                        help="the checked-in frozen release-0 fixture directory for the Windows upgrade refusal")
    parser.add_argument("--release9-app-checkout", type=Path,
                        help="a checkout containing the pinned Release-9 App commit for the direct 9-to-candidate leg")
    parser.add_argument("--allow-release0-to-release1", action="store_true",
                        help="admit only the exact pinned direct journey from its starting release to the candidate")
    parser.add_argument("--release11-app-checkout", type=Path,
                        help="a checkout containing the pinned Release-11 App commit for the direct 11-to-12 update leg")
    parser.add_argument("--allow-release11-to-candidate", action="store_true",
                        help="admit only the exact pinned direct update from the served Release 11 to the candidate")
    parser.add_argument("--source-override", action="store_true", help="TEST MODE: install Agent and Runtime from local Git commits")
    parser.add_argument("--agent-checkout", type=Path)
    parser.add_argument("--runtime-checkout", type=Path)
    parser.add_argument("--retain-for-next-leg", action="store_true",
                        help="keep only this run's installed service for the next named leg")
    parser.add_argument("--diagnostic-keep-going", action="store_true", help="NON-PROOF direct lifecycle diagnostic")
    parser.add_argument("--diagnostic-start-at", choices=("release0-api-seed",),
                        help="DIAGNOSTIC ONLY: run one retained release-0 stage, never qualification")
    parser.add_argument("--diagnostic-state", type=Path,
                        help="exact run-owned precondition checkpoint for --diagnostic-start-at")
    args = parser.parse_args(argv)
    diagnostic = args.diagnostic_start_at is not None
    if args.diagnostic_keep_going and (diagnostic or args.mode != "direct" or
            args.leg not in {"release9-then-install", "fresh-install"} or not args.job_id or args.source_override or args.retain_for_next_leg):
        parser.error("keep-going requires a disposable direct lifecycle matrix job")
    if (args.leg == "update-over-old-declaration") != (args.old_declaration is not None):
        parser.error("--old-declaration is required only for update-over-old-declaration")
    if bool(args.diagnostic_state) != diagnostic:
        parser.error("--diagnostic-state is required only with --diagnostic-start-at")
    if diagnostic:
        if args.job_id:
            parser.error("diagnostic stage starts cannot use a qualification job id")
        if args.mode != "direct" or args.leg != "release9-then-install":
            parser.error("diagnostic stage starts are limited to the direct release9-then-install leg")
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
    if sys.platform == "win32" and args.leg in {"release9-then-install", "r0-handoff-then-update"}:
        parser.error("Windows release-0 upgrade only admits the narrowed refusal leg")
    if args.leg == "r0-handoff-then-update" and (diagnostic or args.diagnostic_keep_going or args.retain_for_next_leg):
        parser.error("r0-handoff-then-update requires a disposable direct qualification job")
    if args.leg == "fresh-install-autostart-off":
        if sys.platform == "win32" or diagnostic or args.diagnostic_keep_going or args.retain_for_next_leg:
            parser.error("fresh-install-autostart-off requires a disposable direct qualification job on Linux or macOS")
        if not (args.job_id and args.mode == "direct" and args.candidate):
            parser.error("fresh-install-autostart-off requires a frozen direct candidate receipt")
        if (args.release0_fixture is not None or args.release9_app_checkout is not None
                or args.release11_app_checkout is not None or args.allow_release0_to_release1
                or args.allow_release11_to_candidate):
            parser.error("fresh-install-autostart-off admits no release source")
    if args.leg == "release11-then-update":
        if sys.platform == "win32" or diagnostic or args.diagnostic_keep_going or args.retain_for_next_leg:
            parser.error("release11-then-update requires a disposable direct qualification job on Linux or macOS")
        if not (args.job_id and args.mode == "direct" and args.candidate):
            parser.error("release11-then-update requires a frozen direct candidate receipt")
        if (args.release11_app_checkout is None or not args.allow_release11_to_candidate
                or args.release0_fixture is not None or args.release9_app_checkout is not None
                or args.allow_release0_to_release1):
            parser.error("release11-then-update requires --release11-app-checkout and --allow-release11-to-candidate, "
                         "and admits no release-0 or release-9 source")
    elif args.release11_app_checkout is not None or args.allow_release11_to_candidate:
        parser.error("release-11 admission is only valid for the release11-then-update leg")
    if args.leg in {"release9-then-install", "r0-handoff-then-update", "windows-upgrade-refusal"}:
        if args.leg == "windows-upgrade-refusal" and (sys.platform != "win32" or args.job_id != "direct-windows" or diagnostic or args.diagnostic_keep_going or args.retain_for_next_leg):
            parser.error("windows-upgrade-refusal requires the disposable direct-windows qualification job")
        if not ((diagnostic or args.job_id) and args.mode == "direct" and args.candidate):
            parser.error("release9-then-install requires a frozen direct candidate receipt")
        if args.leg == "release9-then-install":
            if args.release9_app_checkout is None or args.release0_fixture is not None or not args.allow_release0_to_release1:
                parser.error("release9-then-install requires --release9-app-checkout and --allow-release0-to-release1")
        elif args.leg == "r0-handoff-then-update":
            if args.release9_app_checkout is None or args.release0_fixture is None or not args.allow_release0_to_release1:
                parser.error("r0-handoff-then-update requires --release0-fixture, --release9-app-checkout "
                             "and --allow-release0-to-release1")
        elif args.release0_fixture is None or not args.allow_release0_to_release1 or args.release9_app_checkout is not None:
            parser.error("windows-upgrade-refusal requires --release0-fixture and --allow-release0-to-release1")
    elif args.release0_fixture is not None or args.allow_release0_to_release1 or args.release9_app_checkout is not None:
        parser.error("release-0 and release-9 admission is only valid for the release-0 and release-9 legs")
    if sys.platform not in {"linux", "darwin", "win32"}:
        parser.error("unsupported journey operating system")
    if sys.platform == "win32" and args.shell != "powershell.exe":
        parser.error("Windows hosted journeys require Windows PowerShell 5.1 (powershell.exe)")
    if sys.platform != "win32" and args.shell != "sh":
        parser.error("Linux/macOS require sh")
    try:
        with without_provider_credentials():
            execute(args)
        if args.diagnostic_keep_going:
            if __package__:
                from . import ci_journey_keepgoing as keepgoing
            else:
                import ci_journey_keepgoing as keepgoing
            evidence = args.run_root / "evidence" / args.leg
            command = [sys.executable, "-u", sys.argv[0], *raw_argv]
            try:
                recorded = keepgoing.record_expected_nonzero_exit(evidence, command)
            except Exception as exc:
                recorded = False
                print(f"Intentional diagnostic exit attribution failed: {type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)
            if recorded:
                print("Intentional diagnostic exit: all stages and cleanup passed, but the NON-PROOF "
                      "receipt is qualification-ineligible; returning exit code 1.",
                      file=sys.stderr, flush=True)
            return 1  # Diagnostic runs are never successful proof jobs.
    except Exception as exc:
        print(f"Journey failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
