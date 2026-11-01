"""Prepare, accept, execute, and report one selected F1 update job.

fetch_update_inputs is the single future verified-fetch integration hook. Local
byte digests bind selection to consumption, and make no upstream trust claim.
The worker runs the selected public bootstrap; the existing C2 installer remains
the sole owner of stop, switch, rollback, and recovery.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from runtime_protocol import platform

from . import tools
from .core import TERMINAL_STEPS, current, pending_transaction

if TYPE_CHECKING:
    from .backend import Backend
    from .core import Version

LABEL = "haus.astrid.runtime"


def absolute(value):
    from .core import absolute as resolve
    return resolve(value)


def read_json(path):
    from .core import read_json as read
    return read(path)


def write_json(path, value):
    from .core import write_json as write
    return write(path, value)


class _UnsupportedServiceManager(ValueError):
    """The selected OS cannot safely run an installer update job."""

    reason = "unsupported-service-manager"


MINIMUM_SYSTEMD_VERSION = 254
UNSUPPORTED_SERVICE_MANAGER_MESSAGE = "This update requires user systemd 254 or newer on Linux."


def update_job_availability(*, system: str | None = None) -> dict:
    """Return whether the selected OS can safely run an update job."""
    actual = sys.platform if system is None else system
    if actual != "linux":
        return {"available": True}
    try:
        version = subprocess.run(
            ["systemctl", "--user", "--version"], capture_output=True,
            text=True, timeout=5, check=False,
        )
        if version.returncode != 0:
            raise ValueError("systemd version command failed")
        output = version.stdout if isinstance(version.stdout, str) else ""
        match = re.match(r"^systemd\s+(\d+)(?:\s|$)", output.strip())
        if match is None or int(match.group(1)) < MINIMUM_SYSTEMD_VERSION:
            raise ValueError("systemd version is unsupported or unknown")
        manager = subprocess.run(
            ["systemctl", "--user", "show-environment"], capture_output=True,
            text=True, timeout=5, check=False,
        )
        if manager.returncode != 0:
            raise ValueError("systemd user manager is unavailable")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return {"available": False, "reason": _UnsupportedServiceManager.reason}
    return {"available": True}


def _require_update_job_available() -> None:
    if not update_job_availability()["available"]:
        raise _UnsupportedServiceManager(UNSUPPORTED_SERVICE_MANAGER_MESSAGE)

class _BusyRefusal(ValueError):
    """A reconciliation refusal that must remain visible as machine reason busy."""

    reason = "busy"


class _AcknowledgementUnknown(_BusyRefusal):
    """A submitted native attempt whose acknowledgement cannot be proved."""

    reason = "acknowledgement-unknown"


def _busy(message: str = "An update operation is already active or unresolved; try again after it finishes.") -> NoReturn:
    raise _BusyRefusal(message)

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_BOOTSTRAP_BYTES = 2 * 1024 * 1024
JOB_FORMAT = "astrid-update-job/v1"
JOB_STATES = {"accepted", "launching", "active", "updated", "rolled_back", "failed", "recovery-needed"}
BOOTSTRAP_PROOF_FORMAT = "astrid-update-bootstrap-proof/v1"
MAX_RESULT_LINE = 240
ROUTE_DISPOSITION_FORMAT = "astrid-update-route-disposition/v1"

# These are private Agent outcomes. Runtime owns their HTTP projection.
_ROUTE_DISPOSITIONS = {
    "accepted-new", "continued-existing", "recovered-this-request",
    "confirmed-prior", "acknowledgement-unknown",
}

_ATTEMPT_PHASES = {"reserved", "submitted", "claimed", "retired", "settled"}
_ATTEMPT_SUBMITS = {"not-issued", "spent", "acknowledged", "unknown"}


def _new_route_disposition() -> dict:
    """Start an unresolved operation with a durable held blocker."""
    return {
        "format": ROUTE_DISPOSITION_FORMAT,
        "blocker": "held",
        "disposition": "accepted-new",
        "confirmation": None,
    }


def _route_disposition(job: dict) -> dict | None:
    value = job.get("route_disposition")
    if not isinstance(value, dict) or value.get("format") != ROUTE_DISPOSITION_FORMAT:
        return None
    if value.get("blocker") not in {"held", "cleared"}:
        return None
    if value.get("disposition") not in _ROUTE_DISPOSITIONS:
        return None
    return value


def _with_route_disposition(job: dict, disposition: str, *, blocker: str,
                            confirmation: dict | None = None) -> dict:
    """Return the complete private route outcome to publish with this job write."""
    if disposition not in _ROUTE_DISPOSITIONS or blocker not in {"held", "cleared"}:
        raise ValueError("The update route disposition is invalid.")
    prior = _route_disposition(job)
    value = dict(prior) if prior is not None else _new_route_disposition()
    value.update({"format": ROUTE_DISPOSITION_FORMAT, "blocker": blocker,
                  "disposition": disposition, "confirmation": confirmation})
    return value


def _confirmed_route_disposition(root: Path, job: dict, state: str, message: str,
                                 disposition: str, ready_instance_id: str | None,
                                 ready_identity: dict | None) -> dict:
    """Build H3's evidence-bound private outcome before the atomic job write."""
    if disposition not in {"recovered-this-request", "confirmed-prior"}:
        raise ValueError("The update confirmation disposition is invalid.")
    transaction_id = job.get("transaction_id")
    if not isinstance(transaction_id, str) or not re.fullmatch(r"[0-9a-f]{32}", transaction_id):
        _busy("The confirmed update result has no valid transaction binding.")
    try:
        journal_sha256 = hashlib.sha256((root / "update-transaction.json").read_bytes()).hexdigest()
        selection_sha256 = hashlib.sha256(_selection_path().read_bytes()).hexdigest()
    except OSError as failure:
        raise _BusyRefusal("The confirmed update evidence cannot be retained.") from failure
    confirmation = {
        "operation_id": job.get("operation_id"), "outcome": state, "confirmed": True,
        "transaction_id": transaction_id, "journal_sha256": journal_sha256,
        "selection_sha256": selection_sha256, "ready_instance_id": ready_instance_id,
        "ready_identity": dict(ready_identity) if isinstance(ready_identity, dict) else None,
        "message": _one_line(message),
    }
    return _with_route_disposition(job, disposition, blocker="cleared",
                                   confirmation=confirmation)


def _helper_result(job: dict) -> dict:
    """Expose only additive Agent disposition facts to Runtime's helper caller."""
    result = dict(job)
    route = _route_disposition(job)
    if route is None:
        return result
    disposition = route["disposition"]
    result.update({"disposition": disposition, "blocker": route["blocker"]})
    confirmation = route.get("confirmation")
    if disposition in {"recovered-this-request", "confirmed-prior"}:
        if isinstance(confirmation, dict) and confirmation.get("confirmed") is True:
            result.update({"confirmed": True, "outcome": confirmation.get("outcome"),
                           "new_admission": False, "next_action": "observe"})
    elif disposition == "acknowledgement-unknown":
        result["next_action"] = "observe"
    return result


def _held_operation_id(root_value: object) -> str | None:
    """Read a response identifier without mutating or trusting malformed metadata."""
    try:
        root = absolute(root_value)
        job = read_json(root / "install.json").get("update_job")
        operation_id = job.get("operation_id") if isinstance(job, dict) else None
        return operation_id if isinstance(operation_id, str) and re.fullmatch(r"[0-9a-f]{32}", operation_id) else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _new_attempt(epoch: int) -> dict:
    """Create the private, immutable identity carried by one native wrapper."""
    if type(epoch) is not int or epoch < 1:
        raise ValueError("The update attempt epoch is invalid.")
    return {
        "epoch": epoch,
        "nonce": secrets.token_hex(16),
        "phase": "reserved",
        "submit": "not-issued",
        "wrapper_sha256": None,
        "descriptor_sha256": None,
        "handoff": None,
        "claim": None,
        "retirement": None,
    }


def _attempt(job: dict) -> dict:
    """Return the current native-attempt identity or refuse unsafe legacy data.

    An older wrapper must never be permitted to infer a replacement identity
    from the operation ID.  Missing private attempt evidence is consequently a
    refusal boundary rather than a compatibility fallback.
    """
    value = job.get("attempt")
    if not isinstance(value, dict):
        raise ValueError("The accepted update has no native attempt identity.")
    epoch, nonce = value.get("epoch"), value.get("nonce")
    if (type(epoch) is not int or epoch < 1
            or not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce)
            or value.get("phase") not in _ATTEMPT_PHASES
            or value.get("submit") not in _ATTEMPT_SUBMITS):
        raise ValueError("The accepted update native attempt identity is invalid.")
    for key in ("wrapper_sha256", "descriptor_sha256", "retirement"):
        if value.get(key) is not None and (not isinstance(value[key], str)
                                           or not re.fullmatch(r"[0-9a-f]{64}", value[key])):
            raise ValueError("The accepted update native attempt evidence is invalid.")
    if value.get("claim") is not None and not isinstance(value["claim"], dict):
        raise ValueError("The accepted update native claim is invalid.")
    return value


def _attempt_tuple(job: dict) -> tuple[int, str]:
    value = _attempt(job)
    return value["epoch"], value["nonce"]


def _attempt_matches(job: dict, operation_id: str, epoch: int, nonce: str) -> bool:
    try:
        return (job.get("operation_id") == operation_id
                and _attempt_tuple(job) == (epoch, nonce))
    except ValueError:
        return False


def _attempt_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _current_attempt(root: Path, operation_id: str, epoch: int, nonce: str, *,
                     submitted: bool = False) -> dict:
    """Reread the durable tuple at a native authority boundary.

    Callers already own the installer lock.  The extra reread deliberately
    fences a delayed callback or wrapper: it must fail before manager activity,
    claim, entry, or a job write when an explicit continuation has replaced its
    epoch.
    """
    job = _read_job(root, operation_id)
    if not _attempt_matches(job, operation_id, epoch, nonce):
        _busy("The native update attempt was replaced before it could run.")
    attempt = _attempt(job)
    if submitted and (attempt.get("phase") not in {"submitted", "claimed"}
                      or attempt.get("submit") not in {"spent", "acknowledged", "unknown"}):
        _busy("The native update submission is not durably authorized.")
    return job


