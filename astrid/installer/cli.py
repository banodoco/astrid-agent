"""`astrid install`: the one install, repair, update and uninstall entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
from .installation import Installation, State
from .outcome import RECORD, Request, clear_record, journal_before_stop, receipt_warnings, settle, write_record
from .progress import HINT as PROGRESS_HINT
from .progress import Progress
from .service import (
    LABEL,
    default_program_root,
    default_uv,
    installer_lock_path,
    migrate_install_selection,
    pinokio_owned,
)
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


PUBLIC_INSTALL_LINE = "curl -fsSL https://astrid.haus/install | sh -s --"
PUBLIC_INSTALL_LINE_WINDOWS = "& ([scriptblock]::Create((irm https://astrid.haus/install.ps1)))"


def consent_rerun_command(choices, *, windows: bool) -> str:
    """The public install line with --yes and the user's own choice flags: the exact consent rerun."""
    flags = [f"{'--skill-copies' if name == 'skill_copies' else '--' + name} {'on' if choices[name] else 'off'}"
             for name in CHOICE_NAMES if name in choices]
    line = PUBLIC_INSTALL_LINE_WINDOWS if windows else PUBLIC_INSTALL_LINE
    return " ".join([line, "--yes", *flags])


def _safe(value) -> str:
    return str(value).encode("ascii", errors="backslashreplace").decode("ascii")


def _setup_summary(choices, *, mode, outcome=None, outcomes=None, failure=None,
                   install_root: Path | None = None) -> list[str]:
    """The full setup summary. It goes to install.log, never after the receipt in the terminal."""
    safe = _safe
    lines = ["Astrid setup summary:"]
    print = lines.append  # noqa: A001 - every summary line is collected, not printed
    if outcomes is None and failure is None and outcome is not None:
        pass  # This run never reached the installer's setup steps.
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
    return lines

# One terminal line for each choice the user changed from the recommended setup.
CHOICE_LINES = {
    ("autostart", False): "Your choice: start at sign-in is off. To turn it on, rerun the install line with --autostart on.",
    ("command", False): "Your choice: the astrid command is off. To turn it on, rerun the install line with --command on.",
    ("skills", False): "Your choice: Astrid skills are off. To turn them on, rerun the install line with --skills on.",
    ("skill_copies", True): "Your choice: skills are copies. To use links instead, rerun the install line with --skill-copies off.",
}


def changed_choice_lines(choices, *, install_root: Path | None = None) -> list[str]:
    """A line only for a choice the user changed; the recommended setup prints none."""
    selected = resolve_choices(prompt=choices, accept_recommended=choices is None)
    lines = []
    for name in CHOICE_NAMES:
        if selected[name] == RECOMMENDED_CHOICES[name]:
            continue
        lines.append(CHOICE_LINES.get((name, selected[name]),
                                      f"Your choice: {name.replace('_', ' ')} is "
                                      f"{'on' if selected[name] else 'off'}."))
        if name == "command" and install_root is not None:
            launcher = install_root / ("run-astrid.cmd" if sys.platform == "win32" else "run-astrid")
            if launcher.is_file():
                lines.append(f"Run Astrid with: {_safe(launcher)}")
    return lines


def _append_log(log: Path | None, lines: list[str]) -> bool:
    """Append ``lines`` to install.log; False when there is no usable log."""
    from runtime_protocol import platform

    if log is None:
        return False
    try:
        with Path(log).open("a", encoding="utf-8") as stream:
            platform.owner_only(stream.fileno())
            stream.write("\n".join(lines) + "\n")
        return True
    except OSError:
        return False


def _print_run_summary(choices, *, mode, outcome=None, outcomes=None, failure=None,
                       install_root: Path | None = None, log: Path | None = None) -> None:
    """Point to detail, never dump it: the receipt's outcome lines are the last lines printed.

    The setup summary (startup, command, skill paths, failure class, maintenance and
    the Way back line) goes to install.log. Without a usable log it is printed first,
    above the receipt, which then says the details are shown above in this window.
    """
    summary = _setup_summary(choices, mode=mode, outcome=outcome, outcomes=outcomes,
                             failure=failure, install_root=install_root)
    if not _append_log(log, summary):
        print("\n" + "\n".join(summary))
    changed = changed_choice_lines(choices, install_root=install_root)
    if changed:
        print("\n" + "\n".join(changed))
    if outcome is not None:
        print("\n" + "\n".join(outcome.headline()))


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


