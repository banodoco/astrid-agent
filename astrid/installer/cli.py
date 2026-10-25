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
    absolute,
    current,
    read_json,
    write_json,
)
from .service import LABEL, bootstrap_root, default_program_root
from .support import FAILURE_MESSAGE, default_doctor_bundle
from .tools import download, validate_manifest


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="astrid install", description="Install or restart Astrid for astrid.haus.")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--update", action="store_true", help="back up and install the pinned version")
    mode.add_argument("--uninstall", action="store_true")
    mode.add_argument("--doctor", nargs="?", const="__default__", metavar="PATH",
                      help="write a redacted support bundle using the installed version")
    result.add_argument("--delete-data", action="store_true", help="also delete the selected data root during uninstall")
    result.add_argument("--root", type=Path, help="absolute managed program directory")
    result.add_argument("--data-root", type=Path, help="absolute workspace support directory")
    result.add_argument("--manifest", type=Path, help="local copy of the pinned hosted install manifest")
    result.add_argument("--uv", type=Path, help="absolute pinned uv executable supplied by the bootstrap")
    result.add_argument("--sandbox", action="store_true",
                        help="run inside a seccomp sandbox such as OpenShell: Runtime's sandbox process control "
                             "(docs/setup/openshell-sandbox.md); kept by later updates")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    mode = ("update" if args.update else "uninstall" if args.uninstall
            else "doctor" if args.doctor is not None else "install")
    if args.delete_data and mode != "uninstall":
        parser().error("--delete-data is only valid for uninstall.")
    if args.sandbox and mode not in ("install", "update"):
        parser().error("--sandbox is only valid for install or update.")
    from runtime_protocol import platform
    log = platform.log_dir() / "install.log"
    try:
        platform.private_directory(log.parent)
        root = absolute(args.root or default_program_root(Path.home(), sys.platform))
        uv = args.uv or Path(os.environ.get("ASTRID_INSTALL_UV", bootstrap_root(Path.home(), sys.platform) / ("uv.exe" if sys.platform == "win32" else "uv")))
        backend = Backend(root, log, uv, process_control="sandbox" if args.sandbox else None)
        recorded = backend.service.recorded()
        if not recorded:
            prior = read_json(root / "install.json")
            if prior.get("owner") == LABEL and prior.get("data_root"):
                recorded = {"data_root": prior["data_root"]}
        if recorded.get("program_root"):
            recorded_root = absolute(recorded["program_root"])
            if args.root and recorded_root != root:
                raise ValueError(f"The login service already belongs to {recorded_root}.")
            root = recorded_root
            backend = Backend(root, log, uv, process_control="sandbox" if args.sandbox else None)
        # Windows keeps open files locked, so its per-user lock must survive
        # removal of both the program directory and the default workspace.
        lock_parent = default_program_root(Path.home(), sys.platform).parent
        if sys.platform == "win32":
            lock_parent = lock_parent / "Astrid Installer"
        lock_path = lock_parent / "astrid-install.lock"
        platform.private_directory(lock_path.parent)
        with lock_path.open("a+b") as lock:
            platform.lock_file(lock.fileno(), blocking=False)
            try:
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
                if mode not in {"uninstall", "doctor"}:
                    platform.private_directory(root)
                    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
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
                Installer(root, data, backend).run(mode, manifest, delete_data=args.delete_data, bundle=bundle)
            finally:
                platform.unlock_file(lock.fileno())
        return 0
    except (Exception, KeyboardInterrupt) as failure:
        with contextlib.suppress(OSError):
            with log.open("a", encoding="utf-8") as stream:
                traceback.print_exc(file=stream)
        print(FAILURE_MESSAGE)
        if isinstance(failure, BackupSpaceError):
            print(str(failure))
        print(f"Details: {log}")
        return 1
