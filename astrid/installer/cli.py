"""`astrid install`: the one install, repair, update and uninstall entry point."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import traceback
from pathlib import Path

from . import update_job
from .backend import Backend
from .core import (
    TERMINAL_STEPS,
    Installer,
    RuntimeOperationError,
    VerificationError,
    absolute,
    current,
    pending_transaction,
    read_json,
    separate_roots,
    write_json,
)
from .service import LABEL, default_program_root, default_uv, installer_lock_path, pinokio_owned
from .support import FAILURE_MESSAGE, default_doctor_bundle
from .tools import download, validate_manifest

PINOKIO_UPDATE_UNINSTALL_REFUSAL = (
    "Astrid on this computer was installed with Pinokio. Choose Pinokio's Update or Uninstall button."
)
PINOKIO_STOP_REFUSAL = "Astrid is still running. Choose Pinokio's Stop button, then try again."
RECOVERED_PREVIOUS_VERSION = "Recovered your previous version — run Update again"


def _pinokio_claim_is_live(recorded: dict) -> bool:
    if recorded.get("managed_by") != "pinokio":
        return False
    program_root = recorded.get("program_root")
    if not isinstance(program_root, str) or not program_root:
        return False
    return pinokio_owned(read_json(absolute(program_root) / "install.json"))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="astrid install", description="Install or update Astrid.")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--update", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--update-app", action="store_true", help="update and relaunch Pinokio's app when no workspace migration is needed")
    mode.add_argument("--uninstall", action="store_true")
    mode.add_argument("--doctor", nargs="?", const="__default__", metavar="PATH",
                      help="write a redacted support bundle using the installed version")
    mode.add_argument("--update-recovery", action="store_true",
                      help="plan or apply data-preserving recovery of unresolved update evidence")
    result.add_argument("--delete-data", action="store_true", help="also delete the selected data root during uninstall")
    result.add_argument("--root", type=Path, help="absolute managed program directory")
    result.add_argument("--managed-by", choices=["pinokio"], help="install without a login service; Pinokio owns the process")
    result.add_argument("--data-root", type=Path, help="absolute workspace support directory")
    result.add_argument("--manifest", type=Path, help="local copy of the pinned hosted install manifest")
    result.add_argument("--update-job", help=argparse.SUPPRESS)
    result.add_argument("--apply", metavar="PLAN_SHA256",
                        help="apply the reviewed --update-recovery plan hash")
    result.add_argument("--uv", type=Path, help="absolute pinned uv executable supplied by the bootstrap")
    result.add_argument("--sandbox", action="store_true",
                        help="run inside a seccomp sandbox such as OpenShell: Runtime's sandbox process control "
                             "(docs/setup/openshell-sandbox.md); selected for this install or update")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    mode = ("update-recovery" if args.update_recovery else "app-update" if args.update_app else "update" if args.update else "uninstall" if args.uninstall
            else "doctor" if args.doctor is not None else "install")
    if args.apply and mode != "update-recovery":
        parser().error("--apply is only valid with --update-recovery.")
    if mode == "update-recovery":
        if args.root is None or args.data_root is None:
            parser().error("--update-recovery requires --root and --data-root.")
        if args.manifest or args.managed_by or args.sandbox or args.delete_data or args.uv or args.update_job:
            parser().error("--update-recovery only accepts --root, --data-root, and optional --apply.")
        from .maintenance import MaintenanceRefusal, apply_update_recovery, plan_update_recovery

        try:
            result = (apply_update_recovery(args.root, args.data_root, args.apply)
                      if args.apply else plan_update_recovery(args.root, args.data_root))
        except MaintenanceRefusal as failure:
            payload = {"state": "recovery-needed", "reason": failure.reason, "message": str(failure)}
            if failure.plan is not None:
                payload["plan"] = failure.plan
            print(json.dumps(payload, sort_keys=True))
            return 1
        print(json.dumps(result, sort_keys=True))
        return 0
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
    # Runtime's Windows default log directory is inside R0's data root.
    # R1 installer diagnostics must be outside both retained workspaces,
    # including when admission refuses before staging.
    log = default_program_root(Path.home(), sys.platform).parent / "astrid-installer-r1" / "install.log"
    safe_log = False
    try:
        from .service import install_selection
        log = absolute(log)
        for target in (log, log.with_name("runtime-service.log")):
            target = absolute(target)
            if target.exists() and (not target.is_file() or target.stat().st_nlink != 1):
                raise ValueError("An installer-owned log is not a private regular file.")
        installed_record = read_json(absolute(install_selection(Path.home(), sys.platform)))
        root = absolute(args.root or default_program_root(Path.home(), sys.platform))
        local_record = read_json(absolute(root / "install.json"))
        roots = [installed_record.get("data_root"), installed_record.get("r1_data_root"),
                 local_record.get("data_root"), local_record.get("r1_data_root"),
                 args.data_root, platform.data_root()]
        for source in roots:
            if source and log.is_relative_to(absolute(source)):
                raise ValueError("Installer logs must be outside the retained workspace roots.")
        safe_log = True
        platform.private_directory(log.parent)
        root = absolute(args.root or default_program_root(Path.home(), sys.platform))
        uv = args.uv or default_uv()
        process_control = ("sandbox" if args.sandbox else "strict") if mode in {"install", "update"} else None
        backend = Backend(root, log, uv, process_control=process_control)
        marker = read_json(root / "install.json")
        if args.update_job and marker.get("managed_by") is not None:
            raise ValueError("Click-created update jobs require a login-managed Astrid installation.")
        if (args.update_app or (mode in {"install", "update"} and args.root
                and marker.get("managed_by") == "pinokio")):
            from .app_update import (
                preflight_pinokio_update,
                recover_app,
                selection,
                update_app,
                update_pinokio,
            )

            backend.managed_by = "pinokio"
            pending = read_json(root / "update-journal.r1.json")
            # A running session holds lifetime admission. A stopped operation
            # retains its own custody across manifest preparation and activation.
            selection(root, backend, allow_transaction=bool(pending))
            from .custody import operation

            if not pending or pending.get("step") in TERMINAL_STEPS:
                preflight_pinokio_update(root, backend, absolute(marker.get("r1_data_root") or marker["data_root"]))
            with operation(root, backend):
                if pending and pending.get("step") not in TERMINAL_STEPS:
                    recover_app(root, backend)
                    print(RECOVERED_PREVIOUS_VERSION)
                    return 0
                # Recovery-only invocations return above; fresh operations preflight
                # once here before downloads or staging. C1 remains stop authority.
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
                    recorded = {key: prior[key] for key in ("data_root", "r1_data_root") if prior.get(key)}
                backend.managed_by = managed_by
                data = absolute(recorded.get("r1_data_root") or recorded.get("data_root") or args.data_root or platform.data_root())
                fresh_fields = None
                if (mode in {"install", "update"} and not recorded and current(root) is None
                        and not prior.get("data_root")):
                    legacy_data = data
                    if legacy_data.exists() and any(legacy_data.iterdir()):
                        raise ValueError("The previous workspace has no proven installation identity; repair its selection before updating.")
                    data = absolute(legacy_data.with_name(legacy_data.name + "-r1"))
                    fresh_fields = {"data_root": str(legacy_data), "r1_data_root": str(data)}
                if sys.platform == "win32":
                    for name, path in (("program directory", root), ("workspace", data)):
                        managed, installer_state = path.resolve(), lock_path.parent.resolve()
                        if managed.is_relative_to(installer_state) or installer_state.is_relative_to(managed):
                            raise ValueError(f"The {name} contains protected installer state at {lock_path.parent}; choose a separate directory.")
                if not separate_roots(root, data):
                    raise ValueError("Keep program files and the workspace in separate directories.")
                if sys.platform == "win32" and len(str(root)) > 90:
                    raise ValueError("Choose a shorter program directory (under 90 characters) for Windows tools.")
                marker = read_json(root / "install.json")
                if marker and marker.get("owner") != LABEL:
                    raise ValueError("The program directory belongs to another installer.")
                if not marker and root.exists() and any(p.name != "bootstrap" for p in root.iterdir()):
                    raise ValueError("The program directory contains files Astrid does not own.")
                pending_update = pending_transaction(root)
                if (mode != "doctor" and pending_update
                        and pending_update.get("transaction_kind") == "copy"):
                    backend.require_copy_recovery_contract()
                if mode in {"install", "update"} and pending_update:
                    if pending_update.get("transaction_kind") == "app":
                        # Pinokio recovery starts the retained foreground app.
                        # Drop the broad installer lock first; recover_app
                        # revalidates immutable selection under its own lock
                        # and takes the installer lock only for pointer writes.
                        platform.unlock_file(lock.fileno())
                        locked = False
                        from .app_update import recover_app

                        recover_app(root, backend)
                        print(RECOVERED_PREVIOUS_VERSION)
                    else:
                        from .app_update import locked as app_locked

                        with app_locked(root / ".app-update.lock"):
                            Installer(root, data, backend, managed_by=managed_by)._recover_update()
                        if marker.get("owner") == LABEL:
                            update_job.sweep_owned_jobs(root, locked=True)
                        print(RECOVERED_PREVIOUS_VERSION)
                    return 0
                if mode in {"install", "update", "app-update"} and marker.get("owner") == LABEL:
                    # Sweep only jobs and artifacts named by Astrid's durable
                    # ownership record. Active and unqueryable objects remain.
                    update_job.sweep_owned_jobs(root, locked=True)
                    marker = read_json(root / "install.json")
                if mode == "uninstall" and not marker and recorded.get("owner") != LABEL:
                    raise ValueError("No managed Astrid installation was found to uninstall.")
                installed = current(root)
                if installed is not None and not installed.python.is_file():
                    installed = None
                update_job_record: dict | None = None
                if args.update_job:
                    update_job_record, accepted_old = update_job.admit_worker(
                        root, args.update_job, backend, installed, data, args.manifest)
                    if update_job_record is None:
                        raise ValueError("The update job could not be admitted.")
                    log = backend.log
                if mode != "doctor":
                    if managed_by != "pinokio" and backend.source_checkout_ownership(data, installed):
                        raise ValueError("This Astrid runs from source checkouts; update those instead.")
                    # Preflight each fresh operation once, before install/selection
                    # claims or staging. Copy updates quit through the validated
                    # native job; other operations retain Runtime's stop fence.
                    if mode in {"install", "update"} and installed and not recorded.get("r1_data_root"):
                        backend.copy_preflight(data)
                        backend.require_copy_receipt_contract()
                    else:
                        backend.preflight_stop(data)
                if release_stale_selection:
                    write_json(backend.service.selection, recorded)
                if mode not in {"uninstall", "doctor"}:
                    platform.private_directory(root)
                    from .copy_update import data_fields
                    marker = {**marker, "owner": LABEL, **(fresh_fields or data_fields(marker, data))}
                    if managed_by:
                        marker["managed_by"] = managed_by
                    write_json(root / "install.json", marker)
                    if managed_by == "pinokio":
                        write_json(backend.service.selection, {**recorded, "owner": LABEL,
                                   "program_root": str(root), **data_fields(recorded, data), "managed_by": "pinokio"})
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
                    if update_job_record is None:
                        raise ValueError("The update job could not be admitted.")
                    setattr(backend, "_update_job_operation_id", args.update_job)
                    try:
                        installer.run(mode, manifest, delete_data=args.delete_data, bundle=bundle)
                    except (Exception, KeyboardInterrupt) as update_failure:
                        update_job.record_failure(root, data, backend, update_job_record,
                                                  accepted_old, update_failure)
                        raise
                    else:
                        update_job.record_success(root, data, backend, update_job_record)
                else:
                    installer.run(mode, manifest, delete_data=args.delete_data, bundle=bundle)
            finally:
                if locked:
                    platform.unlock_file(lock.fileno())
        return 0
    except (Exception, KeyboardInterrupt) as failure:  # noqa: BLE001 - CLI boundary renders all install failures.
        with contextlib.suppress(OSError):
            if not safe_log:
                raise OSError("No safe installer log destination was established.")
            with log.open("a", encoding="utf-8") as stream:
                platform.owner_only(stream.fileno())
                traceback.print_exc(file=stream)
        print(FAILURE_MESSAGE)
        if isinstance(failure, (ValueError, RuntimeOperationError)):
            print(str(failure))
            if isinstance(failure, RuntimeOperationError) and failure.details is not None:
                print(json.dumps(failure.details, ensure_ascii=False))
        elif isinstance(failure, VerificationError) and failure.safe_message:
            print(failure.safe_message)
        print(f"Details: {log}")
        return 1