def normalise_completed(backend) -> bool:
    from .boundary_handoff import normalise_completed as normalise

    return normalise(backend)


def normalised_handoff(service, root: Path):
    from .installation import normalised_handoff as normalised

    return normalised(service, root)


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
    from .boundary_handoff import retained_roots as handoff_roots
    from .boundary_registration import retained_roots
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


class _Run:
    """One install/update/repair/recovery run, settled exactly once by its receipt."""

    def __init__(self, mode: str):
        self.mode = mode
        self.target: int | None = None
        self.target_source: str | None = None
        self.reason: str | None = None
        self.refused = False  # the run stopped before doing anything; never reported as reached
        self.before: State | None = None
        self.home: Path | None = None
        self.system: str | None = None
        self.root: Path | None = None
        self.choices: dict[str, bool] | None = None
        self.installer: Any | None = None
        self.transaction_id = None
        self.journal: Path | None = None  # the update journal, and what it held when the run began
        self.started_journal: dict | None = None
        self.operation_id: str | None = None
        self.started_at: int | None = None
        self.protected: list[Path] = []
        self.log: Path | None = None  # the installer log, once it is known to be outside every root

    def observe(self, home: Path, system: str, root: Path | None) -> None:
        import secrets
        import time

        self.home, self.system, self.root = home, system, root
        self.operation_id = self.operation_id or secrets.token_hex(16)
        self.started_at = self.started_at or int(time.time())
        try:
            resolved = Installation.resolve(home, system=system, root=root)
            self.before = resolved
            self.journal = (root or resolved.program_root) / "update-transaction.json"
            pending = read_json(self.journal)
            self.started_journal = pending
            self.transaction_id = pending.get("operation_id")
        except Exception:  # noqa: BLE001 - observation only; settle re-resolves
            self.before = None

    def manifest(self, manifest: dict) -> dict:
        from .core import install_release

        self.target = install_release(manifest)
        # A manifest that names no release leaves the target to the activated release (see activated()).
        self.target_source = "manifest" if "release" in manifest else "default"
        return manifest

    def activated(self, release: int | None) -> None:
        """The release this run activated; it is the target only when the manifest named none."""
        if release is not None and self.target_source != "manifest":
            self.target, self.target_source = release, "activated"

    def _rolled_back(self) -> bool:
        if self.root is None:
            return False
        pending = read_json(self.root / "update-transaction.json")
        return pending.get("step") == "rolled_back" and pending.get("operation_id") != self.transaction_id

    def finish(self, *, failure: BaseException | None = None) -> int:
        try:
            rolled_back = failure is not None and self._rolled_back()
        except (OSError, ValueError):
            rolled_back = False
        request_mode = "update" if self.mode in {"update", "app-update"} else self.mode
        journal = self.journal if self.journal is not None else (
            None if self.root is None else self.root / "update-transaction.json")
        before_stop = (failure is None or journal is None
                       or journal_before_stop(journal, self.started_journal))
        outcome = settle(Request(request_mode, self.target, self.reason, self.refused, self.target_source),
                         self.before,
                         lambda: (None if self.home is None else
                                  Installation.resolve(self.home, system=self.system, root=self.root)),
                         failure=failure, rolled_back=rolled_back,
                         warnings=receipt_warnings(getattr(self.installer, "outcomes", None)),
                         details=None if self.log is None else str(self.log), before_stop=before_stop)
        from dataclasses import replace

        # Every non-zero outcome ends with Next and Details (outcome.next_steps).
        outcome = replace(outcome, log=str(self.log) if self.log is not None else None)
        if self.log is not None:
            # Beside install.log, which diagnostics already read; never in a selected root.
            write_record(self.log.with_name(RECORD), outcome, *self.protected,
                         run={"operation_id": self.operation_id, "started_at": self.started_at})
        _print_run_summary(self.choices, mode=self.mode, outcome=outcome,
                           outcomes=getattr(self.installer, "outcomes", None), failure=failure,
                           install_root=self.root, log=self.log)
        return outcome.exit_code


