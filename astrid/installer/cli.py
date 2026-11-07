"""`astrid install`: the one install, repair, update and uninstall entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

from . import update_job
from .backend import Backend
from .choices import (
    CHOICE_NAMES,
    RECOMMENDED_CHOICES,
    parse_choice,
    render_plan,
    resolve_choices,
    unattended_consent_required,
)
from .core import (
    TERMINAL_STEPS,
    BackupSpaceError,
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


def _prompt_choices(*, input_fn=None, output=None):
    """Ask once, then ask each recommended choice only when Customize is selected."""
    input_fn = input_fn or input
    output = output or sys.stdout
    print("Recommended Astrid setup:", file=output)
    for name in CHOICE_NAMES:
        value = "on" if RECOMMENDED_CHOICES[name] else "off"
        print(f"  {name.replace('_', ' ')}: {value}", file=output)
    while True:
        output.write("Continue with this setup? [Y/n/c]\n")
        output.flush()
        answer = input_fn().strip().lower()
        if answer in {"", "y", "yes"}:
            return dict(RECOMMENDED_CHOICES), False
        if answer in {"n", "no"}:
            return None, True
        if answer in {"c", "customize"}:
            choices = {}
            labels = {
                "autostart": "Start Astrid at sign-in?",
                "command": "Add the astrid terminal command?",
                "skills": "Install Astrid skills for detected agents?",
                "skill_copies": "Use copies for skills?",
            }
            for name in CHOICE_NAMES:
                default = "Y/n" if RECOMMENDED_CHOICES[name] else "y/N"
                while True:
                    output.write(f"{labels[name]} [{default}]\n")
                    output.flush()
                    response = input_fn().strip().lower()
                    if response in {"", "y", "yes"}:
                        choices[name] = RECOMMENDED_CHOICES[name] if not response else True
                        break
                    if response in {"n", "no"}:
                        choices[name] = False
                        break
            return choices, False


def _way_back_command(choices: dict[str, bool] | None, *, mode: str) -> str:
    selected = resolve_choices(prompt=choices, accept_recommended=choices is None)
    flags = ["--yes"]
    if mode == "update":
        flags.append("--update")
    flags.extend(
        f"--{'skill-copies' if name == 'skill_copies' else name} {'on' if selected[name] else 'off'}"
        for name in CHOICE_NAMES
    )
    arguments = " ".join(flags)
    if sys.platform == "win32":
        return f"& ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) {arguments}"
    return f"curl -fsSL https://astrid.haus/install | sh -s -- {arguments}"


def _print_run_summary(choices, *, mode, outcomes=None, failure=None, declined=False,
                       result_note=None, install_root: Path | None = None):
    def safe(value):
        return str(value).encode("ascii", errors="backslashreplace").decode("ascii")

    print("\nAstrid setup summary:")
    if declined or result_note:
        print(f"  Result: {result_note or 'setup declined; no installation changes were applied.'}")
    else:
        outcomes = outcomes or {}
        print(f"  Startup: {outcomes.get('startup', 'not confirmed')}.")
        registered = outcomes.get("autostart_registered")
        status = "registered" if registered is True else "not registered" if registered is False else "not confirmed"
        print(f"  Sign-in autostart: {status}.")
        command = outcomes.get("command")
        if command is None:
            print("  Command/PATH: not reached in this run.")
        elif command.get("action") == "disabled":
            effects = []
            if command.get("removed_shim"):
                effects.append("Astrid's command shim removed")
            if command.get("removed_path"):
                effects.append("Astrid's PATH entry removed")
            if command.get("preserved_shim"):
                effects.append("command shim preserved because ownership could not be verified")
            if command.get("error"):
                message = f"; cleanup failed ({command['error']})"
                if effects:
                    message += "; " + ", ".join(effects)
            else:
                message = "; " + ", ".join(effects) if effects else "; no owned command/PATH changes were needed"
            print("  Command/PATH: off" + message + ".")
        elif command.get("error"):
            print(f"  Command/PATH: setup failed ({command['error']}); use the run-astrid launcher.")
        elif command.get("path") is None:
            print("  Command/PATH: command shim was skipped; see the installer output above.")
        elif command.get("action") == "ready":
            change = command.get("shim_change") or "available"
            detail = f"shim {change} at {safe(command['path'])}"
            if command.get("path_added"):
                detail += f"; user PATH entry added: {safe(command['path_added'])}"
            elif command.get("path_present"):
                detail += "; PATH already contains Astrid's command directory"
            elif command.get("path_hint"):
                detail += "; follow the PATH instruction above"
            elif command.get("path_present") is False:
                detail += "; PATH entry remains absent"
            print("  Command/PATH: " + detail + ".")
        elif command.get("path_hint"):
            print(f"  Command/PATH: shim created at {safe(command['path'])}; follow the PATH instruction above.")
        else:
            print(f"  Command/PATH: shim created at {safe(command['path'])}.")
        skills = outcomes.get("skills")
        if skills is None:
            print("  Skill destinations: not confirmed in this run.")
        elif not skills.get("ok"):
            print("  Skill destinations: sync failed; see the installer log.")
        elif skills.get("destinations"):
            print("  Skill destinations: " + ", ".join(safe(path) for path in skills["destinations"]))
        elif not skills.get("removed"):
            print("  Skill destinations: no target changes were reported.")
        if skills is not None:
            if skills.get("removed"):
                print("  Astrid skill targets removed: " + ", ".join(safe(path) for path in skills["removed"]))
            for conflict in skills.get("conflicts", []):
                if isinstance(conflict, dict):
                    where = safe(conflict.get("path", "(unknown path)"))
                    reason = safe(conflict.get("reason", "ownership is ambiguous"))
                    print(f"  Skill target preserved: {where} ({reason}).")
        if failure is not None:
            safe_context = failure.safe_message if isinstance(failure, VerificationError) else None
            if safe_context:
                print(f"  Failure: {type(failure).__name__}: {safe(safe_context)}")
            else:
                print(f"  Failure: {type(failure).__name__}")
    selected = resolve_choices(prompt=choices, accept_recommended=choices is None)
    if not selected["autostart"]:
        print("  Startup recovery: Astrid will not start at sign-in. To start it later, rerun the install line; to enable sign-in startup, replace --autostart off with --autostart on in the Way back line.")
    if not selected["command"]:
        print("  Command recovery: the astrid command shim is off. To restore it, replace --command off with --command on in the Way back line.")
        if install_root is not None:
            launcher = install_root / ("run-astrid.cmd" if sys.platform == "win32" else "run-astrid")
            if launcher.is_file():
                print(f"  Absolute Astrid CLI path while the command is off: {safe(launcher)}")
    if not selected["skills"]:
        print("  Skills recovery: to enable Astrid skills later, rerun the install line with --skills on (replace this flag in the Way back line).")
    if not selected["skill_copies"]:
        print("  Skill copies recovery: to enable skill copies later, rerun the install line with --skill-copies on (replace this flag in the Way back line).")
    print("  Maintenance: rerun the install line to repair or restart; add --update to update, or --uninstall to remove Astrid. Uninstall keeps the workspace unless --delete-data is added.")
    print("  Way back: " + _way_back_command(choices, mode=mode))


class _ChoiceAction(argparse.Action):
    """Store each public choice flag once and reject duplicate flags."""

    def __call__(self, parser, namespace, values, option_string=None):
        seen = getattr(namespace, "_seen_choices", set())
        if self.dest in seen:
            raise argparse.ArgumentError(self, f"{option_string} may only be supplied once.")
        seen = set(seen)
        seen.add(self.dest)
        setattr(namespace, "_seen_choices", seen)
        try:
            parsed = parse_choice(values)
        except ValueError as failure:
            raise argparse.ArgumentError(self, str(failure)) from failure
        current_values = dict(getattr(namespace, "choices", None) or {})
        current_values[self.dest] = parsed
        setattr(namespace, "choices", current_values)


def _pinokio_claim_is_live(recorded: dict) -> bool:
    if recorded.get("managed_by") != "pinokio":
        return False
    program_root = recorded.get("program_root")
    if not isinstance(program_root, str) or not program_root:
        return False
    return pinokio_owned(read_json(absolute(program_root) / "install.json"))


def _prepare_log(path: Path, *protected: Path) -> Path | None:
    """Use the configured log only when it cannot write into a selected root."""
    from runtime_protocol import platform

    try:
        # Reject links (including linked parents), and existing hard links that
        # could alias a file inside a protected tree, before mkdir/chmod/open.
        path = absolute(path)
        if any(path.is_relative_to(root) for root in protected):
            return None
        if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
            return None
        platform.private_directory(path.parent)
    except (OSError, ValueError):
        return None
    return path


def _retained_r0_roots(service) -> list[Path]:
    from .boundary_registration import retained_roots
    from .boundary_handoff import retained_roots as handoff_roots
    from .r0_stop import _marker_path

    intent = read_json(absolute(_marker_path(service)))
    roots = [absolute(intent[key]) for key in ("program_root", "data_root")] if intent else []
    return roots + retained_roots(service) + handoff_roots(service)


def _provisional_roots(root: Path, requested_data: Path | None, service, mode: str) -> list[Path]:
    """Read candidate selections only; authoritative selection stays under the lock."""
    from runtime_protocol import platform

    recorded = service.recorded()
    programs = [root]
    if recorded.get("program_root") and (not recorded.get("uninstalled") or mode == "uninstall"):
        programs.append(absolute(recorded["program_root"]))
    protected = programs.copy()
    # Include owned marker data even when the per-user selection has changed.
    for program in programs:
        marker = read_json(program / "install.json")
        if marker.get("owner") == LABEL and marker.get("data_root"):
            protected.append(absolute(marker["data_root"]))
        if marker.get("owner") == LABEL and marker.get("r1_data_root"):
            protected.append(absolute(marker["r1_data_root"]))
    selected_data = absolute(recorded.get("data_root") or requested_data or platform.data_root())
    protected.extend((selected_data, absolute(str(selected_data) + "-r1")))
    if recorded.get("r1_data_root"):
        protected.append(absolute(recorded["r1_data_root"]))
    return protected + _retained_r0_roots(service)


def _check_installer_state(lock_path: Path, *protected: Path) -> None:
    """Refuse before creating/chmodding the state directory or opening the lock."""
    lock_path = absolute(lock_path)
    state = lock_path.parent
    for root in protected:
        # On POSIX the default program directory is a child of the broad
        # per-user state parent. Only the parent's own mutation must be outside
        # the roots. Windows reserves its dedicated installer directory too.
        if state.is_relative_to(root) or (sys.platform == "win32" and root.is_relative_to(state)):
            raise ValueError(
                "The recovery marker must be outside the R0 roots. "
                f"The selected directory contains protected installer state at {state}; choose a separate directory."
            )
    if lock_path.exists() and (not lock_path.is_file() or lock_path.stat().st_nlink != 1):
        raise ValueError("The installer lock is not a private regular file.")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="astrid install", description="Install or update Astrid.")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--update", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--update-app", action="store_true", help="update and relaunch Pinokio's app when no workspace migration is needed")
    mode.add_argument("--uninstall", action="store_true")
    mode.add_argument("--rollback", action="store_true", help="switch back to the retained R0 service, keeping both data roots")
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
    result.add_argument("--yes", action="store_true", help="accept the recommended setup choices")
    result.set_defaults(choices={})
    for name in CHOICE_NAMES:
        flag = "--skill-copies" if name == "skill_copies" else f"--{name}"
        result.add_argument(flag, dest=name, metavar="on|off", action=_ChoiceAction,
                            help=f"set {name.replace('_', ' ')} to on or off")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    mode = ("rollback" if args.rollback else "update-recovery" if args.update_recovery else "app-update" if args.update_app else "update" if args.update else "uninstall" if args.uninstall
            else "doctor" if args.doctor is not None else "install")
    summary_choices = None
    summary_installer = None
    summary_root = None
    declined = False
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
    if (args.yes or args.choices) and mode not in {"install", "update", "app-update"}:
        parser().error("setup choices and --yes are only valid for install or update.")
    if args.update_job and (not args.root or not args.root.is_absolute() or not args.data_root
                            or not args.data_root.is_absolute() or not args.manifest
                            or args.update_app or args.uninstall or args.doctor is not None
                            or args.managed_by or args.sandbox or args.delete_data or args.update or args.rollback):
        parser().error("--update-job requires the selected absolute roots and manifest.")
    if args.update_job and (len(args.update_job) != 32
                            or any(char not in "0123456789abcdef" for char in args.update_job)):
        parser().error("--update-job is invalid.")
    from runtime_protocol import platform
    log = None  # Unresolved selection and unsafe/unavailable logs use the console.
    try:
        root = absolute(args.root or default_program_root(Path.home(), sys.platform))
        log_path = platform.log_dir() / "install.log"
        summary_root = root
        uv = args.uv or default_uv()
        process_control = ("sandbox" if args.sandbox else "strict") if mode in {"install", "update"} else None
        backend = Backend(root, log_path, uv, process_control=process_control)
        backend.console_diagnostics = True
        if mode == "uninstall":
            # This read-only gate precedes installer lock/log creation and all
            # service or selection work. lexists also refuses partial journals.
            from .boundary_handoff import refuse_handoff_uninstall

            refuse_handoff_uninstall(backend.service, root)
            if args.root is None:
                # This is only a candidate read. Selection is resolved again
                # under the lock before it can authorize any operation.
                candidate = backend.service.recorded().get("program_root")
                if candidate:
                    refuse_handoff_uninstall(backend.service, absolute(candidate))
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

            log = _prepare_log(log_path, root, absolute(marker["data_root"]))
            backend.console_diagnostics = log is None
            if log is not None:
                backend.log = log
            backend.managed_by = "pinokio"
            pending = read_json(root / "update-transaction.json")
            if mode in {"install", "update", "app-update"}:
                pinokio_record = backend.service.recorded()
                backend.service.choices = resolve_choices(
                    explicit=args.choices,
                    saved=pinokio_record.get("choices"),
                    accept_recommended=args.yes,
                    existing_install=True,
                )
                prior_skill_policy = resolve_choices(saved=pinokio_record.get("choices"), existing_install=True)
                backend.service.skill_policy_changed = any(
                    name in args.choices and backend.service.choices[name] != prior_skill_policy[name]
                    for name in ("skills", "skill_copies")
                )
            # A running session holds lifetime admission. A stopped operation
            # retains its own custody across manifest preparation and activation.
            selection(root, backend, allow_transaction=bool(pending))
            from .custody import operation

            if not pending or pending.get("step") in TERMINAL_STEPS:
                preflight_pinokio_update(root, backend, absolute(marker["data_root"]))
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
                    outcomes = update_app(root, backend, manifest)
                else:
                    outcomes = update_pinokio(root, backend, manifest, force=args.update)
                _print_run_summary(backend.service.choices, mode=mode, outcomes=outcomes)
                return 0
        lock_path = installer_lock_path(Path.home(), sys.platform)
        _check_installer_state(lock_path, *_provisional_roots(root, args.data_root, backend.service, mode))
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
                # Re-read after acquiring the same installer lock, before any
                # manager operation, diagnostic preparation or selection write.
                _check_installer_state(lock_path, *_provisional_roots(root, args.data_root, backend.service, mode))
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
                    backend = Backend(root, log_path, uv, process_control=process_control)
                    backend.console_diagnostics = True
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
                    backend = Backend(root, log_path, uv, process_control=process_control)
                    backend.console_diagnostics = True
                    prior = read_json(root / "install.json")
                if not recorded and prior.get("owner") == LABEL and prior.get("data_root"):
                    recorded = {"data_root": prior["data_root"]}
                backend.managed_by = managed_by
                # Preserve the path acquired above even if the process
                # environment changes before service registration or replay.
                backend.service.activation_lock_path = absolute(lock_path)
                data = absolute(recorded.get("data_root") or args.data_root or platform.data_root())
                protected = [root, data, absolute(str(data) + "-r1"), *_retained_r0_roots(backend.service)]
                _check_installer_state(lock_path, *protected)
                if not separate_roots(root, data):
                    raise ValueError("Keep program files and the workspace in separate directories.")
                if sys.platform == "win32" and len(str(root)) > 90:
                    raise ValueError("Choose a shorter program directory (under 90 characters) for Windows tools.")
                marker = read_json(root / "install.json")
                if marker and marker.get("owner") != LABEL:
                    raise ValueError("The program directory belongs to another installer.")
                if not marker and root.exists() and any(p.name != "bootstrap" for p in root.iterdir()):
                    raise ValueError("The program directory contains files Astrid does not own.")
                if mode == "uninstall" and managed_by != "pinokio":
                    # Recheck the authoritative selection while holding the lock.
                    refuse_handoff_uninstall(backend.service, root)
                # No installer diagnostic writes are allowed until selection is
                # resolved. Include the retained source of an interrupted stop:
                # recovery can precede validating the current data selection.
                log = _prepare_log(log_path, *protected)
                backend.console_diagnostics = log is None
                if log is not None:
                    backend.log = log
                existing_install = (
                    marker.get("owner") == LABEL
                    or (recorded.get("owner") == LABEL
                        and recorded.get("program_root") == str(root)
                        and not recorded.get("uninstalled"))
                )
                policy_change = None
                prompt_values = None
                if (mode in {"install", "update"} and not existing_install
                        and not args.yes and not args.choices and sys.stdin.isatty()):
                    prompt_values, declined = _prompt_choices()
                    if declined:
                        summary_choices = dict(RECOMMENDED_CHOICES)
                        _print_run_summary(summary_choices, mode=mode, declined=True,
                                           install_root=summary_root)
                        return 0
                matching_selection = (
                    recorded.get("owner") == LABEL
                    and recorded.get("program_root") == str(root)
                )
                resolved_choices = resolve_choices(
                    explicit=args.choices,
                    saved=recorded.get("choices") if matching_selection else None,
                    prompt=prompt_values,
                    accept_recommended=args.yes,
                    existing_install=existing_install,
                ) if mode in {"install", "update", "app-update"} else None
                if resolved_choices is not None:
                    previous_skill_policy = resolve_choices(
                        saved=recorded.get("choices") if matching_selection else None,
                        existing_install=existing_install,
                    )
                    backend.service.skill_policy_changed = any(
                        name in args.choices and resolved_choices[name] != previous_skill_policy[name]
                        for name in ("skills", "skill_copies")
                    )
                if mode in {"install", "update"} and unattended_consent_required(
                        existing_install=existing_install, accept_recommended=args.yes,
                        interactive=sys.stdin.isatty()):
                    print(render_plan(resolved_choices))
                    summary_choices = resolved_choices
                    _print_run_summary(
                        summary_choices, mode=mode,
                        result_note="setup not started; interactive consent is required.",
                        install_root=summary_root)
                    return 2
                if resolved_choices is not None:
                    backend.service.choices = resolved_choices
                    summary_choices = resolved_choices
                    policy_change = {name: value for name, value in args.choices.items()
                                     if value != previous_skill_policy[name]} or None
                if mode in {"install", "update", "rollback"} and managed_by != "pinokio":
                    from .boundary_handoff import handle

                    def candidate_manifest():
                        if not uv.is_absolute() or not uv.is_file():
                            raise ValueError("Run the install line to provide the pinned user-local uv.")
                        path = args.manifest or root / "install-manifest.json"
                        if not args.manifest:
                            download("https://astrid.haus/install-manifest.json", path)
                        return validate_manifest(read_json(path))

                    # R0's data stays read-only. This must precede legacy
                    # journal replay, Runtime preflight, staging and claims.
                    if handle(root, data, backend, manifest_loader=candidate_manifest,
                              rollback=mode == "rollback", policy_change=policy_change):
                        # A handled R0/R1 boundary never reaches the installer's
                        # consent application below. Saved choices stay in the
                        # selection the handoff carries over, and a requested
                        # change was refused inside handle(). Report that.
                        if mode in {"install", "update"}:
                            _print_run_summary(
                                summary_choices, mode=mode, install_root=summary_root,
                                result_note="R0 to R1 handoff handled; saved setup choices were kept and no command, autostart or skills change was made by it.")
                        return 0
                else:
                    from .boundary_registration import journal_path
                    from .r0_stop import refuse_unintegrated_r1_handoff

                    if os.path.lexists(journal_path(backend.service)):
                        raise ValueError("An R1 registration needs recovery. Run the install line again before this operation.")
                    if managed_by != "pinokio":
                        refuse_unintegrated_r1_handoff(root, backend.service)
                    elif mode == "rollback":
                        raise ValueError("Copy-only service rollback is available for direct installations.")
                pending_update = pending_transaction(root)
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
                    if mode in {"install", "update"}:
                        _print_run_summary(summary_choices, mode=mode)
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
                               "program_root": str(root), "data_root": str(data), "managed_by": "pinokio",
                               "choices": resolved_choices})
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
                summary_installer = installer
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
        if mode in {"install", "update"}:
            _print_run_summary(summary_choices, mode=mode,
                               outcomes=getattr(summary_installer, "outcomes", None),
                               install_root=summary_root)
        return 0
    except (Exception, KeyboardInterrupt) as failure:  # noqa: BLE001 - CLI boundary renders all install failures.
        try:
            if log is None:
                traceback.print_exc()
            else:
                with log.open("a", encoding="utf-8") as stream:
                    platform.owner_only(stream.fileno())
                    traceback.print_exc(file=stream)
        except OSError:
            log = None
            traceback.print_exc()
        print(FAILURE_MESSAGE)
        if isinstance(failure, (BackupSpaceError, ValueError, RuntimeOperationError)):
            print(str(failure))
            if isinstance(failure, RuntimeOperationError) and failure.details is not None:
                print(json.dumps(failure.details, ensure_ascii=False))
        elif isinstance(failure, VerificationError) and failure.safe_message:
            print(failure.safe_message)
        if log is not None:
            print(f"Details: {log}")
        if mode in {"install", "update"}:
            _print_run_summary(summary_choices, mode=mode,
                               outcomes=getattr(summary_installer, "outcomes", None),
                               failure=failure, install_root=summary_root)
        return 1
