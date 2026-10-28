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
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

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

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_BOOTSTRAP_BYTES = 2 * 1024 * 1024
JOB_FORMAT = "astrid-update-job/v1"
JOB_STATES = {"accepted", "launching", "active", "updated", "rolled_back", "failed", "recovery-needed"}
MAX_RESULT_LINE = 240


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
            if type(installed_release) is not int or installed_release < 0 or metadata["release"] <= installed_release:
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
        raise ValueError("The Astrid installer is already running.") from None
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
    if any(not isinstance(normalized[name], str) or not re.fullmatch(r"[0-9a-f]{64}", normalized[name])
           for name in ("manifest_sha256", "bootstrap_sha256")):
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
        "accepted_at": int(time.time()), "message": "Update accepted.",
    }


def accept(request: dict) -> dict:
    """Durably accept one already-prepared request under the normal install lock."""
    root = absolute(request.get("root", ""))
    stream = _locked(root)
    try:
        marker_path = root / "install.json"
        marker = read_json(marker_path)
        prior = marker.get("update_job")
        pending = read_json(root / "update-transaction.json")
        if pending and pending.get("step") not in TERMINAL_STEPS:
            raise ValueError("The installer must recover its pending update first.")
        if isinstance(prior, dict) and prior.get("state") in {"accepted", "launching", "active", "recovery-needed"}:
            raise ValueError("An update operation is already active.")
        job = _validate_request(request, root)
        # Capture the journal observed under the installer lock. A later direct
        # transaction has a different operation ID; an unchanged journal predates
        # this acceptance even if the bootstrap never reaches staging.
        if not pending or isinstance(pending.get("operation_id"), str):
            job["prior_transaction_id"] = pending.get("operation_id")
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
        "residue", "autostart_snapshot",
    ) if key in job}


def _write_job(root: Path, operation_id: str, *, state: str, message: str,
               transaction_id: str | None = None, fields: dict | None = None) -> dict:
    job = _read_job(root, operation_id)
    terminal = {"updated", "rolled_back", "failed", "recovery-needed"}
    if job.get("state") in terminal and state != job.get("state"):
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
    selected_identity = selected.get("identity")
    if selected_identity is not None:
        expected = job.get("old_identity")
        if not isinstance(selected_identity, dict) or any(
                selected_identity.get(key) != expected.get(key)
                for key in ("release", "managed_by")):
            raise ValueError("The selected startup identity changed.")
    observed_instance, observed_identity = _probe_identity(absolute(job["data_root"]))
    if (observed_instance != job.get("old_instance_id")
            or observed_identity != job.get("old_identity")):
        raise ValueError("The selected Runtime instance changed after acceptance.")
    return old, absolute(job["data_root"])


def _worker_command(old: Version, job: dict) -> list[str]:
    """Run run_job with the selected old interpreter and its recorded sources."""
    profile = read_json(old.profile)
    source_paths = []
    for key in ("runtime_checkout", "source_checkout"):
        value = profile.get(key)
        if isinstance(value, str) and value:
            path = absolute(value)
            if path.is_dir():
                source_paths.append(str(path))
    script = (
        "import os,sys; "
        f"_log=open({job['log_path']!r},'a',encoding='utf-8',buffering=1); sys.stdout=sys.stderr=_log; "
        f"sys.path[:0]={source_paths!r}; "
        f"os.environ.update({old.env!r}); "
        "from astrid.installer.update_job import run_job; "
        f"result=run_job({job['program_root']!r}, {job['operation_id']!r}); "
        "print(__import__('json').dumps(result))"
    )
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
        worker_script = directory / f"worker-{job['operation_id']}.py"
        if worker_script.exists() or worker_script.is_symlink():
            raise ValueError("The private update worker script already exists.")
        try:
            with worker_script.open("xb") as stream:
                platform.owner_only(stream.fileno())
                stream.write((script + "\n").encode("utf-8"))
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


