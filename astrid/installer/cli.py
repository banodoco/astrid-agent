"""`astrid install`: the one install, repair, update and uninstall entry point."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import traceback
from pathlib import Path

from .backend import Backend
from .core import (
    BackupSpaceError,
    Installer,
    RuntimeOperationError,
    VerificationError,
    absolute,
    current,
    read_json,
    write_json,
)
from .service import LABEL, bootstrap_root, default_program_root, installer_lock_path
from .support import FAILURE_MESSAGE, default_doctor_bundle
from .tools import download, validate_manifest

PINOKIO_UPDATE_UNINSTALL_REFUSAL = (
    "Astrid on this computer was installed with Pinokio. Choose Pinokio's Update or Uninstall button."
)
PINOKIO_STOP_REFUSAL = "Astrid is still running. Choose Pinokio's Stop button, then try again."


def _pinokio_claim_is_live(recorded: dict) -> bool:
    if recorded.get("managed_by") != "pinokio":
        return False
    program_root = recorded.get("program_root")
    if not isinstance(program_root, str) or not program_root:
        return False
    marker = read_json(absolute(program_root) / "install.json")
    return marker.get("owner") == LABEL and marker.get("managed_by") == "pinokio"


def _record_update_success(root: Path, data: Path, backend: Backend, job: dict) -> None:
    from . import update_job

    saved_job = update_job._read_job(root, job["operation_id"])
    journal = read_json(root / "update-transaction.json")
    binding = journal.get("update_job") if isinstance(journal, dict) else None
    if (journal.get("step") != "complete" or not isinstance(binding, dict)
            or binding.get("operation_id") != job["operation_id"]
            or saved_job.get("transaction_id") != journal.get("operation_id")):
        update_job._write_job(root, job["operation_id"], state="recovery-needed",
                              message="Recovery needed. The completed C2 transaction was not bound to this update job.")
        raise ValueError("The completed installer transaction was not bound to this update job.")
    installed = current(root)
    if installed is None or installed.release != job.get("target_release"):
        update_job._write_job(root, job["operation_id"], state="recovery-needed",
                              message="Recovery needed. The target release is not selected.")
        raise ValueError("The selected target release did not become current.")
    realm = backend.workspace(installed, data)["realm_id"]
    backend.verify(installed, data, realm)
    discovery = read_json(data / "runtime" / "discovery.json")
    instance_id = discovery.get("runtime_instance_id")
    if not isinstance(instance_id, str) or not instance_id or instance_id == job.get("old_instance_id"):
        update_job._write_job(root, job["operation_id"], state="recovery-needed",
                              message="Recovery needed. The updated Runtime instance was not confirmed.")
        raise ValueError("The updated Runtime instance was not confirmed.")
    identity = _canonical_identity(discovery, instance_id)
    if identity != {"release": job.get("target_release"), "managed_by": None}:
        update_job._write_job(root, job["operation_id"], state="recovery-needed",
                              message="Recovery needed. The updated Runtime identity did not match the target.")
        raise ValueError("The updated Runtime identity did not match the target release.")
    update_job._write_job(root, job["operation_id"], state="updated",
                          message=f"Updated to release {installed.release}.")


def _record_update_failure(root: Path, data: Path, backend: Backend, job: dict,
                           accepted_old, failure: BaseException) -> None:
    from . import update_job

    journal = read_json(root / "update-transaction.json")
    saved_job = update_job._read_job(root, job["operation_id"])
    binding = journal.get("update_job") if isinstance(journal, dict) else None
    linked = (isinstance(binding, dict) and binding.get("operation_id") == job["operation_id"]
              and saved_job.get("transaction_id") == journal.get("operation_id"))
    step = journal.get("step") if linked else None
    if journal and journal.get("step") not in {"complete", "rolled_back"}:
        update_job._write_job(root, job["operation_id"], state="recovery-needed",
                              message="Recovery needed. Run the install line to recover the interrupted update.",
                              transaction_id=journal.get("operation_id") if linked else None)
        return
    selected = current(root)
    if selected is None:
        update_job._write_job(root, job["operation_id"], state="recovery-needed",
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
            discovery = read_json(data / "runtime" / "discovery.json")
            if _canonical_identity(discovery, restored_id) != job.get("old_identity"):
                raise ValueError("The restored Runtime identity did not match the prior release.")
            message = f"Rolled back: {_safe_failure_line(failure)}"
            update_job._write_job(root, job["operation_id"], state="rolled_back", message=message,
                                  transaction_id=journal.get("operation_id"))
            return
        # A pre-stop or staged failure may leave C2 complete/absent. Report
        # failed only when the old selected owner still passes canonical ready.
        if selected.document() != accepted_old.document():
            raise ValueError("The accepted old version is no longer selected.")
        realm = backend.workspace(selected, data)["realm_id"]
        backend.verify(selected, data, realm)
        discovery = read_json(data / "runtime" / "discovery.json")
        instance_id = discovery.get("runtime_instance_id")
        if _canonical_identity(discovery, instance_id) != job.get("old_identity"):
            raise ValueError("The previous Runtime identity is not ready.")
        update_job._write_job(root, job["operation_id"], state="failed",
                              message=f"Failed: {_safe_failure_line(failure)}")
    except (Exception, KeyboardInterrupt):
        update_job._write_job(root, job["operation_id"], state="recovery-needed",
                              message="Recovery needed. Astrid could not confirm the previous Runtime is ready.",
                              transaction_id=journal.get("operation_id") if journal else None)


def _safe_failure_line(failure: BaseException) -> str:
    value = " ".join(str(failure).replace("\x00", " ").splitlines()).strip()
    return (value or "Update failed.")[:180]


def _canonical_identity(discovery: dict, instance_id: str) -> dict:
    endpoint = discovery.get("endpoint") or discovery.get("door_endpoint")
    if not isinstance(endpoint, str) or not endpoint or not isinstance(instance_id, str):
        raise ValueError("The Runtime startup identity is unavailable.")
    from astrid_runtime.runtime_boundary import LocalRuntimeBoundary
    health = LocalRuntimeBoundary().owner_startup_identity(endpoint=endpoint, instance_id=instance_id)
    return Backend._validated_startup_identity(health, instance_id)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="astrid install", description="Install or update Astrid.")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--update", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--update-app", action="store_true", help="update and relaunch Pinokio's app when no workspace migration is needed")
    mode.add_argument("--uninstall", action="store_true")
    mode.add_argument("--doctor", nargs="?", const="__default__", metavar="PATH",
                      help="write a redacted support bundle using the installed version")
    result.add_argument("--delete-data", action="store_true", help="also delete the selected data root during uninstall")
    result.add_argument("--root", type=Path, help="absolute managed program directory")
    result.add_argument("--managed-by", choices=["pinokio"], help="install without a login service; Pinokio owns the process")
    result.add_argument("--data-root", type=Path, help="absolute workspace support directory")
    result.add_argument("--manifest", type=Path, help="local copy of the pinned hosted install manifest")
    result.add_argument("--update-job", help=argparse.SUPPRESS)
    result.add_argument("--uv", type=Path, help="absolute pinned uv executable supplied by the bootstrap")
    result.add_argument("--sandbox", action="store_true",
                        help="run inside a seccomp sandbox such as OpenShell: Runtime's sandbox process control "
                             "(docs/setup/openshell-sandbox.md); selected for this install or update")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    mode = ("app-update" if args.update_app else "update" if args.update else "uninstall" if args.uninstall
            else "doctor" if args.doctor is not None else "install")
    if args.delete_data and mode != "uninstall":
        parser().error("--delete-data is only valid for uninstall.")
    if args.managed_by and (args.root is None or not args.root.expanduser().is_absolute()):
        parser().error("--managed-by pinokio requires an absolute --root.")
    if args.update_app and (args.managed_by != "pinokio" or args.data_root or args.sandbox):
        parser().error("--update-app requires --managed-by pinokio and keeps the existing workspace and process settings.")
    if args.sandbox and mode not in ("install", "update"):
        parser().error("--sandbox is only valid for install or update.")
    if args.update_job and (not args.root or not args.root.is_absolute() or not args.data_root
                            or not args.data_root.is_absolute() or not args.manifest
                            or args.update_app or args.uninstall or args.doctor is not None
                            or args.managed_by or args.sandbox or args.delete_data or args.update):
        parser().error("--update-job requires the selected absolute roots and manifest.")
    if args.update_job and (len(args.update_job) != 32
                            or any(char not in "0123456789abcdef" for char in args.update_job)):
        parser().error("--update-job is invalid.")
    from runtime_protocol import platform
    log = platform.log_dir() / "install.log"
    try:
        platform.private_directory(log.parent)
        root = absolute(args.root or default_program_root(Path.home(), sys.platform))
        uv = args.uv or Path(os.environ.get("ASTRID_INSTALL_UV", bootstrap_root(Path.home(), sys.platform) / ("uv.exe" if sys.platform == "win32" else "uv")))
        process_control = ("sandbox" if args.sandbox else "strict") if mode in {"install", "update"} else None
        backend = Backend(root, log, uv, process_control=process_control)
        marker = read_json(root / "install.json")
        if args.update_job and marker.get("managed_by") is not None:
            raise ValueError("Click-created update jobs require a login-managed Astrid installation.")
        if (args.update_app or (mode in {"install", "update"} and args.root
                and marker.get("managed_by") == "pinokio")):
            from .app_update import recover_app, selection, update_app, update_pinokio

            backend.managed_by = "pinokio"
            pending = read_json(root / "update-transaction.json")
            # The session holds the lifetime installer lock. The shared update
            # adapter serializes the journal separately and takes that lock
            # only for pointer writes, then hands it to the journal-admitted foreground.
            selection(root, backend, allow_transaction=bool(pending))
            if pending and pending.get("step") not in {"complete", "rolled_back"}:
                recover_app(root, backend)
                return 0
            # Recovery-only invocations return above; fresh operations preflight
            # once here before downloads or staging. C1 remains stop authority.
            backend.preflight_stop(absolute(marker["data_root"]))
            if not uv.is_absolute() or not uv.is_file():
                raise ValueError("Run the install line to provide the pinned user-local uv.")
            manifest_path = args.manifest or root / "install-manifest.json"
            if not args.manifest:
                download("https://astrid.haus/install-manifest.json", manifest_path)
            manifest = validate_manifest(read_json(manifest_path))
            if args.update_app:
                update_app(root, backend, manifest)
            else:
                update_pinokio(root, backend, manifest, force=args.update)
            return 0
        lock_path = installer_lock_path(Path.home(), sys.platform)
        platform.private_directory(lock_path.parent)
        with lock_path.open("a+b") as lock:
            locked = False
            if mode != "doctor":
                try:
                    platform.lock_file(lock.fileno(), blocking=False)
                except BlockingIOError:
                    # These reads only explain contention. Ownership and all
                    # mutations are checked below, after obtaining the lock.
                    prior = read_json(root / "install.json")
                    recorded = backend.service.recorded()
                    pinokio_live = _pinokio_claim_is_live(recorded)
                    if not args.root and (pinokio_live
                                          or prior.get("managed_by") == "pinokio"):
                        raise ValueError(PINOKIO_UPDATE_UNINSTALL_REFUSAL) from None
                    if (args.managed_by == "pinokio" or prior.get("managed_by") == "pinokio"
                            or (args.root and pinokio_live
                                and recorded.get("program_root") == str(root))):
                        raise ValueError(PINOKIO_STOP_REFUSAL) from None
                    if pinokio_live:
                        raise ValueError(PINOKIO_UPDATE_UNINSTALL_REFUSAL) from None
                    raise
                locked = True
            try:
                # The single per-user selection is also the Pinokio pointer.
                # Inspect it under the installer lock before claiming any root.
                recorded = backend.service.recorded()
                release_stale_selection = False
                if recorded.get("managed_by") == "pinokio" and not _pinokio_claim_is_live(recorded):
                    # Reset or deleting the Pinokio app can remove its program
                    # files without letting the installer clear its per-user
                    # selection. Keep the workspace receipt, but release the
                    # stale manager claim while holding the installer lock.
                    recorded = {**recorded, "uninstalled": True}
                    recorded.pop("managed_by", None)
                    recorded.pop("program_root", None)
                    release_stale_selection = mode != "doctor"
                if mode == "doctor" and not args.root and recorded.get("managed_by") == "pinokio":
                    root = absolute(recorded["program_root"])
                    backend = Backend(root, log, uv, process_control=process_control)
                prior = read_json(root / "install.json")
                managed_by = args.managed_by or (prior.get("managed_by") if args.root or mode == "doctor" else None)
                if (managed_by is None and (args.root or mode == "doctor")
                        and recorded.get("managed_by") == "pinokio"
                        and recorded.get("program_root") == str(root)):
                    managed_by = "pinokio"
                if managed_by not in {None, "pinokio"}:
                    raise ValueError("Unknown Astrid installation manager.")
                if recorded.get("managed_by") == "pinokio":
                    if managed_by != "pinokio" or (not args.root and mode != "doctor"):
                        raise ValueError(PINOKIO_UPDATE_UNINSTALL_REFUSAL)
                    if absolute(recorded["program_root"]) != root:
                        raise ValueError(f"Astrid is already installed in Pinokio at {recorded['program_root']}.")
                elif managed_by == "pinokio":
                    login_root = recorded.get("program_root") if not recorded.get("uninstalled") else None
                    default_root = absolute(default_program_root(Path.home(), sys.platform))
                    default_marker = read_json(default_root / "install.json")
                    if not login_root and default_marker.get("owner") == LABEL and default_marker.get("managed_by") != "pinokio":
                        login_root = str(default_root)
                    if prior.get("owner") == LABEL and prior.get("managed_by") != "pinokio":
                        login_root = str(root)
                    if not login_root and backend.service.login_service_exists():
                        login_root = str(backend.service.path) if sys.platform != "win32" else "the Astrid Runtime sign-in task"
                    if login_root:
                        raise ValueError(f"Astrid is already installed on this computer ({login_root}). Uninstall that copy first, then install Astrid in Pinokio.")
                if prior.get("managed_by") == "pinokio" and managed_by != "pinokio":
                    raise ValueError(PINOKIO_UPDATE_UNINSTALL_REFUSAL)
                if (recorded.get("program_root") and managed_by != "pinokio"
                        and (not recorded.get("uninstalled") or mode == "uninstall")):
                    recorded_root = absolute(recorded["program_root"])
                    if args.root and recorded_root != root:
                        raise ValueError(f"The login service already belongs to {recorded_root}.")
                    root = recorded_root
                    backend = Backend(root, log, uv, process_control=process_control)
                    prior = read_json(root / "install.json")
                if not recorded and prior.get("owner") == LABEL and prior.get("data_root"):
                    recorded = {"data_root": prior["data_root"]}
                backend.managed_by = managed_by
                data = absolute(recorded.get("data_root") or args.data_root or platform.data_root())
                if sys.platform == "win32":
                    for name, path in (("program directory", root), ("workspace", data)):
                        managed, installer_state = path.resolve(), lock_path.parent.resolve()
                        if managed.is_relative_to(installer_state) or installer_state.is_relative_to(managed):
                            raise ValueError(f"The {name} contains protected installer state at {lock_path.parent}; choose a separate directory.")
                if root == data or root in data.parents or data in root.parents:
                    raise ValueError("Keep program files and the workspace in separate directories.")
                if sys.platform == "win32" and len(str(root)) > 90:
                    raise ValueError("Choose a shorter program directory (under 90 characters) for Windows tools.")
                marker = read_json(root / "install.json")
                if marker and marker.get("owner") != LABEL:
                    raise ValueError("The program directory belongs to another installer.")
                if not marker and root.exists() and any(p.name != "bootstrap" for p in root.iterdir()):
                    raise ValueError("The program directory contains files Astrid does not own.")
                pending = read_json(root / "update-transaction.json")
                if (mode in {"install", "update"} and pending
                        and pending.get("step") not in {"complete", "rolled_back"}):
                    if pending.get("transaction_kind") == "app":
                        # Pinokio recovery starts the retained foreground app.
                        # Drop the broad installer lock first; recover_app
                        # revalidates immutable selection under its own lock
                        # and takes the installer lock only for pointer writes.
                        platform.unlock_file(lock.fileno())
                        locked = False
                        from .app_update import recover_app

                        recover_app(root, backend)
                    else:
                        from .app_update import locked as app_locked

                        with app_locked(root / ".app-update.lock"):
                            Installer(root, data, backend, managed_by=managed_by)._recover_update()
                    return 0
                if mode == "uninstall" and not marker and recorded.get("owner") != LABEL:
                    raise ValueError("No managed Astrid installation was found to uninstall.")
                installed = current(root)
                if installed is not None and not installed.python.is_file():
                    installed = None
                update_job_record = None
                if args.update_job:
                    from . import update_job as update_jobs

                    update_job_record = update_jobs._read_job(root, args.update_job)
                    accepted_old, selected_data = update_jobs._validate_worker_binding(root, update_job_record)
                    metadata = update_job_record.get("metadata", {})
                    if (update_job_record.get("state") != "active" or installed is None
                            or installed.document() != update_job_record.get("old_version")
                            or accepted_old.document() != installed.document()
                            or data != selected_data or root != absolute(update_job_record.get("program_root", ""))
                            or args.manifest != Path(metadata.get("manifest_path", ""))):
                        raise ValueError("The accepted update job no longer matches this installation.")
                    update_jobs.validate_update_inputs(
                        update_jobs._validate_input_paths(metadata)[0],
                        {"manifest_sha256": metadata["manifest_sha256"],
                         "bootstrap_sha256": metadata["bootstrap_sha256"],
                         "release": metadata["release"]}, installed.release)
                    if validate_manifest(read_json(args.manifest)).get("release", 0) != update_job_record.get("target_release"):
                        raise ValueError("The selected update manifest release changed.")
                    if (Path(update_job_record["log_path"]).is_symlink()
                            or not Path(update_job_record["log_path"]).is_relative_to(root)):
                        raise ValueError("The accepted update log path is invalid.")
                    backend.log = Path(update_job_record["log_path"])
                    log = backend.log
                if mode != "doctor":
                    if managed_by != "pinokio" and backend.source_checkout_ownership(data, installed):
                        raise ValueError("This Astrid runs from source checkouts; update those instead.")
                    # Preflight each fresh operation once, before install/selection
                    # claims or staging. Every real stop still takes Runtime's fence.
                    backend.preflight_stop(data)
                if release_stale_selection:
                    write_json(backend.service.selection, recorded)
                if mode not in {"uninstall", "doctor"}:
                    platform.private_directory(root)
                    marker = {**marker, "owner": LABEL, "data_root": str(data)}
                    if managed_by:
                        marker["managed_by"] = managed_by
                    write_json(root / "install.json", marker)
                    if managed_by == "pinokio":
                        write_json(backend.service.selection, {**recorded, "owner": LABEL,
                                   "program_root": str(root), "data_root": str(data), "managed_by": "pinokio"})
                print(f"Using your workspace at {data}.")
                manifest = {}
                if mode not in {"uninstall", "doctor"}:
                    if not args.manifest:
                        args.manifest = root / "install-manifest.json"
                        download("https://astrid.haus/install-manifest.json", args.manifest)
                    manifest = validate_manifest(read_json(args.manifest))
                bundle = None
                if mode == "doctor":
                    bundle = default_doctor_bundle() if args.doctor == "__default__" else Path(args.doctor)
                options = {"managed_by": managed_by} if managed_by else {}
                installer = Installer(root, data, backend, **options)
                if args.update_job:
                    backend._update_job_operation_id = args.update_job
                    try:
                        installer.run(mode, manifest, delete_data=args.delete_data, bundle=bundle)
                    except (Exception, KeyboardInterrupt) as update_failure:
                        _record_update_failure(root, data, backend, update_job_record,
                                               accepted_old, update_failure)
                        raise
                    else:
                        _record_update_success(root, data, backend, update_job_record)
                else:
                    installer.run(mode, manifest, delete_data=args.delete_data, bundle=bundle)
            finally:
                if locked:
                    platform.unlock_file(lock.fileno())
        return 0
    except (Exception, KeyboardInterrupt) as failure:
        with contextlib.suppress(OSError):
            with log.open("a", encoding="utf-8") as stream:
                traceback.print_exc(file=stream)
        print(FAILURE_MESSAGE)
        if isinstance(failure, (BackupSpaceError, ValueError, RuntimeOperationError)):
            print(str(failure))
            if isinstance(failure, RuntimeOperationError) and failure.details is not None:
                print(json.dumps(failure.details, ensure_ascii=False))
        elif isinstance(failure, VerificationError) and failure.safe_message:
            print(failure.safe_message)
        print(f"Details: {log}")
        return 1