def _selected_bytes(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file() or not platform.is_owner_only(path):
        raise ValueError("Selected update inputs must be private regular files.")
    with path.open("rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Selected update input is not regular.")
        raw = stream.read(limit + 1)
    if not raw or len(raw) > limit:
        raise ValueError("Selected update input is empty or too large.")
    return raw


def validate_update_inputs(directory: Path, metadata: dict, installed_release: int) -> dict:
    if directory.is_symlink() or not directory.is_dir() or not platform.is_owner_only(directory, directory=True):
        raise ValueError("Selected update inputs must be in a private directory.")
    values = {}
    for name, limit in (("manifest", MAX_MANIFEST_BYTES), ("bootstrap", MAX_BOOTSTRAP_BYTES)):
        raw = _selected_bytes(directory / name, limit)
        if not raw or len(raw) > limit or hashlib.sha256(raw).hexdigest() != metadata[name + "_sha256"]:
            raise ValueError("Selected update input changed or is invalid.")
        values[name] = raw
    manifest = tools.validate_manifest(json.loads(values["manifest"]))
    release = manifest.get("release", 0)
    if (type(installed_release) is not int or installed_release < 0
            or type(release) is not int or release <= installed_release or metadata["release"] != release):
        raise ValueError("The target release must be newer than the installed release.")
    return metadata


def _validate_input_paths(metadata: dict) -> tuple[Path, Path, Path]:
    manifest = absolute(metadata.get("manifest_path", ""))
    bootstrap = absolute(metadata.get("bootstrap_path", ""))
    directory = manifest.parent
    if manifest != directory / "manifest" or bootstrap != directory / "bootstrap":
        raise ValueError("Selected update input paths do not match their fixed names.")
    if bootstrap.parent != directory:
        raise ValueError("Selected update inputs must share a private directory.")
    return directory, manifest, bootstrap


def _windows_bootstrap_script(metadata: dict, operation_id: str) -> Path:
    """Copy the selected bytes to a private .ps1 path for PowerShell -File."""
    directory, _, selected = _validate_input_paths(metadata)
    expected = metadata.get("bootstrap_sha256")
    raw = _selected_bytes(selected, MAX_BOOTSTRAP_BYTES)
    if not isinstance(expected, str) or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("The selected bootstrap changed before execution.")
    target = directory / f"bootstrap-{operation_id}.ps1"
    if target.exists() or target.is_symlink():
        raise ValueError("The private PowerShell bootstrap handoff already exists.")
    try:
        with target.open("xb") as stream:
            platform.owner_only(stream.fileno())
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
        raise
    copied = _selected_bytes(target, MAX_BOOTSTRAP_BYTES)
    if hashlib.sha256(copied).hexdigest() != expected:
        target.unlink(missing_ok=True)
        raise ValueError("The PowerShell bootstrap handoff changed before execution.")
    return target


def _validate_existing_windows_worker(old: Version, job: dict) -> None:
    """Reuse an existing Windows worker only when its exact private bytes match."""
    if sys.platform != "win32":
        return
    metadata = job.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("The accepted update job has no private worker directory.")
    directory, _, _ = _validate_input_paths(metadata)
    epoch, nonce = _attempt_tuple(job)
    worker_script = directory / f"worker-{job['operation_id']}-{epoch}-{nonce}.py"
    if worker_script.is_symlink():
        raise ValueError("The private update worker script is a link.")
    if not worker_script.exists():
        return
    if not worker_script.is_file() or not platform.is_owner_only(worker_script):
        raise ValueError("The existing private update worker script is not owner-only regular data.")
    expected = (_worker_command_source(old, job) + "\n").encode("utf-8")
    if worker_script.read_bytes() != expected:
        raise ValueError("The existing private update worker script does not match the accepted command.")


def _bootstrap_command(bootstrap: Path, args: list[str], *, system: str | None = None) -> list[str]:
    system = system or sys.platform
    if system == "win32":
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(bootstrap), *args]
    return ["/bin/sh", str(bootstrap), *args]


def fetch_update_inputs(directory: Path, *, manifest_url: str, bootstrap_url: str,
                        installed_release: int) -> dict:
    """Download once and validate with today's optional-hash policy.

    URLs are supplied only by Runtime's fixed server policy. No environment URL
    override, signature implementation, or verification bypass is introduced.
    Callers own this private directory for the full selection lifetime.
    """
    platform.private_directory(directory)
    metadata = {}
    for name, url in (("manifest", manifest_url), ("bootstrap", bootstrap_url)):
        path = directory / name
        # tools.download opens/truncates this owner-only file in place.
        descriptor, temporary = platform.private_temporary(directory, name + "-")
        os.close(descriptor)
        Path(temporary).replace(path)
        tools.download(url, path)
        raw = _selected_bytes(path, MAX_MANIFEST_BYTES if name == "manifest" else MAX_BOOTSTRAP_BYTES)
        metadata[name + "_path"] = str(path)
        metadata[name + "_sha256"] = hashlib.sha256(raw).hexdigest()
        if name == "manifest":
            manifest = tools.validate_manifest(json.loads(raw))
            metadata["release"] = manifest.get("release", 0)
            release = metadata["release"]
            if (type(installed_release) is not int or installed_release < 0
                    or type(release) is not int or release <= installed_release):
                raise ValueError("The target release must be newer than the installed release.")
    return validate_update_inputs(directory, metadata, installed_release)


def _locked(root: Path, *, blocking: bool = False):
    """Acquire the installer-wide lock used by normal install/repair callers."""
    from .service import installer_lock_path
    lock_path = installer_lock_path(Path.home(), sys.platform)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    platform.private_directory(lock_path.parent)
    stream = lock_path.open("a+b")
    try:
        platform.lock_file(stream.fileno(), blocking=blocking)
    except BlockingIOError:
        stream.close()
        raise _BusyRefusal("The Astrid installer is already running.") from None
    return stream


def _unlock(stream) -> None:
    try:
        platform.unlock_file(stream.fileno())
    finally:
        stream.close()


def _one_line(value: object) -> str:
    raw = str(value or "")
    normalized = "".join(" " if ord(char) < 32 or 127 <= ord(char) <= 159 else char
                         for char in raw)
    line = " ".join(normalized.split()).strip()
    return line[:MAX_RESULT_LINE] or "Update did not complete."


def _job_message(job: dict) -> str:
    value = job.get("message")
    return value if isinstance(value, str) else "Update did not complete."


def autostart_off(value: object) -> bool:
    return value is False or (isinstance(value, str) and value.casefold() == "off")


def _selection_path() -> Path:
    from .service import install_selection
    return install_selection(Path.home(), sys.platform)


class _IdentityUnavailable(ValueError):
    """Discovery lacks the endpoint or instance id needed to probe Runtime identity."""


def _probe_identity(data_root: Path) -> tuple[str, dict]:
    discovery = read_json(data_root / "runtime" / "discovery.json")
    instance_id = discovery.get("runtime_instance_id")
    endpoint = discovery.get("endpoint") or discovery.get("door_endpoint")
    if not isinstance(instance_id, str) or not instance_id or not isinstance(endpoint, str) or not endpoint:
        raise _IdentityUnavailable("The live Runtime owner identity is unavailable.")
    from astrid_runtime.runtime_boundary import LocalRuntimeBoundary
    health = LocalRuntimeBoundary().owner_startup_identity(endpoint=endpoint, instance_id=instance_id)
    from astrid_runtime.identity import validate_live_owner_identity
    identity = validate_live_owner_identity(health, instance_id=instance_id)
    return instance_id, identity


def _probe_startup_identity(data_root: Path) -> tuple[str, dict]:
    """The success and failure paths keep the startup wording for unavailable identity."""
    try:
        return _probe_identity(data_root)
    except _IdentityUnavailable:
        raise ValueError("The Runtime startup identity is unavailable.") from None


def _validate_request(request: dict, root: Path) -> dict:
    from .core import Version, current
    if not isinstance(request, dict):
        raise ValueError("An update request object is required.")
    operation_id = request.get("operation_id")
    if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ValueError("The update operation identifier is invalid.")
    selected_root = absolute(request.get("root", ""))
    data_root = absolute(request.get("data_root", ""))
    selection_path = absolute(request.get("selection_path", ""))
    if selected_root != root or selection_path != _selection_path():
        raise ValueError("The update request does not match this installation selection.")
    marker = read_json(root / "install.json")
    selected = read_json(selection_path)
    if (marker.get("owner") != LABEL or selected.get("owner") != LABEL
            or marker.get("managed_by") is not None or selected.get("managed_by") is not None
            or absolute(marker.get("data_root", "")) != data_root
            or absolute(selected.get("program_root", "")) != root
            or absolute(selected.get("data_root", "")) != data_root
            or selected.get("uninstalled")):
        raise ValueError("The selected installer ownership does not match the update request.")
    old = current(root)
    if old is None or not old.python.is_file():
        raise ValueError("The selected Astrid installation is no longer available.")
    old_version = request.get("old_version")
    if isinstance(old_version, dict):
        expected_version = Version.load(old_version).document()
        if old.document() != expected_version:
            raise ValueError("The selected Astrid version changed before acceptance.")
    elif old_version not in {old.root.name, str(old.root), old.document().get("identity", {}).get("release")}:
        raise ValueError("The selected Astrid version changed before acceptance.")
    old_identity = request.get("old_identity")
    if not isinstance(old_identity, dict):
        raise ValueError("The selected live identity is missing.")
    # Compare the immutable fields represented in the installer version. The
    # separately captured runtime instance ID prevents a request from binding
    # to a successor process that happens to use the same version.
    recorded_identity = {"release": old.release, "managed_by": old.managed_by}
    for key, value in recorded_identity.items():
        if old_identity.get(key) != value:
            raise ValueError("The selected live identity changed before acceptance.")
    old_instance_id = request.get("old_instance_id")
    if not isinstance(old_instance_id, str) or not old_instance_id or len(old_instance_id) > 128:
        raise ValueError("The selected live instance is missing.")
    observed_instance, observed_identity = _probe_identity(data_root)
    if observed_instance != old_instance_id or observed_identity != old_identity:
        raise ValueError("The selected live Runtime identity changed before acceptance.")
    metadata = request.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("Selected update inputs are missing.")
    # Accept the runtime's explicit path names and the preparation hook's
    # returned names. In either case, validate the same selected private bytes.
    manifest_path = Path(metadata.get("manifest_path", ""))
    bootstrap_path = Path(metadata.get("bootstrap_path", ""))
    normalized = {
        "manifest_path": str(absolute(manifest_path)),
        "bootstrap_path": str(absolute(bootstrap_path)),
        "manifest_sha256": metadata.get("manifest_sha256"),
        "bootstrap_sha256": metadata.get("bootstrap_sha256"),
        "release": metadata.get("release"),
    }
    for name in ("manifest_sha256", "bootstrap_sha256"):
        digest = normalized[name]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Selected update input identities are invalid.")
    directory, _, _ = _validate_input_paths(normalized)
    validate_update_inputs(directory, {
        "manifest_sha256": normalized["manifest_sha256"],
        "bootstrap_sha256": normalized["bootstrap_sha256"],
        "release": normalized["release"],
    }, old.release)
    job_name = request.get("job_name")
    if not isinstance(job_name, str) or not job_name or len(job_name) > 160 or any(ord(c) < 32 for c in job_name):
        raise ValueError("The update job name is invalid.")
    if job_name != f"astrid-update-{operation_id}":
        raise ValueError("The update job name must be unique to its operation.")
    log_path = absolute(request.get("log_path") or root / "logs" / f"update-{operation_id}.log")
    if not log_path.is_relative_to(root):
        raise ValueError("The update log must remain inside the selected installation.")
    if log_path.exists() and log_path.is_symlink():
        raise ValueError("The update log cannot be a link.")
    return {
        "format": JOB_FORMAT, "operation_id": operation_id, "state": "accepted",
        "job_name": job_name, "log_path": str(log_path), "program_root": str(root),
        "data_root": str(data_root), "old_instance_id": old_instance_id,
        "old_identity": dict(old_identity), "old_version": old.document(),
        "target_release": normalized["release"], "metadata": normalized,
        "bootstrap_proof": {"format": BOOTSTRAP_PROOF_FORMAT,
                            "operation_id": operation_id, "program_root": str(root),
                            "data_root": str(data_root), "owner": LABEL,
                            "state": "never-entered", "claim_state": "unclaimed"},
        "route_disposition": _new_route_disposition(),
        # This tuple is carried by the native wrapper itself.  It is distinct
        # from the stable logical operation ID so a later continuation can
        # revoke a delayed old wrapper without changing the operation.
        "attempt": _new_attempt(1),
        "retired_attempts": [],
        "accepted_at": int(time.time()), "message": "Update accepted.",
    }


