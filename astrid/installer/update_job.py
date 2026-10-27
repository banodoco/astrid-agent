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

if TYPE_CHECKING:
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
JOB_STATES = {"accepted", "active", "updated", "rolled_back", "failed", "recovery-needed"}
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


def _selection_path() -> Path:
    from .service import install_selection
    return install_selection(Path.home(), sys.platform)


def _probe_identity(data_root: Path) -> tuple[str, dict]:
    discovery = read_json(data_root / "runtime" / "discovery.json")
    instance_id = discovery.get("runtime_instance_id")
    endpoint = discovery.get("endpoint") or discovery.get("door_endpoint")
    if not isinstance(instance_id, str) or not instance_id or not isinstance(endpoint, str) or not endpoint:
        raise ValueError("The live Runtime owner identity is unavailable.")
    from astrid_runtime.runtime_boundary import LocalRuntimeBoundary
    health = LocalRuntimeBoundary().owner_startup_identity(endpoint=endpoint, instance_id=instance_id)
    identity = __import__("astrid.installer.backend", fromlist=["Backend"]).Backend._validated_startup_identity(
        health, instance_id)
    return instance_id, identity


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
        if pending and pending.get("step") not in {"complete", "rolled_back"}:
            raise ValueError("The installer must recover its pending update first.")
        if isinstance(prior, dict) and prior.get("state") in {"accepted", "active", "recovery-needed"}:
            raise ValueError("An update operation is already active.")
        job = _validate_request(request, root)
        log_path = Path(job["log_path"])
        platform.private_directory(log_path.parent)
        with log_path.open("a", encoding="utf-8") as log_stream:
            platform.owner_only(log_stream.fileno())
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
    ) if key in job}


def _write_job(root: Path, operation_id: str, *, state: str, message: str,
               transaction_id: str | None = None) -> dict:
    job = _read_job(root, operation_id)
    if job.get("state") in {"updated", "rolled_back", "failed", "recovery-needed"}:
        return job
    if state not in JOB_STATES:
        raise ValueError("The update job result state is invalid.")
    job["state"] = state
    job["message"] = _one_line(message)
    if transaction_id:
        job["transaction_id"] = transaction_id
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
        journal = read_json(root / "update-transaction.json")
        if journal and journal.get("step") not in {"complete", "rolled_back"}:
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


def run_job(root: Path, operation_id: str) -> dict:
    """Run the accepted selected public bootstrap in an independent worker."""
    root = absolute(root)
    windows_bootstrap = None
    # A competing installer may have started after acceptance. Wait for its
    # existing lock, then recover and recheck the selected owner and release.
    stream = _locked(root, blocking=True)
    try:
        job = _read_job(root, operation_id)
        if job.get("state") in {"updated", "rolled_back", "failed", "recovery-needed", "active"}:
            return job
        if job.get("state") != "accepted":
            raise ValueError("The update job is not accepted for execution.")
        journal = read_json(root / "update-transaction.json")
        if journal and journal.get("step") not in {"complete", "rolled_back"}:
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
            return fail(root, operation_id, f"Bootstrap exited with status {result.returncode} without a verified result.")
        return completed
    except (Exception, KeyboardInterrupt) as failure:
        if stream is not None:
            with contextlib.suppress(Exception):
                journal = read_json(root / "update-transaction.json")
                state = ("recovery-needed" if journal and journal.get("step") not in {"complete", "rolled_back"}
                         else "failed")
                message = ("Recovery needed. Run the install line to recover the interrupted update."
                           if state == "recovery-needed" else f"Failed: {_one_line(failure)}")
                return _write_job(root, operation_id, state=state, message=message,
                                  transaction_id=journal.get("operation_id") if journal else None)
        else:
            with contextlib.suppress(Exception):
                return fail(root, operation_id, failure)
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
    else:
        raise ValueError("Unknown update helper action.")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        result = run_job(Path(sys.argv[2]), sys.argv[3])
        print(json.dumps(result))
    else:
        main()