def launch_job(root: Path, operation_id: str) -> dict:
    """Register and start the already accepted job exactly once."""
    root = absolute(root)
    stream = _locked(root)
    try:
        job = _read_job(root, operation_id)
        if job.get("state") in {"launching", "active", "updated", "rolled_back", "failed", "recovery-needed"}:
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
        old = Version.load(job["old_version"])
        try:
            command = _worker_command(old, job)
            api = _platform_api()
            descriptor = api.describe(job, command)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as failure:
            return _write_job(root, operation_id, state="failed",
                              message="Failed: the operating system update job could not be prepared.",
                              fields={"launch_status": "failed", "launch_error": _one_line(failure),
                                      "cleanup_status": "not-needed"})
        # Ownership must be durable before system registration or launch. A
        # crash after registration can then be recognized by a later sweep.
        job = _write_job(root, operation_id, state="launching", message="Update job registration is pending.",
                         fields={"platform": descriptor, "launch_status": "launching",
                                 "cleanup_status": "pending"})
    finally:
        _unlock(stream)
    try:
        api.launch(descriptor, command, Path(job["log_path"]))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as failure:
        stream = _locked(root)
        try:
            rejected = (type(failure).__name__ == "UpdateJobLaunchError"
                        or isinstance(failure, subprocess.CalledProcessError))
            current = _read_job(root, operation_id)
            if current.get("state") != "launching":
                failed = current
            else:
                try:
                    still_active = api.is_active(descriptor)
                    query_known = True
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                    still_active, query_known = True, False
            if current.get("state") == "launching" and rejected and query_known and not still_active:
                failed = _write_job(root, operation_id, state="failed",
                                    message="Failed: the operating system rejected update job launch.",
                                    fields={"launch_status": "failed", "launch_error": _one_line(failure)})
            elif current.get("state") == "launching":
                failed = _write_job(root, operation_id, state="launching",
                                    message="Update launch acknowledgement was lost; repair will inspect the owned job.",
                                    fields={"launch_status": "unknown", "launch_error": _one_line(failure)})
        finally:
            _unlock(stream)
        if failed.get("state") == "failed":
            return _cleanup_terminal(root, operation_id)
        return failed
    stream = _locked(root)
    try:
        job = _read_job(root, operation_id)
        if job.get("state") == "launching":
            # Keep state launching until the worker claims it. If launch starts
            # immediately, the worker must not mistake our ack for a claim.
            return _write_job(root, operation_id, state="launching", message="Update job launched.",
                              fields={"launch_status": "acknowledged"})
        if job.get("state") == "active" and job.get("launch_status") == "launching":
            return _write_job(root, operation_id, state="active", message=job.get("message"),
                              fields={"launch_status": "acknowledged"})
        return job
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
            return _write_job(root, operation_id, state=job["state"], message=job.get("message"),
                              fields={"cleanup_status": "not-needed"})
        if not _owned_descriptor(descriptor, operation_id):
            return _write_job(root, operation_id, state=job["state"], message=job.get("message"),
                              fields={"cleanup_status": "unknown",
                                      "cleanup_error": "Recorded platform object did not match this operation."})
        # Persist intent before cleanup: launchd bootout may stop this worker.
        job = _write_job(root, operation_id, state=job["state"], message=job.get("message"),
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
        job = _write_job(root, operation_id, state=job["state"], message=job.get("message"), fields=fields)
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
    worker = directory / f"worker-{job['operation_id']}.py"
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


def run_job(root: Path, operation_id: str) -> dict:
    """Run the accepted selected public bootstrap in an independent worker."""
    root = absolute(root)
    windows_bootstrap = None
    claimed = False
    # A competing installer may have started after acceptance. Wait for its
    # existing lock, then recover and recheck the selected owner and release.
    stream = _locked(root, blocking=True)
    try:
        job = _read_job(root, operation_id)
        if job.get("state") in {"updated", "rolled_back", "failed", "recovery-needed", "active"}:
            return job
        if job.get("state") not in {"accepted", "launching"}:
            raise ValueError("The update job is not accepted for execution.")
        # This process owns the one-shot launch now. Persist the claim before
        # validation so a second invocation cannot run and terminal errors can
        # safely clean up from inside the OS job's own kill domain.
        _write_job(root, operation_id, state="active", message="Update worker is validating the accepted operation.")
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
        _write_job(root, operation_id, state="active", message="Update is running.")
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
    if action == "prepare":
        metadata = fetch_update_inputs(Path(value["directory"]), manifest_url=value["manifest_url"],
                                       bootstrap_url=value["bootstrap_url"],
                                       installed_release=value["installed_release"])
        print(json.dumps(metadata))
    elif action == "accept":
        print(json.dumps(accept(value)))
    elif action == "fail":
        print(json.dumps(fail(Path(value["root"]), value["operation_id"], value.get("reason"))))
    elif action == "launch":
        print(json.dumps(launch_job(Path(value["root"]), value["operation_id"])))
    else:
        raise ValueError("Unknown update helper action.")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        result = run_job(Path(sys.argv[2]), sys.argv[3])
        print(json.dumps(result))
    else:
        main()