def _reconcile_request_binding(request: dict, root: Path, job: dict) -> Path:
    """Check the new click still names the installation before any recovery."""
    try:
        data_root = absolute(request.get("data_root", ""))
        selection_path = absolute(request.get("selection_path", ""))
        if absolute(request.get("root", "")) != root or selection_path != _selection_path():
            _busy("The update request does not match the selected installation.")
        marker = read_json(root / "install.json")
        selected = read_json(selection_path)
        if (marker.get("owner") != LABEL or marker.get("managed_by") is not None
                or selected.get("owner") != LABEL or selected.get("managed_by") is not None
                or marker.get("uninstalled") or selected.get("uninstalled")
                or absolute(marker.get("data_root", "")) != data_root
                or absolute(selected.get("program_root", "")) != root
                or absolute(selected.get("data_root", "")) != data_root
                or data_root != absolute(job.get("data_root", ""))):
            _busy("The selected installer ownership does not match the unresolved update.")
        return data_root
    except _BusyRefusal:
        raise
    except (OSError, ValueError, TypeError, KeyError) as failure:
        raise _BusyRefusal("The selected installation ownership cannot be proved.") from failure


def _validate_reconcile_job(root: Path, job: dict, request: dict, api=None) -> tuple[Path, dict]:
    data_root = _reconcile_request_binding(request, root, job)
    operation_id = job.get("operation_id")
    if (job.get("format") != JOB_FORMAT or not isinstance(operation_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", operation_id)
            or job.get("job_name") != f"astrid-update-{operation_id}"
            or absolute(job.get("program_root", "")) != root
            or absolute(job.get("data_root", "")) != data_root):
        _busy("The unresolved update is not bound to this installation.")
    descriptor = job.get("platform")
    api = api or _platform_api()
    try:
        from .core import Version
        old = Version.load(job["old_version"])
        expected_command = _expected_worker_command(old, job)
        if (job.get("worker_command_sha256") is not None
                and job["worker_command_sha256"] != _worker_binding_digest(old, job)):
            _busy("The accepted update worker command changed after acceptance.")
        expected = api.describe(job, expected_command)
    except _BusyRefusal:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as failure:
        raise _BusyRefusal("The unresolved update native command cannot be proved.") from failure
    if descriptor is not None and (not _owned_descriptor(descriptor, operation_id) or descriptor != expected):
        _busy("The unresolved update native descriptor does not match its accepted command.")
    return data_root, expected


def _bootstrap_proof_valid(job: dict) -> bool:
    proof = job.get("bootstrap_proof")
    if not isinstance(proof, dict):
        return False
    valid = (proof.get("format") == BOOTSTRAP_PROOF_FORMAT
            and proof.get("operation_id") == job.get("operation_id")
            and proof.get("program_root") == job.get("program_root")
            and proof.get("data_root") == job.get("data_root")
            and proof.get("owner") == LABEL
            and proof.get("state") == "never-entered"
            and isinstance(proof.get("claim_state"), str)
            and proof.get("claim_state") in {"unclaimed", "claimed"})
    if not valid:
        return False
    if proof.get("claim_state") == "unclaimed":
        return proof.get("native_claim") is None
    claim = proof.get("native_claim")
    return (isinstance(claim, dict)
            and claim.get("operation_id") == job.get("operation_id")
            and claim.get("program_root") == job.get("program_root")
            and claim.get("data_root") == job.get("data_root")
            and claim.get("owner") == LABEL
            and isinstance(claim.get("descriptor"), dict)
            and claim.get("descriptor") == job.get("platform"))


def _journal_is_accepted_preexisting(root: Path, job: dict, journal: dict | None) -> bool:
    """Recognize only the exact journal snapshot observed during acceptance."""
    if journal is None:
        return (job.get("prior_transaction_present") is False
                and "prior_transaction_id" in job and "prior_transaction_sha256" in job
                and job.get("prior_transaction_id") is None
                and job.get("prior_transaction_sha256") is None)
    try:
        if job.get("prior_transaction_hash_kind") != "raw-sha256/v1":
            return False
        encoded = (root / "update-transaction.json").read_bytes()
    except OSError:
        return False
    digest = hashlib.sha256(encoded).hexdigest()
    return (job.get("prior_transaction_present") is True
            and job.get("prior_transaction_id") == journal.get("operation_id")
            and job.get("prior_transaction_sha256") == digest)


def _validate_prebootstrap(root: Path, job: dict, data_root: Path) -> Version:
    """Revalidate accepted old owner and private inputs before replacing a native job."""
    from .core import current
    if not _bootstrap_proof_valid(job):
        _busy("The update bootstrap-entry proof is missing or unresolved.")
    try:
        old, selected_data = _validate_worker_binding(root, job)
        if selected_data != data_root or current(root) is None:
            _busy("The accepted old installation is no longer selected.")
        proof = job["bootstrap_proof"]
        if (job.get("state") == "active" and proof.get("claim_state") != "claimed"
                or proof.get("claim_state") == "claimed"
                and job.get("state") not in {"active", "recovery-needed"}):
            _busy("The update claim state does not match its durable bootstrap proof.")
        if job.get("worker_command_sha256") != _worker_binding_digest(old, job):
            _busy("The accepted update worker command cannot be proved.")
        selection = read_json(_selection_path())
        preferences = selection.get("preferences")
        autostart = preferences.get("autostart") if isinstance(preferences, dict) else None
        if autostart_off(autostart) or autostart_off(job.get("autostart_snapshot")):
            _busy("Saved autostart prevents this update from continuing.")
        metadata = job.get("metadata")
        if (not isinstance(metadata, dict)
                or metadata.get("release") != job.get("target_release")):
            _busy("The accepted update inputs cannot be proved.")
        validate_update_inputs(_validate_input_paths(metadata)[0], metadata, old.release)
        log_path = Path(job.get("log_path", ""))
        if (not log_path.is_absolute() or not log_path.is_relative_to(root)
                or log_path.is_symlink() or not log_path.is_file()
                or not platform.is_owner_only(log_path)
                or log_path.parent.is_symlink()
                or not platform.is_owner_only(log_path.parent, directory=True)):
            _busy("The accepted update log is not private regular data inside the installation.")
        _validate_existing_windows_worker(old, job)
        return old
    except _BusyRefusal:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as failure:
        raise _BusyRefusal("The accepted old version, live Runtime, or private update inputs changed.") from failure


def _continue_prebootstrap(root: Path, job: dict, descriptor: dict,
                           old: Version, registration_present: bool, api=None) -> dict:
    """Retire one fenced attempt and reserve its same-ID replacement under L."""
    api = api or _platform_api()
    operation_id = job["operation_id"]
    old_epoch, old_nonce = _attempt_tuple(job)
    _current_attempt(root, operation_id, old_epoch, old_nonce)
    try:
        command = _worker_command(old, job, inactive_reconciliation=True)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as failure:
        raise _BusyRefusal("The private update worker command cannot be safely prepared.") from failure
    retirement = {
        "operation_id": operation_id,
        "epoch": old_epoch,
        "nonce": old_nonce,
        "registration": "absent" if not registration_present else "owned-inactive",
    }
    if registration_present:
        try:
            # This was positively observed inactive immediately before cleanup.
            # A manager error, a still-present object, or an ambiguous readback
            # leaves the old authority unresolved and blocks replacement.
            _current_attempt(root, operation_id, old_epoch, old_nonce)
            api.cleanup(descriptor)
            _current_attempt(root, operation_id, old_epoch, old_nonce)
            if api.is_registered(descriptor, command) is not False:
                _busy("The inactive update registration did not reach an authoritative absence barrier.")
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as failure:
            raise _BusyRefusal("The inactive update registration could not be safely retired.") from failure
        retirement["registration"] = "retired-absent"
    old_attempt = dict(_attempt(job))
    old_attempt.update({"phase": "retired", "retirement": _attempt_digest(retirement)})
    retired_attempts = job.get("retired_attempts")
    if not isinstance(retired_attempts, list):
        _busy("The accepted update retirement history is invalid.")
    replacement = _new_attempt(old_epoch + 1)
    proof = dict(job["bootstrap_proof"])
    proof.pop("native_claim", None)
    proof.update({"claim_state": "unclaimed", "continuation_at": int(time.time())})
    job = _write_job(root, operation_id, state="accepted",
                     message="Continuing the accepted update operation.",
                     fields={"platform": None, "launch_status": "not-issued",
                             "cleanup_status": "not-needed", "cleanup_error": None,
                             "bootstrap_proof": proof, "attempt": replacement,
                             "retired_attempts": [*retired_attempts, old_attempt],
                             "worker_command_sha256": _worker_binding_digest(old, {
                                 **job, "attempt": replacement}),
                             "route_disposition": _with_route_disposition(
                                 job, "continued-existing", blocker="held"),
                             "continuation_attempted_at": int(time.time())}, allow_reconcile=True)
    return _launch_current_attempt(root, job, old, api, continuation=True)


def _matching_journal(job: dict, journal: dict) -> bool:
    binding = journal.get("update_job") if isinstance(journal, dict) else None
    transaction_id = journal.get("operation_id") if isinstance(journal, dict) else None
    if not (isinstance(binding, dict)
            and binding.get("operation_id") == job.get("operation_id")
            and transaction_id == job.get("transaction_id")
            and isinstance(transaction_id, str) and re.fullmatch(r"[0-9a-f]{32}", transaction_id)
            and transaction_id != job.get("prior_transaction_id")
            and journal.get("format") == "astrid-install-update/v1"
            and absolute(journal.get("data_root", "")) == absolute(job.get("data_root", ""))
            and journal.get("old") == job.get("old_version")
            and journal.get("transaction_kind") in {"migration", "app"}
            and isinstance(journal.get("realm"), dict)
            and isinstance(journal["realm"].get("realm_id"), str)
            and bool(journal["realm"].get("realm_id"))
            and isinstance(journal["realm"].get("realm_root"), str)
            and journal.get("step") in {"staged", "stopping", "stopped", "backed_up", "migrated",
                                         "switched", "verified", "recovering", "rollback_selected",
                                         "complete", "rolled_back"}
            and isinstance(journal.get("candidate"), dict)
            and journal.get("candidate", {}).get("identity", {}).get("release") == job.get("target_release")):
        return False
    if (journal.get("step") == "complete"
            and journal.get("outcome") not in {None, "stop_refused", "recovered_before_stop"}):
        return False
    try:
        from .core import Version
        old, candidate = Version.load(journal["old"]), Version.load(journal["candidate"])
        root = absolute(job["program_root"])
        old_identity = job.get("old_identity")
        return (old.root == absolute(root / "versions" / old.root.name)
                and candidate.root == absolute(root / "versions" / candidate.root.name)
                and old.document() == job.get("old_version")
                and old.managed_by is None
                and isinstance(old_identity, dict)
                and old_identity == {"release": old.release, "managed_by": old.managed_by}
                and candidate.release == job.get("target_release")
                and candidate.managed_by is None
                and candidate.root != old.root)
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _journal_result(job: dict, journal: dict) -> tuple[dict, dict, str, str]:
    if journal.get("step") == "rolled_back" or journal.get("outcome") == "recovered_before_stop":
        version, identity = job.get("old_version"), job.get("old_identity")
        if not isinstance(version, dict) or not isinstance(identity, dict):
            _busy("The prior version identity is incomplete.")
        return (version, identity, "rolled_back",
                "Recovered your previous version — run Update again")
    if journal.get("outcome") == "stop_refused":
        version, identity = job.get("old_version"), job.get("old_identity")
        if not isinstance(version, dict) or not isinstance(identity, dict):
            _busy("The prior version identity is incomplete.")
        return (version, identity, "failed",
                "Failed: Update was refused before stopping Astrid.")
    candidate = journal.get("candidate")
    if not isinstance(candidate, dict):
        _busy("The candidate version identity is incomplete.")
    return (candidate, {"release": job.get("target_release"), "managed_by": None},
            "updated", f"Updated to release {job.get('target_release')}.")


def _verify_journal_result_ready(root: Path, data_root: Path, job: dict, journal: dict,
                                 expected: dict, identity: dict, state: str) -> tuple[Version, str, dict]:
    selected, instance_id, observed_identity = _verify_selected_ready(
        root, data_root, expected, identity, journal.get("realm"))
    requires_new_instance = (state == "updated"
                             or (state == "rolled_back" and journal.get("step") == "rolled_back"))
    if requires_new_instance and instance_id == job.get("old_instance_id"):
        raise ValueError("The recovered Runtime instance was not restarted.")
    return selected, instance_id, observed_identity


def _verify_selected_ready(root: Path, data_root: Path, expected_version: dict,
                           expected_identity: dict, expected_realm: dict | None = None) -> tuple[Version, str, dict]:
    """Verify the selected installer image and the live Runtime owner."""
    from .backend import Backend
    from .core import current
    selected = current(root)
    if selected is None or selected.document() != expected_version:
        raise ValueError("The reconciled update selected an unexpected version.")
    backend = Backend(root, root / "logs" / "install.log", root / "bootstrap" / "uv", say=lambda _line: None,
                      managed_by=None)
    workspace = backend.workspace(selected, data_root)
    realm_id = workspace.get("realm_id")
    if not isinstance(realm_id, str) or not realm_id:
        raise ValueError("The selected workspace has no valid Runtime identity.")
    if expected_realm is not None:
        if (realm_id != expected_realm.get("realm_id")
                or absolute(workspace.get("realm_root", "")) != absolute(expected_realm.get("realm_root", ""))):
            raise ValueError("The selected workspace differs from the transaction realm.")
    backend.verify(selected, data_root, realm_id)
    instance_id, identity = _probe_startup_identity(data_root)
    if not isinstance(instance_id, str) or not instance_id or identity != expected_identity:
        raise ValueError("The reconciled Runtime identity does not match its selected version.")
    return selected, instance_id, identity


def _prove_recovery_owner(data_root: Path, expected_identity: dict, expected_realm: dict) -> str:
    """Classify the Runtime owner and bind a live one before recovery writes."""
    from astrid_runtime.paths import RuntimePaths
    from astrid_runtime.runtime_boundary import LocalRuntimeBoundary

    paths = RuntimePaths.current(data_root=data_root)
    if (not isinstance(expected_realm, dict)
            or not isinstance(expected_realm.get("realm_id"), str)
            or not expected_realm.get("realm_id")
            or not isinstance(expected_realm.get("realm_root"), str)):
        raise ValueError("The matching transaction has no valid Runtime realm identity.")
    realm_root = absolute(expected_realm["realm_root"])
    snapshots = []
    records: list[dict | None] = []
    for path in (paths.discovery_path, paths.instance_lock_path):
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raw = None
        except OSError as failure:
            raise ValueError("Runtime owner evidence cannot be read.") from failure
        snapshots.append(raw)
        if raw is None:
            records.append(None)
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as failure:
            raise ValueError("Runtime owner evidence is malformed.") from failure
        if not isinstance(record, dict):
            raise ValueError("Runtime owner evidence is malformed.")
        records.append(record)
    discovery, owner_lock = records
    if discovery is None or owner_lock is None:
        # Without both publications there is no recorded PID incarnation to
        # positively observe; absence alone is not proof that Runtime stopped.
        raise ValueError("Runtime discovery and instance lock are both required for a stopped proof.")
    shared = ("pid", "process_birth_id", "runtime_instance_id", "realm_root")
    if (any(discovery.get(key) != owner_lock.get(key) for key in shared)
            or discovery.get("active_realm") != owner_lock.get("realm_id")):
        raise ValueError("Runtime discovery and instance lock do not identify the same owner.")
    pid, expected_birth = discovery.get("pid"), discovery.get("process_birth_id")
    instance_id = discovery.get("runtime_instance_id")
    if (type(pid) is not int or pid <= 0 or not isinstance(expected_birth, str) or not expected_birth
            or not isinstance(instance_id, str) or not instance_id
            or discovery.get("active_realm") != expected_realm["realm_id"]
            or absolute(discovery.get("realm_root", "")) != realm_root):
        raise ValueError("Runtime owner records do not match the transaction realm.")
    actual_birth = LocalRuntimeBoundary.process_birth_identity(pid)
    if actual_birth is None:
        state = "stopped"  # The exact advertised process incarnation is positively absent.
    elif actual_birth != expected_birth:
        raise ValueError("The Runtime owner PID was reused; the recorded incarnation is unresolved.")
    else:
        endpoint = discovery.get("endpoint") or discovery.get("door_endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError("The live Runtime owner endpoint is incomplete.")
        boundary = LocalRuntimeBoundary()
        if not boundary.validate_owner(endpoint=endpoint, pid=pid, instance_id=instance_id,
                                       owner_lock=paths.instance_lock_path,
                                       process_birth_id=expected_birth,
                                       expected_realm_id=expected_realm["realm_id"],
                                       expected_realm_root=realm_root):
            raise ValueError("The live Runtime owner could not be validated.")
        observed_instance, identity = _probe_identity(data_root)
        if observed_instance != instance_id or identity != expected_identity:
            raise ValueError("The live Runtime identity differs from the selected transaction version.")
        state = "live"
    for path, snapshot in zip((paths.discovery_path, paths.instance_lock_path), snapshots):
        try:
            current_bytes = path.read_bytes()
        except FileNotFoundError:
            current_bytes = None
        if current_bytes != snapshot:
            raise ValueError("Runtime owner evidence changed during reconciliation.")
    return state


def _set_reconciled_job(root: Path, job: dict, state: str, message: str, *,
                        disposition: str | None = None,
                        ready_instance_id: str | None = None,
                        ready_identity: dict | None = None) -> dict:
    """Allow only explicit reconciliation to resolve the unresolved state."""
    marker_path = root / "install.json"
    marker = read_json(marker_path)
    latest = marker.get("update_job")
    if (not isinstance(latest, dict) or latest.get("operation_id") != job.get("operation_id")
            or latest.get("state") not in {"accepted", "launching", "active", "recovery-needed"}):
        _busy("The unresolved update record changed during reconciliation.")
    latest["state"] = state
    latest["message"] = _one_line(message)
    latest["finished_at"] = int(time.time())
    if disposition is not None:
        latest["route_disposition"] = _confirmed_route_disposition(
            root, latest, state, latest["message"], disposition,
            ready_instance_id, ready_identity)
    marker["update_job"] = latest
    write_json(marker_path, marker)
    return latest


def _archive_prior_job(marker: dict, job: dict, journal: dict | None = None) -> None:
    history = marker.get("update_job_history", [])
    if not isinstance(history, list):
        history = []
    if not any(isinstance(item, dict) and item.get("operation_id") == job.get("operation_id")
               for item in history):
        record = dict(job)
        if isinstance(journal, dict) and _matching_journal(job, journal):
            record["transaction"] = journal
        history.append(record)
    marker["update_job_history"] = history


def _recover_matching_transaction(root: Path, job: dict, journal: dict) -> dict:
    """Recover one already-bound transaction and prove its resulting owner."""
    from .backend import Backend
    from .core import Installer, Version, current
    data_root = absolute(job["data_root"])
    old = Version.load(job["old_version"])
    candidate = Version.load(journal["candidate"])
    if (old.root != absolute(root / "versions" / old.root.name)
            or candidate.root != absolute(root / "versions" / candidate.root.name)
            or candidate.release != job.get("target_release")
            or candidate.root == old.root):
        _busy("The matching update journal names an unexpected version image.")
    selected = current(root)
    if selected is None or selected.document() not in (old.document(), candidate.document()):
        _busy("The selected version is not part of the matching update transaction.")
    backend = Backend(root, root / "logs" / "install.log", root / "bootstrap" / "uv", say=lambda _line: None)
    try:
        expected_live_identity = (job.get("old_identity") if selected.document() == old.document()
                                  else {"release": job.get("target_release"), "managed_by": None})
        expected_realm = journal.get("realm")
        if not isinstance(expected_live_identity, dict) or not isinstance(expected_realm, dict):
            _busy("The matching update has incomplete Runtime identity evidence.")
        owner_state = _prove_recovery_owner(data_root, expected_live_identity, expected_realm)
        # A staged transaction with no backup never stopped the old owner; it
        # is safe to confirm only when that original owner is ready. Later
        # phases may have stopped Runtime, so C1's read-only stop preflight is
        # the authority for whether recovery may proceed.
        if journal.get("step") == "staged" and journal.get("backup") is None:
            if selected.document() != old.document() or owner_state != "live":
                raise ValueError("The pre-stop transaction no longer has its accepted Runtime owner.")
            old_identity = job.get("old_identity")
            if not isinstance(old_identity, dict):
                raise ValueError("The pre-stop transaction has no accepted Runtime identity.")
            _verify_selected_ready(root, data_root, old.document(), old_identity, journal.get("realm"))
        else:
            backend.preflight_stop(data_root)
        Installer(root, data_root, backend, say=lambda _line: None)._recover_update()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as failure:
        raise _BusyRefusal("The matching update transaction could not be safely recovered.") from failure
    result_journal = read_json(root / "update-transaction.json")
    if (not isinstance(result_journal, dict) or result_journal.get("step") not in TERMINAL_STEPS
            or not _matching_journal(job, result_journal)):
        _busy("The update transaction did not reach a verified terminal phase.")
    expected, identity, state, message = _journal_result(job, result_journal)
    try:
        _selected, ready_instance_id, ready_identity = _verify_journal_result_ready(
            root, data_root, job, result_journal, expected, identity, state)
    except (Exception, KeyboardInterrupt) as failure:  # noqa: BLE001 - an interrupted manager acknowledgement is unknown.
        raise _BusyRefusal("The recovered Runtime identity or readiness could not be confirmed.") from failure
    return _set_reconciled_job(root, job, state, message,
                               disposition="recovered-this-request",
                               ready_instance_id=ready_instance_id,
                               ready_identity=ready_identity)


def _reconcile_existing(root: Path, marker: dict, request: dict) -> tuple[dict | None, bool]:
    """Resolve an existing operation, returning whether this request continued it."""
    job = marker.get("update_job")
    if not isinstance(job, dict) or job.get("state") not in {
            "accepted", "launching", "active", "recovery-needed"}:
        return None, False
    try:
        api = _platform_api()
        data_root, _descriptor = _validate_reconcile_job(root, job, request, api)
        journal_path = root / "update-transaction.json"
        if journal_path.is_symlink():
            _busy("The unresolved update journal path cannot be trusted.")
        journal = read_json(journal_path) if journal_path.exists() else None
        if journal is not None and _matching_journal(job, journal):
            if job.get("platform") is None:
                _busy("The matching update transaction has no recorded native descriptor.")
            try:
                if api.is_active(_descriptor) is not False:
                    _busy("The unresolved update is still active.")
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as failure:
                raise _BusyRefusal("The unresolved update's native liveness is unknown.") from failure
            if journal.get("step") not in TERMINAL_STEPS:
                return _recover_matching_transaction(root, job, journal), False
            expected, identity, state, message = _journal_result(job, journal)
            _selected, ready_instance_id, ready_identity = _verify_journal_result_ready(
                root, data_root, job, journal, expected, identity, state)
            return _set_reconciled_job(root, job, state, message,
                                       disposition="confirmed-prior",
                                       ready_instance_id=ready_instance_id,
                                       ready_identity=ready_identity), False
        if not _journal_is_accepted_preexisting(root, job, journal):
            _busy("The unresolved update journal is new, missing, or unrelated to its accepted snapshot.")
        old = _validate_prebootstrap(root, job, data_root)
        expected = api.describe(job, _expected_worker_command(old, job))
        if job.get("platform") is not None and job["platform"] != expected:
            _busy("The unresolved update native descriptor does not match its accepted command.")
        expected_command = _expected_worker_command(old, job)
        try:
            registered = api.is_registered(expected, expected_command)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as failure:
            raise _BusyRefusal("The unresolved update registration ownership is unknown.") from failure
        if type(registered) is not bool:
            _busy("The unresolved update registration ownership is unknown.")
        if job.get("transaction_id") is not None:
            _busy("The unresolved update records a transaction without a matching journal.")
        if registered:
            try:
                if api.is_active(expected) is not False:
                    _busy("The unresolved update is still active.")
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as failure:
                raise _BusyRefusal("The unresolved update's native liveness is unknown.") from failure
        return _continue_prebootstrap(root, job, expected, old, registered, api), True
    except _BusyRefusal:
        raise
    except (Exception, KeyboardInterrupt) as failure:
        raise _BusyRefusal("The unresolved update evidence could not be safely reconciled.") from failure


def accept(request: dict) -> dict:
    """Durably accept one already-prepared request under the normal install lock."""
    _require_update_job_available()
    root = absolute(request.get("root", ""))
    stream = _locked(root)
    try:
        marker_path = root / "install.json"
        marker = read_json(marker_path)
        prior = marker.get("update_job")
        if prior is not None and (not isinstance(prior, dict)
                                  or prior.get("format") != JOB_FORMAT
                                  or not isinstance(prior.get("state"), str)
                                  or prior["state"] not in JOB_STATES):
            _busy("The prior update operation is unreadable or has an unknown state.")
        unresolved = (isinstance(prior, dict) and prior.get("state") in
                      {"accepted", "launching", "active", "recovery-needed"})
        pending_path = root / "update-transaction.json"
        if pending_path.is_symlink():
            if unresolved:
                _busy("The unresolved update journal path cannot be trusted.")
            raise ValueError("The installer journal path cannot be trusted.")
        try:
            pending = read_json(pending_path) if pending_path.exists() else None
        except FileNotFoundError:
            pending = None
        except (OSError, ValueError, TypeError) as failure:
            if unresolved:
                raise _BusyRefusal("The unresolved update journal cannot be read.") from failure
            raise
        recovered_unfinished = bool(unresolved and isinstance(pending, dict)
                                    and pending.get("step") not in TERMINAL_STEPS)
        reconciled, continued = _reconcile_existing(root, marker, request)
        if continued:
            if reconciled is None:
                _busy("The accepted update continuation could not be confirmed.")
            return reconciled
        if recovered_unfinished:
            # A request that performs rollback/completion is recovery-only. It
            # cannot admit another operation until a later explicit click.
            if reconciled is None:
                _busy("The unfinished update could not be reconciled.")
            return reconciled
        if pending and pending.get("step") not in TERMINAL_STEPS and not unresolved:
            _busy("The installer must recover its pending update first.")
        if reconciled is not None:
            current_version = current(root)
            metadata = request.get("metadata")
            target_release = metadata.get("release") if isinstance(metadata, dict) else None
            if (current_version is not None
                    and (type(target_release) is not int or target_release <= current_version.release)):
                return reconciled
            marker = read_json(marker_path)
            _archive_prior_job(marker, reconciled, pending)
        if isinstance(prior, dict) and request.get("operation_id") == prior.get("operation_id"):
            _busy("A new update requires a new operation identifier; the previous operation was preserved.")
        job = _validate_request(request, root)
        from .core import Version
        job["worker_command_sha256"] = _worker_binding_digest(Version.load(job["old_version"]), job)
        # Capture the journal observed under the installer lock. A later direct
        # transaction has a different operation ID; an unchanged journal predates
        # this acceptance even if the bootstrap never reaches staging.
        if pending is None or isinstance(pending, dict):
            job["prior_transaction_present"] = pending is not None
            job["prior_transaction_id"] = pending.get("operation_id") if isinstance(pending, dict) else None
            job["prior_transaction_hash_kind"] = "raw-sha256/v1"
            job["prior_transaction_sha256"] = (
                hashlib.sha256(pending_path.read_bytes()).hexdigest()
                if isinstance(pending, dict) else None)
        preferences = read_json(_selection_path()).get("preferences")
        autostart = preferences.get("autostart") if isinstance(preferences, dict) else None
        if autostart_off(autostart):
            raise ValueError("This update cannot run because saved autostart is off and the installer cannot preserve that choice yet.")
        job["autostart_snapshot"] = autostart
        log_path = Path(job["log_path"])
        platform.private_directory(log_path.parent)
        with log_path.open("a", encoding="utf-8") as log_stream:
            platform.owner_only(log_stream.fileno())
        residues = marker.get("update_job_residue", [])
        if not isinstance(residues, list):
            residues = []
        if isinstance(prior, dict) and prior.get("cleanup_status") not in {None, "clean", "not-needed"}:
            descriptor = prior.get("platform")
            if isinstance(descriptor, dict):
                residues = [*residues, {"operation_id": prior.get("operation_id"),
                                       "descriptor": descriptor,
                                       "cleanup_status": prior.get("cleanup_status"),
                                       "cleanup_error": prior.get("cleanup_error"),
                                       "residue": prior.get("residue")}]
        if residues:
            marker["update_job_residue"] = residues
        if isinstance(prior, dict) and prior.get("state") not in {
                "accepted", "launching", "active", "recovery-needed"}:
            _archive_prior_job(marker, prior, pending)
        marker["update_job"] = job
        write_json(marker_path, marker)
        return job
    finally:
        _unlock(stream)


def _read_job(root: Path, operation_id: str) -> dict:
    job = read_json(root / "install.json").get("update_job")
    if (not isinstance(job, dict) or job.get("format") != JOB_FORMAT
            or job.get("operation_id") != operation_id):
        raise ValueError("The accepted update operation is no longer current.")
    return job


def read_job(root: Path) -> dict:
    """Return the bounded K2 update metadata without running recovery."""
    root = absolute(root)
    job = read_json(root / "install.json").get("update_job")
    if not isinstance(job, dict) or job.get("format") != JOB_FORMAT:
        return {"state": "none"}
    return {key: job.get(key) for key in (
        "format", "operation_id", "state", "job_name", "log_path", "program_root",
        "data_root", "old_instance_id", "old_identity", "old_version", "target_release",
        "metadata", "transaction_id", "accepted_at", "finished_at", "message",
        "platform", "launch_status", "launch_error", "cleanup_status", "cleanup_error",
        "residue", "autostart_snapshot", "route_disposition",
    ) if key in job}


def _write_job(root: Path, operation_id: str, *, state: str, message: str,
               transaction_id: str | None = None, fields: dict | None = None,
               allow_reconcile: bool = False) -> dict:
    job = _read_job(root, operation_id)
    terminal = {"updated", "rolled_back", "failed", "recovery-needed"}
    if job.get("state") in terminal and state != job.get("state") and not allow_reconcile:
        return job
    if state not in JOB_STATES:
        raise ValueError("The update job result state is invalid.")
    job["state"] = state
    job["message"] = _one_line(message)
    if transaction_id:
        job["transaction_id"] = transaction_id
    if fields:
        job.update(fields)
    if state in {"updated", "rolled_back", "failed", "recovery-needed"}:
        job["finished_at"] = int(time.time())
    marker_path = root / "install.json"
    marker = read_json(marker_path)
    marker["update_job"] = job
    write_json(marker_path, marker)
    return job


def fail(root: Path, operation_id: str, reason: object) -> dict:
    root = absolute(root)
    stream = _locked(root)
    try:
        journal = pending_transaction(root)
        if journal:
            return _write_job(root, operation_id, state="recovery-needed",
                              message="Recovery needed. Run the install line to recover the interrupted update.",
                              transaction_id=journal.get("operation_id"))
        line = _one_line(reason)
        if not line.lower().startswith("failed:"):
            line = "Failed: " + line
        return _write_job(root, operation_id, state="failed", message=line)
    finally:
        _unlock(stream)


def _validate_worker_binding(root: Path, job: dict) -> tuple[Version, Path]:
    from .core import current
    marker = read_json(root / "install.json")
    selected = read_json(_selection_path())
    if (marker.get("owner") != LABEL or marker.get("managed_by") is not None
            or absolute(marker.get("data_root", "")) != absolute(job["data_root"])
            or selected.get("owner") != LABEL or selected.get("managed_by") is not None
            or selected.get("uninstalled")
            or absolute(selected.get("program_root", "")) != root
            or absolute(selected.get("data_root", "")) != absolute(job["data_root"])):
        raise ValueError("The selected installation ownership changed.")
    old = current(root)
    if old is None or old.document() != job.get("old_version"):
        raise ValueError("The selected installation has no current version.")
    if job.get("worker_command_sha256") != _worker_binding_digest(old, job):
        raise ValueError("The accepted worker command changed after acceptance.")
    selected_identity = selected.get("identity")
    if selected_identity is not None:
        expected = job.get("old_identity")
        if not isinstance(selected_identity, dict) or not isinstance(expected, dict) or any(
                selected_identity.get(key) != expected.get(key)
                for key in ("release", "managed_by")):
            raise ValueError("The selected startup identity changed.")
    observed_instance, observed_identity = _probe_identity(absolute(job["data_root"]))
    if (observed_instance != job.get("old_instance_id")
            or observed_identity != job.get("old_identity")):
        raise ValueError("The selected Runtime instance changed after acceptance.")
    return old, absolute(job["data_root"])


def _worker_command_source(old: Version, job: dict) -> str:
    epoch, nonce = _attempt_tuple(job)
    profile = read_json(old.profile)
    source_paths = []
    for key in ("runtime_checkout", "source_checkout"):
        value = profile.get(key)
        if isinstance(value, str) and value:
            path = absolute(value)
            if path.is_dir():
                source_paths.append(str(path))
    return (
        "import os,sys; "
        f"_log=open({job['log_path']!r},'a',encoding='utf-8',buffering=1); sys.stdout=sys.stderr=_log; "
        f"sys.path[:0]={source_paths!r}; "
        f"os.environ.update({old.env!r}); "
        "from astrid.installer.update_job import run_job; "
        f"result=run_job({job['program_root']!r}, {job['operation_id']!r}, {epoch!r}, {nonce!r}); "
        "print(__import__('json').dumps(result))"
    )


def _expected_worker_command(old: Version, job: dict) -> list[str]:
    """Derive the exact worker argv without creating the Windows handoff file."""
    script = _worker_command_source(old, job)
    if sys.platform == "win32":
        metadata = job.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("The accepted update job has no private worker directory.")
        directory, _, _ = _validate_input_paths(metadata)
        epoch, nonce = _attempt_tuple(job)
        return [str(old.python), "-B", "-X", "utf8", "-I",
                str(directory / f"worker-{job['operation_id']}-{epoch}-{nonce}.py")]
    return [str(old.python), "-B", "-X", "utf8", "-I", "-c", script]


def _worker_command_digest(command: list[str]) -> str:
    encoded = json.dumps(command, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _worker_binding_digest(old: Version, job: dict) -> str:
    # Windows argv names a private script. Bind its intended contents too,
    # including before that script exists in the descriptorless case.
    return _worker_command_digest([*_expected_worker_command(old, job), _worker_command_source(old, job)])


def _worker_command(old: Version, job: dict, *, inactive_reconciliation: bool = False) -> list[str]:
    """Run run_job with the selected old interpreter and its recorded sources."""
    script = _worker_command_source(old, job)
    if sys.platform == "win32":
        # PowerShell 5.1's legacy native conversion can corrupt the quotes in
        # Python -c source. Keep source and literal data in this private file;
        # PowerShell forwards only its ordinary path argument to Python.
        metadata = job.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("The accepted update job has no private worker directory.")
        directory, _, _ = _validate_input_paths(metadata)
        if (not directory.is_dir() or directory.is_symlink()
                or not platform.is_owner_only(directory, directory=True)):
            raise ValueError("The accepted update worker directory is not private.")
        epoch, nonce = _attempt_tuple(job)
        worker_script = directory / f"worker-{job['operation_id']}-{epoch}-{nonce}.py"
        expected_bytes = (script + "\n").encode("utf-8")
        if worker_script.is_symlink():
            raise ValueError("The private update worker script already exists as a link.")
        if worker_script.exists():
            if (not inactive_reconciliation or not worker_script.is_file()
                    or not platform.is_owner_only(worker_script)):
                raise ValueError("The private update worker script already exists.")
            if worker_script.read_bytes() != expected_bytes:
                raise ValueError("The private update worker script already exists with changed content.")
        if not worker_script.exists():
            try:
                with worker_script.open("xb") as stream:
                    platform.owner_only(stream.fileno())
                    stream.write(expected_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                with contextlib.suppress(OSError):
                    worker_script.unlink(missing_ok=True)
                raise
        if worker_script.is_symlink() or not platform.is_owner_only(worker_script):
            with contextlib.suppress(OSError):
                worker_script.unlink(missing_ok=True)
            raise ValueError("The private update worker script is not owner-only.")
        return [str(old.python), "-B", "-X", "utf8", "-I", str(worker_script)]
    return [str(old.python), "-B", "-X", "utf8", "-I", "-c", script]


def _platform_api():
    from . import update_job_platform
    return update_job_platform


def _owned_descriptor(descriptor: dict, operation_id: str) -> bool:
    """Check the operation-specific identity before querying or deleting it."""
    if (not isinstance(descriptor, dict)
            or descriptor.get("format") != "astrid-update-platform/v1"
            or descriptor.get("operation_id") != operation_id):
        return False
    system = descriptor.get("system")
    expected = (f"astrid-update-{operation_id}.service" if system == "linux"
                else f"astrid-update-{operation_id}" if system in {"darwin", "win32"} else None)
    if not expected or descriptor.get("name") != expected:
        return False
    if system == "darwin":
        path = Path(descriptor.get("plist_path", ""))
        root = Path(descriptor.get("program_root", ""))
        log = Path(descriptor.get("log_path", ""))
        return (path.is_absolute() and root.is_absolute() and log.is_absolute()
                and log.is_relative_to(root) and path == log.parent / f".{expected}.plist"
                and descriptor.get("label") == expected)
    if system == "win32":
        root = Path(descriptor.get("program_root", ""))
        log = Path(descriptor.get("log_path", ""))
        return (descriptor.get("task_name") == expected and root.is_absolute()
                and log.is_absolute() and log.is_relative_to(root))
    return descriptor.get("unit") == expected


def _submit_native_attempt(root: Path, operation_id: str, epoch: int, nonce: str,
                           api, descriptor: dict, command: list[str], log_path: Path) -> None:
    """Make the one manager call only if this exact attempt is still current.

    Keeping this fence adjacent to ``api.launch`` is intentional.  It covers a
    submitter delayed after its durable reservation as well as a normal caller;
    a replacement has changed the durable epoch/nonce, so the stale caller
    cannot reach any native API method.
    """
    job = _current_attempt(root, operation_id, epoch, nonce, submitted=True)
    if job.get("platform") != descriptor:
        _busy("The native update descriptor was replaced before submission.")
    api.launch(descriptor, command, log_path)


def _launch_current_attempt(root: Path, job: dict, old: Version, api, *,
                            continuation: bool = False) -> dict:
    """Reserve, fence, submit, and acknowledge one current native attempt.

    The caller holds the installer lock.  No write is made after the final
    fence unless the just-reread tuple still names the attempt being submitted.
    """
    operation_id = job["operation_id"]
    epoch, nonce = _attempt_tuple(job)
    _current_attempt(root, operation_id, epoch, nonce)
    try:
        command = _worker_command(old, job)
        if job.get("worker_command_sha256") != _worker_binding_digest(old, job):
            raise ValueError("The accepted worker command changed after acceptance.")
        descriptor = api.describe(job, command)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as failure:
        return _write_job(root, operation_id, state="failed",
                          message="Failed: the operating system update job could not be prepared.",
                          fields={"launch_status": "failed", "launch_error": _one_line(failure),
                                  "cleanup_status": "not-needed"},
                          allow_reconcile=continuation)

    attempt = dict(_attempt(job))
    attempt.update({
        "phase": "submitted",
        "submit": "spent",
        "wrapper_sha256": hashlib.sha256((_worker_command_source(old, job) + "\n").encode("utf-8")).hexdigest(),
        "descriptor_sha256": _attempt_digest(descriptor),
    })
    # Spend the launch authority before the one manager call.  The final
    # reread in _submit_native_attempt is the epoch fence for delayed callers.
    job = _write_job(root, operation_id, state="launching", message="Update job registration is pending.",
                     fields={"platform": descriptor, "launch_status": "launching",
                             "cleanup_status": "pending", "attempt": attempt},
                     allow_reconcile=continuation)
    try:
        _submit_native_attempt(root, operation_id, epoch, nonce, api, descriptor, command,
                               Path(job["log_path"]))
    except _BusyRefusal:
        raise
    except (Exception, KeyboardInterrupt) as failure:  # noqa: BLE001 - an interrupted manager acknowledgement is unknown.
        # A manager failure can race a worker claim only when the exact current
        # attempt launched.  Reread it before recording any outcome.
        current = _current_attempt(root, operation_id, epoch, nonce, submitted=True)
        current_attempt = dict(_attempt(current))
        current_attempt["submit"] = "unknown"
        rejected = (type(failure).__name__ == "UpdateJobLaunchError"
                    or isinstance(failure, subprocess.CalledProcessError))
        if current.get("state") != "launching":
            return current
        try:
            still_active = api.is_active(descriptor)
            query_known = True
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            still_active, query_known = True, False
        if rejected and query_known and still_active is False:
            return _write_job(root, operation_id, state="failed",
                              message="Failed: the operating system rejected update job launch.",
                              fields={"launch_status": "failed", "launch_error": _one_line(failure),
                                      "attempt": current_attempt}, allow_reconcile=continuation)
        failed = _write_job(root, operation_id, state="launching",
                            message="Update launch acknowledgement was lost; repair will inspect the owned job.",
                            fields={"launch_status": "unknown", "launch_error": _one_line(failure),
                                    "attempt": current_attempt,
                                    "route_disposition": _with_route_disposition(
                                        current, "acknowledgement-unknown", blocker="held")},
                            allow_reconcile=continuation)
        if continuation:
            _write_job(root, operation_id, state="recovery-needed",
                       message="Recovery needed. Update launch acknowledgement was lost; run Update again.",
                       fields={"launch_status": "unknown", "launch_error": _one_line(failure),
                               "attempt": current_attempt,
                               "route_disposition": _with_route_disposition(
                                   current, "acknowledgement-unknown", blocker="held")},
                       allow_reconcile=True)
            raise _AcknowledgementUnknown("The update continuation launch could not be acknowledged.") from failure
        return failed

    current = _current_attempt(root, operation_id, epoch, nonce, submitted=True)
    current_attempt = dict(_attempt(current))
    current_attempt["submit"] = "acknowledged"
    # Publish the grant together with the acknowledgement while the
    # coordinator still owns the installer lock. A wrapper delivered after an
    # unknown manager reply must never acquire bootstrap authority.
    current_attempt["handoff"] = {"epoch": epoch, "nonce": nonce}
    if current.get("state") == "launching":
        return _write_job(root, operation_id, state="launching", message="Update job launched.",
                          fields={"launch_status": "acknowledged", "launch_error": None,
                                  "attempt": current_attempt}, allow_reconcile=continuation)
    if current.get("state") == "active" and current.get("launch_status") == "launching":
        return _write_job(root, operation_id, state="active", message=_job_message(current),
                          fields={"launch_status": "acknowledged", "attempt": current_attempt},
                          allow_reconcile=continuation)
    return current


def launch_job(root: Path, operation_id: str) -> dict:
    """Register and start the accepted job while retaining the installer lock."""
    _require_update_job_available()
    root = absolute(root)
    stream = _locked(root)
    try:
        try:
            job = _read_job(root, operation_id)
        except ValueError as failure:
            raise _BusyRefusal("The accepted update operation is no longer current; no job was launched.") from failure
        if job.get("state") in {"launching", "active", "updated", "rolled_back", "failed", "recovery-needed"}:
            if job.get("state") in {"updated", "rolled_back", "failed", "recovery-needed"}:
                _busy("The selected update is terminal or unresolved; no new native job was launched.")
            return job
        if job.get("state") != "accepted":
            raise ValueError("The update job is not accepted for launch.")
        selection = read_json(_selection_path())
        preferences = selection.get("preferences")
        autostart = preferences.get("autostart") if isinstance(preferences, dict) else None
        if autostart_off(autostart) or autostart_off(job.get("autostart_snapshot")):
            return _write_job(root, operation_id, state="failed",
                              message="Failed: saved autostart is off and the installer cannot preserve that choice.",
                              fields={"launch_status": "refused", "cleanup_status": "not-needed"})
        from .core import Version
        return _launch_current_attempt(root, job, Version.load(job["old_version"]), _platform_api())
    finally:
        _unlock(stream)


def _cleanup_terminal(root: Path, operation_id: str, *, locked: bool = False,
                      finishing: bool = False) -> dict:
    """Attempt only the current recorded job's cleanup, preserving residue."""
    stream = None if locked else _locked(root)
    try:
        job = _read_job(root, operation_id)
        if job.get("state") not in {"updated", "rolled_back", "failed", "recovery-needed"}:
            return job
        descriptor = job.get("platform")
        if not isinstance(descriptor, dict):
            return _write_job(root, operation_id, state=job["state"], message=_job_message(job),
                              fields={"cleanup_status": "not-needed"})
        if not _owned_descriptor(descriptor, operation_id):
            return _write_job(root, operation_id, state=job["state"], message=_job_message(job),
                              fields={"cleanup_status": "unknown",
                                      "cleanup_error": "Recorded platform object did not match this operation."})
        # Persist intent before cleanup: launchd bootout may stop this worker.
        job = _write_job(root, operation_id, state=job["state"], message=_job_message(job),
                         fields={"cleanup_status": "attempting", "residue": descriptor.get("name")})
    finally:
        if stream is not None:
            _unlock(stream)
    try:
        api = _platform_api()
        if finishing:
            # launchd bootout can terminate this worker; remove only the exact
            # private selection first, after terminal state is durable.
            _cleanup_private_inputs(job, "clean")
        api.cleanup(descriptor, finishing=finishing)
        if finishing and descriptor.get("system") == "linux":
            # systemd --collect removes this unit after its own process exits;
            # querying during this call necessarily sees the worker as active.
            active = api.is_active(descriptor)
            status = "deferred" if active else "clean"
            error = None
        else:
            active = api.is_active(descriptor)
            status = "residue" if active else "clean"
            error = "The owned update job is still active." if active else None
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as failure:
        status, error = "unknown", _one_line(failure)
    stream = None if locked else _locked(root)
    try:
        job = _read_job(root, operation_id)
        fields = {"cleanup_status": status, "cleanup_error": error,
                  "residue": descriptor.get("name") if status != "clean" else None}
        job = _write_job(root, operation_id, state=job["state"], message=_job_message(job), fields=fields)
        if not finishing:
            _cleanup_private_inputs(job, status)
        return job
    finally:
        if stream is not None:
            _unlock(stream)


def _cleanup_private_inputs(job: dict, cleanup_status: str) -> None:
    if cleanup_status != "clean":
        return
    metadata = job.get("metadata")
    if not isinstance(metadata, dict):
        return
    directory, _, _ = _validate_input_paths(metadata)
    # Delete only the private input files in the operation's fixed directory.
    if (not directory.name.startswith("astrid-update-") or directory.is_symlink()
            or not platform.is_owner_only(directory, directory=True)):
        return
    for name, digest in (("manifest", metadata.get("manifest_sha256")),
                         ("bootstrap", metadata.get("bootstrap_sha256"))):
        candidate = directory / name
        try:
            raw = _selected_bytes(candidate, MAX_MANIFEST_BYTES if name == "manifest" else MAX_BOOTSTRAP_BYTES)
            if hashlib.sha256(raw).hexdigest() == digest:
                candidate.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
    handoff = directory / f"bootstrap-{job['operation_id']}.ps1"
    if handoff.is_file() and not handoff.is_symlink() and platform.is_owner_only(handoff):
        handoff.unlink(missing_ok=True)
    attempts = [job.get("attempt")]
    retired = job.get("retired_attempts")
    if isinstance(retired, list):
        attempts.extend(retired)
    for attempt in attempts:
        try:
            epoch, nonce = _attempt_tuple({"attempt": attempt})
        except ValueError:
            continue
        worker = directory / f"worker-{job['operation_id']}-{epoch}-{nonce}.py"
        if worker.is_file() and not worker.is_symlink() and platform.is_owner_only(worker):
            worker.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        if platform.is_owner_only(directory, directory=True):
            directory.rmdir()


def sweep_owned_jobs(root: Path, *, locked: bool = False) -> list[dict]:
    """Reconcile only recorded owned leftovers while the installer lock is held."""
    root = absolute(root)
    stream = None if locked else _locked(root)
    try:
        marker = read_json(root / "install.json")
        job = marker.get("update_job")
        results = []
        if isinstance(job, dict) and job.get("format") == JOB_FORMAT:
            descriptor = job.get("platform")
            state = job.get("state")
            if state in {"updated", "rolled_back", "failed", "recovery-needed"}:
                if isinstance(descriptor, dict):
                    if not _owned_descriptor(descriptor, job.get("operation_id", "")):
                        results.append(job)
                        descriptor = None
                if isinstance(descriptor, dict):
                    try:
                        active = _platform_api().is_active(descriptor)
                    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                        active = True
                    if active:
                        results.append(job)
                    else:
                        results.append(_cleanup_terminal(root, job["operation_id"], locked=True))
            elif isinstance(descriptor, dict):
                if not _owned_descriptor(descriptor, job.get("operation_id", "")):
                    results.append(job)
                    descriptor = None
            if isinstance(descriptor, dict) and state not in {"updated", "rolled_back", "failed", "recovery-needed"}:
                try:
                    active = _platform_api().is_active(descriptor)
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                    active = True
                if active:
                    results.append(job)
                elif state in {"launching", "active"}:
                    journal = read_json(root / "update-transaction.json")
                    binding = journal.get("update_job") if isinstance(journal, dict) else None
                    transaction_started = (isinstance(binding, dict)
                                           and binding.get("operation_id") == job["operation_id"])
                    pending = bool(journal and journal.get("step") not in TERMINAL_STEPS)
                    if transaction_started or pending:
                        job = _write_job(root, job["operation_id"], state="recovery-needed",
                                         message="Recovery needed. The update worker exited during the installer transaction; run the install line to recover.",
                                         transaction_id=journal.get("operation_id") if journal else None,
                                         fields={"launch_status": "unknown"})
                    else:
                        job = _write_job(root, job["operation_id"], state="failed",
                                         message="Failed: the registered update job became inactive without a result.",
                                         fields={"launch_status": "unknown"})
                    results.append(_cleanup_terminal(root, job["operation_id"], locked=True))
                else:
                    results.append(job)
            elif state == "accepted":
                job = _write_job(root, job["operation_id"], state="failed",
                                 message="Failed: accepted update was never launched; run Check for updates to try again.",
                                 fields={"launch_status": "not-started", "cleanup_status": "not-needed"})
                results.append(job)

        residues = marker.get("update_job_residue", [])
        if isinstance(residues, list) and residues:
            residue_changed = False
            for residue in residues:
                if not isinstance(residue, dict) or not isinstance(residue.get("descriptor"), dict):
                    results.append(residue)
                    continue
                if residue.get("cleanup_status") == "clean":
                    continue
                descriptor = residue["descriptor"]
                if not _owned_descriptor(descriptor, residue.get("operation_id", "")):
                    results.append(residue)
                    continue
                try:
                    api = _platform_api()
                    if api.is_active(descriptor):
                        results.append(residue)
                        continue
                    api.cleanup(descriptor)
                    residue["cleanup_status"] = "clean"
                    residue["cleanup_error"] = None
                    residue["residue"] = None
                    residue_changed = True
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as failure:
                    residue["cleanup_status"] = "unknown"
                    residue["cleanup_error"] = _one_line(failure)
                    residue_changed = True
                    results.append(residue)
            if residue_changed:
                latest_marker = read_json(root / "install.json")
                latest_marker["update_job_residue"] = residues
                write_json(root / "install.json", latest_marker)
        return results
    finally:
        if stream is not None:
            _unlock(stream)


def admit_worker(root: Path, operation_id: str, backend: Backend, installed: Version | None,
                 data: Path, manifest_path: Path | None) -> tuple[dict, Version]:
    """Validate the accepted job before the worker's installer run; point backend at its log."""
    job = _read_job(root, operation_id)
    selection_record = read_json(backend.service.selection)
    preferences = selection_record.get("preferences")
    if isinstance(preferences, dict) and autostart_off(preferences.get("autostart")):
        raise ValueError("This update cannot continue because saved autostart is off and the installer cannot preserve that choice yet.")
    if autostart_off(job.get("autostart_snapshot")):
        raise ValueError("This update cannot continue because saved autostart was off when it was accepted.")
    accepted_old, selected_data = _validate_worker_binding(root, job)
    metadata = job.get("metadata", {})
    if (job.get("state") != "active" or installed is None
            or installed.document() != job.get("old_version")
            or accepted_old.document() != installed.document()
            or data != selected_data or root != absolute(job.get("program_root", ""))
            or manifest_path != Path(metadata.get("manifest_path", ""))):
        raise ValueError("The accepted update job no longer matches this installation.")
    validate_update_inputs(_validate_input_paths(metadata)[0],
                           {"manifest_sha256": metadata["manifest_sha256"],
                            "bootstrap_sha256": metadata["bootstrap_sha256"],
                            "release": metadata["release"]}, installed.release)
    if tools.validate_manifest(read_json(manifest_path)).get("release", 0) != job.get("target_release"):
        raise ValueError("The selected update manifest release changed.")
    if Path(job["log_path"]).is_symlink() or not Path(job["log_path"]).is_relative_to(root):
        raise ValueError("The accepted update log path is invalid.")
    backend.log = Path(job["log_path"])
    return job, accepted_old


def _safe_failure_line(failure: BaseException) -> str:
    value = " ".join(str(failure).replace("\x00", " ").splitlines()).strip()
    return (value or "Update failed.")[:180]


def record_success(root: Path, data: Path, backend: Backend, job: dict) -> None:
    saved_job = _read_job(root, job["operation_id"])
    journal = read_json(root / "update-transaction.json")
    binding = journal.get("update_job") if isinstance(journal, dict) else None
    if (journal.get("step") != "complete" or not isinstance(binding, dict)
            or binding.get("operation_id") != job["operation_id"]
            or saved_job.get("transaction_id") != journal.get("operation_id")):
        _write_job(root, job["operation_id"], state="recovery-needed",
                   message="Recovery needed. The completed C2 transaction was not bound to this update job.")
        raise ValueError("The completed installer transaction was not bound to this update job.")
    installed = current(root)
    if installed is None or installed.release != job.get("target_release"):
        _write_job(root, job["operation_id"], state="recovery-needed",
                   message="Recovery needed. The target release is not selected.")
        raise ValueError("The selected target release did not become current.")
    realm = backend.workspace(installed, data)["realm_id"]
    backend.verify(installed, data, realm)
    discovery = read_json(data / "runtime" / "discovery.json")
    instance_id = discovery.get("runtime_instance_id")
    if not isinstance(instance_id, str) or not instance_id or instance_id == job.get("old_instance_id"):
        _write_job(root, job["operation_id"], state="recovery-needed",
                   message="Recovery needed. The updated Runtime instance was not confirmed.")
        raise ValueError("The updated Runtime instance was not confirmed.")
    _, identity = _probe_startup_identity(data)
    if identity != {"release": job.get("target_release"), "managed_by": None}:
        _write_job(root, job["operation_id"], state="recovery-needed",
                   message="Recovery needed. The updated Runtime identity did not match the target.")
        raise ValueError("The updated Runtime identity did not match the target release.")
    _write_job(root, job["operation_id"], state="updated", message=f"Updated to release {installed.release}.")


def record_failure(root: Path, data: Path, backend: Backend, job: dict,
                   accepted_old: Version, failure: BaseException) -> None:
    journal = read_json(root / "update-transaction.json")
    saved_job = _read_job(root, job["operation_id"])
    binding = journal.get("update_job") if isinstance(journal, dict) else None
    linked = (isinstance(binding, dict) and binding.get("operation_id") == job["operation_id"]
              and saved_job.get("transaction_id") == journal.get("operation_id"))
    step = journal.get("step") if linked else None
    if journal and journal.get("step") not in TERMINAL_STEPS:
        _write_job(root, job["operation_id"], state="recovery-needed",
                   message="Recovery needed. Run the install line to recover the interrupted update.",
                   transaction_id=journal.get("operation_id") if linked else None)
        return
    selected = current(root)
    if selected is None:
        _write_job(root, job["operation_id"], state="recovery-needed",
                   message="Recovery needed. No current Astrid version is selected.")
        return
    try:
        if step == "rolled_back" and linked:
            if selected.document() != accepted_old.document():
                raise ValueError("The old version was not restored.")
            realm = backend.workspace(selected, data)["realm_id"]
            backend.verify(selected, data, realm)
            restored_id = read_json(data / "runtime" / "discovery.json").get("runtime_instance_id")
            if not isinstance(restored_id, str) or not restored_id or restored_id == job.get("old_instance_id"):
                raise ValueError("The restored Runtime instance was not confirmed.")
            _, identity = _probe_startup_identity(data)
            if identity != job.get("old_identity"):
                raise ValueError("The restored Runtime identity did not match the prior release.")
            message = f"Rolled back: {_safe_failure_line(failure)}"
            _write_job(root, job["operation_id"], state="rolled_back", message=message,
                       transaction_id=journal.get("operation_id"))
            return
        # A pre-stop or staged failure may leave C2 complete/absent. Report
        # failed only when the old selected owner still passes canonical ready.
        if selected.document() != accepted_old.document():
            raise ValueError("The accepted old version is no longer selected.")
        realm = backend.workspace(selected, data)["realm_id"]
        backend.verify(selected, data, realm)
        _, identity = _probe_startup_identity(data)
        if identity != job.get("old_identity"):
            raise ValueError("The previous Runtime identity is not ready.")
        _write_job(root, job["operation_id"], state="failed",
                   message=f"Failed: {_safe_failure_line(failure)}")
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - readiness failures are recorded as recovery-needed.
        _write_job(root, job["operation_id"], state="recovery-needed",
                   message="Recovery needed. Astrid could not confirm the previous Runtime is ready.",
                   transaction_id=journal.get("operation_id") if journal else None)


def run_job(root: Path, operation_id: str, epoch: int | None = None,
            nonce: str | None = None) -> dict:
    """Run the accepted selected public bootstrap in an independent worker.

    Native wrappers always supply their embedded epoch/nonce.  The optional
    form remains for in-process diagnostics and legacy test callers, but it
    still snapshots the current tuple while holding the installer lock.
    """
    root = absolute(root)
    windows_bootstrap = None
    claimed = False
    # A competing installer may have started after acceptance. Wait for its
    # existing lock, then recover and recheck the selected owner and release.
    stream = _locked(root, blocking=True)
    try:
        job = _read_job(root, operation_id)
        direct_call = epoch is None and nonce is None
        if direct_call:
            epoch, nonce = _attempt_tuple(job)
        elif type(epoch) is not int or not isinstance(nonce, str):
            _busy("The native update wrapper has an incomplete attempt identity.")
        # This must precede *every* state branch: an obsolete wrapper must not
        # even return/rewrite the replacement job, much less claim or enter it.
        job = _current_attempt(root, operation_id, epoch, nonce,
                               submitted=not direct_call)
        if not direct_call:
            attempt = _attempt(job)
            if (attempt.get("submit") != "acknowledged"
                    or attempt.get("handoff") != {"epoch": epoch, "nonce": nonce}):
                _busy("The native update attempt has no acknowledged handoff.")
        if job.get("state") in {"updated", "rolled_back", "failed", "recovery-needed", "active"}:
            return job
        if job.get("state") not in {"accepted", "launching"}:
            raise ValueError("The update job is not accepted for execution.")
        # This process owns the one-shot launch now. Persist the claim before
        # validation so a second invocation cannot run and terminal errors can
        # safely clean up from inside the OS job's own kill domain.
        proof = job.get("bootstrap_proof")
        if not isinstance(proof, dict) or not _bootstrap_proof_valid(job):
            raise ValueError("The accepted update has no valid bootstrap-entry proof.")
        attempt = dict(_attempt(job))
        if not direct_call and (attempt.get("phase") != "submitted" or attempt.get("claim") is not None):
            _busy("The native update wrapper is not eligible to claim this attempt.")
        proof = dict(proof)
        proof["claim_state"] = "claimed"
        proof["claimed_at"] = int(time.time())
        descriptor = job.get("platform")
        if not isinstance(descriptor, dict) or not _owned_descriptor(descriptor, operation_id):
            raise ValueError("The accepted update native claim is not bound to this operation.")
        proof["native_claim"] = {"operation_id": operation_id,
                                 "program_root": str(root), "data_root": job["data_root"],
                                 "owner": LABEL, "descriptor": descriptor,
                                 "epoch": epoch, "nonce": nonce}
        attempt.update({"phase": "claimed", "claim": {
            "operation_id": operation_id, "epoch": epoch, "nonce": nonce,
        }})
        _current_attempt(root, operation_id, epoch, nonce, submitted=not direct_call)
        _write_job(root, operation_id, state="active", message="Update worker is validating the accepted operation.",
                   fields={"bootstrap_proof": proof, "attempt": attempt})
        claimed = True
        if pending_transaction(root):
            from .backend import Backend
            backend = Backend(root, root / "logs" / "install.log", root / "bootstrap" / "uv")
            marker = read_json(root / "install.json")
            from .app_update import locked as app_locked
            from .core import Installer
            with app_locked(root / ".app-update.lock"):
                Installer(root, absolute(job["data_root"]), backend,
                          managed_by=marker.get("managed_by"))._recover_update()
        old, data_root = _validate_worker_binding(root, job)
        if old.release != job.get("old_version", {}).get("identity", {}).get("release"):
            raise ValueError("The installed release changed after this update was accepted.")
        metadata = job["metadata"]
        _validate_input_paths(metadata)
        inputs = validate_update_inputs(
            Path(metadata["manifest_path"]).parent,
            {"manifest_sha256": metadata["manifest_sha256"],
             "bootstrap_sha256": metadata["bootstrap_sha256"], "release": metadata["release"]},
            old.release,
        )
        if inputs["release"] != job["target_release"] or inputs["release"] <= old.release:
            raise ValueError("The selected update release is no longer newer.")
        _current_attempt(root, operation_id, epoch, nonce, submitted=not direct_call)
        _write_job(root, operation_id, state="active", message="Update is running.")
        # This durable transition is the boundary after which a fresh bootstrap
        # must never be launched, even if the next process call loses its ack.
        proof = dict(_read_job(root, operation_id)["bootstrap_proof"])
        proof["state"] = "entered"
        proof["entered_at"] = int(time.time())
        _current_attempt(root, operation_id, epoch, nonce, submitted=not direct_call)
        _write_job(root, operation_id, state="active", message="Update is running.",
                   fields={"bootstrap_proof": proof})
        # The worker must release the installer lock before bootstrap launches
        # fresh `astrid install`, which reacquires it and owns all C2 writes.
        _unlock(stream)
        stream = None
        bootstrap = Path(metadata["bootstrap_path"])
        args = ["--manifest", metadata["manifest_path"], "--root", str(root),
                "--data-root", str(data_root), "--update-job", operation_id]
        if sys.platform == "win32":
            bootstrap = _windows_bootstrap_script(metadata, operation_id)
            windows_bootstrap = bootstrap
        command = _bootstrap_command(bootstrap, args)
        log = Path(job["log_path"])
        log.parent.mkdir(parents=True, exist_ok=True)
        platform.private_directory(log.parent)
        if log.is_symlink() or (log.exists() and not platform.is_owner_only(log)):
            raise ValueError("The update log is not a private regular file.")
        with log.open("a", encoding="utf-8", buffering=1) as output:
            platform.owner_only(output.fileno())
            try:
                if sys.platform == "win32":
                    raw = _selected_bytes(bootstrap, MAX_BOOTSTRAP_BYTES)
                    if hashlib.sha256(raw).hexdigest() != metadata["bootstrap_sha256"]:
                        raise ValueError("The selected PowerShell bootstrap changed before -File consumption.")
                result = subprocess.run(command, cwd=str(root), stdin=subprocess.DEVNULL,
                                        stdout=output, stderr=subprocess.STDOUT, check=False,
                                        env={key: value for key, value in os.environ.items()
                                             if key not in {"ASTRID_INSTALL_MANIFEST_URL", "PYTHONPATH", "PYTHONHOME"}})
            finally:
                if sys.platform == "win32":
                    bootstrap.unlink(missing_ok=True)
        # Fresh CLI records the authoritative result. A zero bootstrap exit
        # without that record is not success.
        completed = read_job(root)
        if completed.get("operation_id") != operation_id or completed.get("state") not in {
                "updated", "rolled_back", "failed", "recovery-needed"}:
            fail(root, operation_id, f"Bootstrap exited with status {result.returncode} without a verified result.")
            return _cleanup_terminal(root, operation_id, finishing=claimed)
        return _cleanup_terminal(root, operation_id, finishing=claimed)
    except _BusyRefusal:
        raise
    except (Exception, KeyboardInterrupt) as failure:
        if stream is not None:
            failed = None
            with contextlib.suppress(Exception):
                journal = read_json(root / "update-transaction.json")
                state = ("recovery-needed" if journal and journal.get("step") not in TERMINAL_STEPS
                         else "failed")
                message = ("Recovery needed. Run the install line to recover the interrupted update."
                           if state == "recovery-needed" else f"Failed: {_one_line(failure)}")
                failed = _write_job(root, operation_id, state=state, message=message,
                                    transaction_id=journal.get("operation_id") if journal else None)
            _unlock(stream)
            stream = None
            if failed is not None and failed.get("state") in {"failed", "recovery-needed"}:
                return _cleanup_terminal(root, operation_id, finishing=claimed)
            raise
        else:
            with contextlib.suppress(Exception):
                failed = fail(root, operation_id, failure)
                return _cleanup_terminal(root, operation_id, finishing=claimed) if failed.get("state") in {"failed", "rolled_back", "recovery-needed"} else failed
        raise
    finally:
        if windows_bootstrap is not None:
            with contextlib.suppress(OSError):
                windows_bootstrap.unlink(missing_ok=True)
        if stream is not None:
            _unlock(stream)


def main():
    # Internal selected-Python helper, not a browser-controlled command.
    raw = sys.stdin.read(65537)
    if len(raw) > 65536:
        raise ValueError("The update helper request is too large.")
    value = json.loads(raw)
    action = value.get("action", "prepare")
    if action == "availability":
        print(json.dumps(update_job_availability()))
    elif action == "prepare":
        metadata = fetch_update_inputs(Path(value["directory"]), manifest_url=value["manifest_url"],
                                       bootstrap_url=value["bootstrap_url"],
                                       installed_release=value["installed_release"])
        print(json.dumps(metadata))
    elif action == "accept":
        try:
            result = accept(value)
        except (_UnsupportedServiceManager, _BusyRefusal) as failure:
            response = {"state": "recovery-needed", "reason": failure.reason,
                        "message": _one_line(failure)}
            if isinstance(failure, _BusyRefusal):
                response.update({"disposition": ("acknowledgement-unknown"
                                                 if failure.reason == "acknowledgement-unknown"
                                                 else "refused"),
                                 "blocker": "held", "operation_id": _held_operation_id(value.get("root"))})
                if failure.reason == "acknowledgement-unknown":
                    response["next_action"] = "observe"
            print(json.dumps(response))
            raise SystemExit(1)
        print(json.dumps(_helper_result(result)))
    elif action == "fail":
        print(json.dumps(fail(Path(value["root"]), value["operation_id"], value.get("reason"))))
    elif action == "launch":
        try:
            result = launch_job(Path(value["root"]), value["operation_id"])
        except (_UnsupportedServiceManager, _BusyRefusal) as failure:
            response = {"state": "recovery-needed", "reason": failure.reason,
                        "message": _one_line(failure)}
            if isinstance(failure, _BusyRefusal):
                response.update({"disposition": ("acknowledgement-unknown"
                                                 if failure.reason == "acknowledgement-unknown"
                                                 else "refused"),
                                 "blocker": "held", "operation_id": _held_operation_id(value.get("root"))})
                if failure.reason == "acknowledgement-unknown":
                    response["next_action"] = "observe"
            print(json.dumps(response))
            raise SystemExit(1)
        print(json.dumps(_helper_result(result)))
    else:
        raise ValueError("Unknown update helper action.")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        result = run_job(Path(sys.argv[2]), sys.argv[3])
        print(json.dumps(result))
    else:
        main()
