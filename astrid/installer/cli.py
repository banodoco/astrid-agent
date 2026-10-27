"""`astrid install`: the one install, repair, update and uninstall entry point."""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import traceback
from pathlib import Path

from .backend import Backend
from .core import (
    BackupSpaceError,
    Installer,
    VerificationError,
    RuntimeOperationError,
    absolute,
    current,
    read_json,
    write_json,
)
from .service import LABEL, bootstrap_root, default_program_root, installer_lock_path
from .support import FAILURE_MESSAGE, default_doctor_bundle
from .tools import download, validate_manifest


def _pinokio_claim_is_live(recorded: dict) -> bool:
    if recorded.get("managed_by") != "pinokio":
        return False
    program_root = recorded.get("program_root")
    if not isinstance(program_root, str) or not program_root:
        return False
    marker = read_json(absolute(program_root) / "install.json")
    return marker.get("owner") == LABEL and marker.get("managed_by") == "pinokio"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="astrid install", description="Install or restart Astrid for astrid.haus.")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--update", action="store_true", help="back up and install the pinned version")
    mode.add_argument("--update-app", action="store_true", help="update and relaunch Pinokio's app when no workspace migration is needed")
    mode.add_argument("--uninstall", action="store_true")
    mode.add_argument("--doctor", nargs="?", const="__default__", metavar="PATH",
                      help="write a redacted support bundle using the installed version")
    result.add_argument("--delete-data", action="store_true", help="also delete the selected data root during uninstall")
    result.add_argument("--root", type=Path, help="absolute managed program directory")
    result.add_argument("--managed-by", choices=["pinokio"], help="install without a login service; Pinokio owns the process")
    result.add_argument("--data-root", type=Path, help="absolute workspace support directory")
    result.add_argument("--manifest", type=Path, help="local copy of the pinned hosted install manifest")
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
    from runtime_protocol import platform
    log = platform.log_dir() / "install.log"
    try:
        platform.private_directory(log.parent)
        root = absolute(args.root or default_program_root(Path.home(), sys.platform))
        uv = args.uv or Path(os.environ.get("ASTRID_INSTALL_UV", bootstrap_root(Path.home(), sys.platform) / ("uv.exe" if sys.platform == "win32" else "uv")))
        process_control = ("sandbox" if args.sandbox else "strict") if mode in {"install", "update"} else None
        backend = Backend(root, log, uv, process_control=process_control)
        if args.update_app:
            from .app_update import selection, update_app

            backend.managed_by = "pinokio"
            selection(root, backend)  # Validate existing ownership before downloading anything.
            if not uv.is_absolute() or not uv.is_file():
                raise ValueError("Run the install line to provide the pinned user-local uv.")
            manifest_path = args.manifest or root / "install-manifest.json"
            if not args.manifest:
                download("https://astrid.haus/install-manifest.json", manifest_path)
            update_app(root, backend, validate_manifest(read_json(manifest_path)))
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
                        raise ValueError("Astrid on this computer was installed with Pinokio. Use Pinokio to update or uninstall it.") from None
                    if (args.managed_by == "pinokio" or prior.get("managed_by") == "pinokio"
                            or (args.root and pinokio_live
                                and recorded.get("program_root") == str(root))):
                        raise ValueError("Astrid is still running. Stop it in Pinokio, then try again.") from None
                    if pinokio_live:
                        raise ValueError("Astrid on this computer was installed with Pinokio. Use Pinokio to update or uninstall it.") from None
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
                        raise ValueError("Astrid on this computer was installed with Pinokio. Use Pinokio to update or uninstall it.")
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
                    raise ValueError("Astrid on this computer was installed with Pinokio. Use Pinokio to update or uninstall it.")
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
                if mode == "uninstall" and not marker and recorded.get("owner") != LABEL:
                    raise ValueError("No managed Astrid installation was found to uninstall.")
                if mode != "doctor":
                    # A read-only busy refusal must precede install/selection
                    # claims. The real stop repeats this under Runtime's fence.
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
                installed = current(root)
                if installed is not None and not installed.python.is_file():
                    installed = None
                installed = installed or backend.existing(data)
                if mode not in {"uninstall", "doctor"} and (mode == "update" or installed is None):
                    if not args.manifest:
                        args.manifest = root / "install-manifest.json"
                        download("https://astrid.haus/install-manifest.json", args.manifest)
                    manifest = validate_manifest(read_json(args.manifest))
                    if not uv.is_absolute() or not uv.is_file():
                        raise ValueError("Run the install line to provide the pinned user-local uv.")
                bundle = None
                if mode == "doctor":
                    bundle = default_doctor_bundle() if args.doctor == "__default__" else Path(args.doctor)
                options = {"managed_by": managed_by} if managed_by else {}
                Installer(root, data, backend, **options).run(mode, manifest, delete_data=args.delete_data, bundle=bundle)
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
        elif isinstance(failure, VerificationError) and failure.safe_message:
            print(failure.safe_message)
        print(f"Details: {log}")
        return 1