def _speaking(backend, say):
    """Route the backend's printed lines through ``say``, so none lands inside an animated phase line."""
    for speaker in (backend, getattr(backend, "service", None)):
        if getattr(speaker, "say", None) is print:
            speaker.say = say
    return backend


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
    run = _Run(mode)
    settled = mode in {"install", "update", "app-update"}
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
    # Installer lines; the current phase line stays alive on an interactive terminal.
    say = Progress()
    os.environ.pop(PROGRESS_HINT, None)  # the bootstrap's hint is for this process alone
    if settled:
        # A run that fails before it can write its own receipt must not leave
        # the previous run's receipt standing as if it were this run's.
        try:
            clear_record(platform.log_dir() / RECORD)
        except (OSError, ValueError, KeyError):
            pass
    try:
        root = absolute(args.root or default_program_root(Path.home(), sys.platform))
        log_path = platform.log_dir() / "install.log"
        uv = args.uv or default_uv()
        process_control = ("sandbox" if args.sandbox else "strict") if mode in {"install", "update"} else None
        backend = _speaking(Backend(root, log_path, uv, process_control=process_control), say)
        backend.console_diagnostics = True
        if mode != "doctor":
            # Before any selection read, including the Pinokio update path.
            migrate_install_selection(backend.service.home, backend.service.system)
        if settled:
            run.observe(backend.service.home, backend.service.system, root if args.root else None)
            run.root = root
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
            run.protected, run.log = [root, absolute(marker["data_root"])], log
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
                run.choices = backend.service.choices
                if pending and pending.get("step") not in TERMINAL_STEPS:
                    recover_app(root, backend)
                    print(RECOVERED_PREVIOUS_VERSION)
                    run.reason = "recovered-previous-version"
                    return run.finish()
                # Recovery-only invocations return above; fresh operations preflight
                # once here before downloads or staging. C1 remains stop authority.
                if not uv.is_absolute() or not uv.is_file():
                    raise ValueError("Run the install line to provide the pinned user-local uv.")
                manifest_path = args.manifest or root / "install-manifest.json"
                if not args.manifest:
                    download("https://astrid.haus/install-manifest.json", manifest_path)
                manifest = run.manifest(validate_manifest(read_json(manifest_path)))
                outcomes = (update_app(root, backend, manifest) if args.update_app
                            else update_pinokio(root, backend, manifest, force=args.update))
                run.installer = SimpleNamespace(outcomes=outcomes)
                # With no release named by the manifest, the receipt targets the release this run activated.
                run.activated(outcomes.get("release"))
                return run.finish()
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
                    backend = _speaking(Backend(root, log_path, uv, process_control=process_control), say)
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
                    run.root = root
                    backend = _speaking(Backend(root, log_path, uv, process_control=process_control), say)
                    backend.console_diagnostics = True
                    prior = read_json(root / "install.json")
                if not recorded and prior.get("owner") == LABEL and prior.get("data_root"):
                    recorded = {"data_root": prior["data_root"]}
                backend.managed_by = managed_by
                if managed_by == "pinokio":
                    say.tracks = None  # Pinokio keeps its own record of this terminal: plain lines
                if mode in {"install", "update"} and managed_by != "pinokio" and normalise_completed(backend):
                    # The first run after a finished storage move makes R1 the selected
                    # data root once; from here this is an ordinary direct install.
                    recorded = backend.service.recorded()
                handed_off = (managed_by != "pinokio" and mode != "rollback"
                              and normalised_handoff(backend.service, root) is not None)
                if handed_off:
                    backend.service.adopt_handed_off_label()
                # Preserve the path acquired above even if the process
                # environment changes before service registration or replay.
                backend.service.activation_lock_path = absolute(lock_path)
                data = absolute(recorded.get("data_root") or args.data_root or platform.data_root())
                protected = [root, data, absolute(str(data) + "-r1"), *_retained_r0_roots(backend.service)]
                if recorded.get("retained_r0_data_root"):
                    protected.append(absolute(recorded["retained_r0_data_root"]))
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
                run.protected, run.log = protected, log
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
                        run.choices = dict(RECOMMENDED_CHOICES)
                        run.reason = "declined"
                        return run.finish()
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
                    print("Exact rerun command: " + consent_rerun_command(args.choices, windows=backend.service.system == "win32"))
                    run.choices = resolved_choices
                    run.reason = "consent-required"
                    return run.finish()
                if resolved_choices is not None:
                    backend.service.choices = resolved_choices
                    run.choices = resolved_choices
                    policy_change = {name: value for name, value in args.choices.items()
                                     if value != previous_skill_policy[name]} or None
                if mode in {"install", "update", "rollback"} and managed_by != "pinokio" and not handed_off:
                    from .boundary_handoff import handle

                    def candidate_manifest():
                        if not uv.is_absolute() or not uv.is_file():
                            raise ValueError("Run the install line to provide the pinned user-local uv.")
                        path = args.manifest or root / "install-manifest.json"
                        if not args.manifest:
                            download("https://astrid.haus/install-manifest.json", path)
                        return run.manifest(validate_manifest(read_json(path)))

                    # R0's data stays read-only. This must precede legacy
                    # journal replay, Runtime preflight, staging and claims.
                    if handle(root, data, backend, manifest_loader=candidate_manifest,
                              rollback=mode == "rollback", policy_change=policy_change):
                        # This run performed the copy-only handoff (normalised at its
                        # end) or recovered one. The receipt reports what is live now.
                        if mode == "rollback":
                            return 0
                        if run.target is None:
                            run.reason = "recovered-previous-version"
                        return run.finish()
                elif not handed_off:
                    from .boundary_registration import journal_path
                    from .r0_stop import refuse_unintegrated_r1_handoff

                    if (os.path.lexists(journal_path(backend.service))
                            and normalised_handoff(backend.service, root) is None):
                        raise ValueError("An R1 registration needs recovery. Run the install line again before this operation.")
                    if managed_by != "pinokio":
                        refuse_unintegrated_r1_handoff(root, backend.service)
                    elif mode == "rollback":
                        raise ValueError("Copy-only service rollback is available for direct installations.")
                if handed_off and mode in {"install", "update"}:
                    from .installation import OtherRuntimeRunning, retire_r0_leftover

                    try:
                        # Before any stop: a known R0-era leftover is stopped through
                        # R0's own label (files kept); anything else is refused.
                        if retire_r0_leftover(backend.service, root):
                            print("Stopped the earlier Astrid copy that was running again; its files were kept.")
                    except OtherRuntimeRunning as refusal:
                        print(str(refusal))
                        run.reason, run.refused = "other-runtime-running", True
                        return run.finish()
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
                    run.reason = "recovered-previous-version"
                    return run.finish()
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
                    if settled:
                        run.manifest(manifest)
                bundle = None
                if mode == "doctor":
                    bundle = default_doctor_bundle() if args.doctor == "__default__" else Path(args.doctor)
                options = {"managed_by": managed_by} if managed_by else {}
                installer = Installer(root, data, backend, say=say, **options)
                run.installer = installer
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
        if settled:
            return run.finish()
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
            run.log = None  # the log became unusable: Details points at this window instead
            traceback.print_exc()
        print(FAILURE_MESSAGE)
        if isinstance(failure, (BackupSpaceError, ValueError, RuntimeOperationError)):
            print(str(failure))
            if isinstance(failure, RuntimeOperationError) and failure.details is not None:
                # Detail goes to install.log; the Details line points there.
                details = json.dumps(failure.details, ensure_ascii=False)
                if not _append_log(log, ["Failure details: " + details]):
                    print(details)
        elif isinstance(failure, VerificationError) and failure.safe_message:
            print(failure.safe_message)
        if log is not None and not settled:
            print(f"Details: {log}")  # settled runs name it in the receipt's closing lines
        if settled:
            return run.finish(failure=failure)
        return 1
