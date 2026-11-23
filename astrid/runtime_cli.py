"""Thin, identity-fenced facade over the installed workspace Runtime CLI.

Astrid deliberately owns no workspace catalog or process supervisor.  This
module resolves Astrid's stable support root, invokes ``astrid-runtime`` with
that root explicitly, and checks that lifecycle results still describe the
one Runtime-selected workspace.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from astrid.sdk.storage_root import ensure_no_unmigrated_runtime, resolve_runtime_data_root

RUNTIME_COMMAND_ENV = "ASTRID_RUNTIME_CLI"
PROFILE = "astrid"
OBSERVE_EFFECTS = ("observe",)
_STARTUP_PHASES = {
    "verifying_identity", "checking_realm", "checking_objects", "checking_history", "establishing_service",
}
_STARTUP_SCOPE_REQUIRED = "Runtime startup operations require the selected installed Runtime environment."


class RuntimeCLIError(RuntimeError):
    """A typed failure at the installed Runtime command boundary."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "runtime_unavailable",
        result: Mapping[str, Any] | None = None,
        returncode: int = 1,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.result = dict(result or {})
        self.returncode = returncode


@dataclass(frozen=True)
class RuntimeResult:
    argv: tuple[str, ...]
    returncode: int
    data: Mapping[str, Any]
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.data.get("ok", True) is not False


def _absolute_root(value: str | Path, *, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"{label} contains a symlink component: {path}")
    return path


def _runtime_command(env: Mapping[str, str] | None = None, *, windows: bool | None = None) -> tuple[str, ...]:
    target = os.environ if env is None else env
    configured = target.get(RUNTIME_COMMAND_ENV, "").strip()
    if configured:
        try:
            structured = json.loads(configured)
        except json.JSONDecodeError:
            structured = None
        if isinstance(structured, list) and all(isinstance(item, (str, int, float)) for item in structured):
            command = tuple(str(item) for item in structured)
        elif (os.name == "nt" if windows is None else windows):
            # A configured Windows executable path is commonly a single value
            # containing spaces. Structured JSON argv handles arguments; the
            # legacy string form is treated as one executable on Windows.
            command = (configured,)
        else:
            command = tuple(shlex.split(configured))
        if not command:
            raise RuntimeCLIError(f"{RUNTIME_COMMAND_ENV} is empty")
        return command
    installed = shutil.which("astrid-runtime")
    if installed:
        return (installed,)
    # This remains an installed-package invocation; it does not import or
    # duplicate Runtime implementation inside Astrid.
    return (sys.executable, "-m", "astrid_runtime")


def _json_payload(stdout: str, stderr: str) -> Mapping[str, Any]:
    for text in (stdout, stderr):
        text = text.strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            return dict(value)
    return {
        "ok": False,
        "problem_code": "runtime_unavailable",
        "error": (stderr or stdout or "Runtime returned no JSON result").strip(),
    }


class RuntimeCLI:
    """Invoke, but never reimplement, the installed Runtime command surface."""

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        runner: Any = None,
    ) -> None:
        self.command = tuple(command or _runtime_command())
        self._runner = runner

    def invoke(self, args: Sequence[str], *, timeout: float | None = 120.0) -> RuntimeResult:
        argv = (*self.command, *map(str, args))
        try:
            if timeout is None and self._runner is None:
                # Stay attached to the exact Runtime child. Its lifecycle
                # authority includes startup timing and cleanup.
                with subprocess.Popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
                    stdout, stderr = child.communicate()
                    completed = subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)
            else:
                runner = subprocess.run if self._runner is None else self._runner
                completed = runner(list(argv), capture_output=True, text=True, check=False, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            code = "observation_timeout" if timeout is not None and timeout <= 5.0 else "runtime_unavailable"
            message = "the Astrid Runtime CLI timed out"
            if timeout is not None:
                message += f" after {timeout:.3f}s"
            raise RuntimeCLIError(
                message,
                code=code,
                result={"ok": False, "problem_code": code},
                returncode=124,
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeCLIError(f"could not run the Astrid Runtime CLI: {exc}") from exc
        data = _json_payload(completed.stdout, completed.stderr)
        return RuntimeResult(argv, int(completed.returncode), data, completed.stderr.strip())

    def inspect(
        self,
        *,
        support_root: str | Path,
        realm_root: str | Path | None = None,
        expected_realm_id: str | None = None,
        timeout: float = 120.0,
    ) -> RuntimeResult:
        support = _absolute_root(support_root, label="support root")
        args = ["workspace", "inspect", "--data-root", str(support)]
        if realm_root is not None:
            args.extend(("--realm-root", str(_absolute_root(realm_root, label="realm root"))))
        if expected_realm_id:
            args.extend(("--expected-realm-id", expected_realm_id))
        args.append("--json")
        return self.invoke(args, timeout=timeout)

    def _startup_status(
        self, support: Path, *, realm: Path | None = None, realm_id: str | None = None,
    ) -> tuple[bool, RuntimeResult | None]:
        args = ["startup-status", "--data-root", str(support)]
        if realm is not None and realm_id is not None:
            args.extend(("--realm-root", str(realm), "--expected-realm-id", realm_id))
        result = self.invoke((*args, "--json"), timeout=5.0)
        if result.returncode == 2 and "invalid choice: 'startup-status' (choose from " in result.stderr:
            return False, None
        if result.returncode == 1 and result.data == {"state": "unavailable"}:
            return True, None

        data = result.data
        state = data.get("state")
        operation = data.get("operation_id")
        attempt = data.get("attempt_id")
        count = data.get("objects_checked")
        phase = data.get("phase")
        if (result.returncode == 0 and data.keys() == {"operation_id", "phase", "state", "objects_checked"}
                and isinstance(operation, str) and re.fullmatch(r"[a-f0-9]{32}", operation)
                and isinstance(phase, str) and phase in _STARTUP_PHASES
                and type(count) is int and 0 <= count < 2**63 and state in ("pending", "ended")):
            if state == "ended":
                return True, None
            key = "startup_operation"
        elif (result.returncode == 0 and data.keys() == {"attempt_id", "state"}
              and isinstance(attempt, str) and re.fullmatch(r"[a-f0-9]{32}", attempt)
              and state in ("inspecting_workspace", "starting_service", "failed")):
            if state == "failed":
                return True, None
            key = "startup_attempt"
        else:
            raise RuntimeCLIError(
                str(data.get("error") or "Runtime startup status could not be verified"), result=data,
            )
        return True, RuntimeResult(result.argv, 1, {
            "ok": False,
            "state": "startup_pending",
            "problem_code": "startup_pending",
            key: data,
            "next_action": "observe with astrid-runtime startup-status using the same --data-root",
        }, result.stderr)

    def _inspect_for_up(
        self, support: Path, *, realm: Path | None = None, realm_id: str | None = None,
    ) -> tuple[RuntimeResult, bool]:
        coordinated, pending = self._startup_status(support, realm=realm, realm_id=realm_id)
        if pending is not None:
            return pending, True
        if not coordinated:
            return self.inspect(support_root=support), False

        selected = self.invoke(
            ("workspace", "inspect", "--data-root", str(support), "--startup-operation", "--json"),
            timeout=None,
        )
        if selected.returncode == 1 and selected.data == {"ok": False, "error": _STARTUP_SCOPE_REQUIRED}:
            return self.inspect(support_root=support), False
        if not selected.ok and selected.data.get("state") in ("startup_pending", "starting_service"):
            # The owner can appear after the first status read. Reconfirm
            # against the requested selection before making a pending receipt.
            _, pending = self._startup_status(support, realm=realm, realm_id=realm_id)
            if pending is not None:
                return pending, True
        return selected, True

    def configure(
        self,
        operation: str,
        *,
        support_root: str | Path,
        realm_root: str | Path,
        realm_id: str,
        display_name: str,
        source_manifest: str | Path | None = None,
    ) -> RuntimeResult:
        if operation not in {"create", "attach"}:
            raise ValueError("workspace operation must be create or attach")
        support = _absolute_root(support_root, label="support root")
        realm = _absolute_root(realm_root, label="realm root")
        args = [
            "workspace", operation,
            "--data-root", str(support),
            "--realm-root", str(realm),
            "--realm-id", realm_id,
            "--display-name", display_name,
        ]
        if source_manifest is not None:
            args.extend(("--source-manifest", str(_absolute_root(source_manifest, label="source manifest"))))
        args.append("--json")
        return self.invoke(args)

    def up(self, *, support_root: str | Path, expected_realm_id: str, realm_root: str | Path) -> RuntimeResult:
        support = _absolute_root(support_root, label="support root")
        realm = _absolute_root(realm_root, label="realm root")
        selected, coordinated = self._inspect_for_up(support, realm=realm, realm_id=expected_realm_id)
        if not selected.ok:
            if coordinated and (selected.data.get("problem_code") == "startup_pending"
                                or selected.data.get("state") in ("startup_busy", "startup_attempt_failed")):
                return selected
            raise RuntimeCLIError(
                str(selected.data.get("error") or "selected workspace is unavailable"),
                code=str(selected.data.get("problem_code") or "workspace_missing"),
                result=selected.data,
                returncode=selected.returncode,
            )
        validate_selected_workspace(
            selected.data,
            expected_realm_id=expected_realm_id,
            expected_realm_root=realm,
            expected_support_root=support,
        )
        command = ("up", "--profile", PROFILE, "--data-root", str(support), "--json")
        if coordinated:
            still_coordinated, pending = self._startup_status(support, realm=realm, realm_id=expected_realm_id)
            if not still_coordinated:
                raise RuntimeCLIError("Runtime startup status support changed after workspace inspection")
            if pending is not None:
                return pending
            result = self.invoke(command, timeout=None)
        else:
            result = self.invoke(command)
        if result.ok:
            validate_started_workspace(
                result.data,
                expected_realm_id=expected_realm_id,
                expected_realm_root=realm,
                expected_support_root=support,
            )
        return result

    def observe(self, command: str, *, support_root: str | Path, timeout: float = 5.0) -> RuntimeResult:
        if command not in {"status", "doctor"}:
            raise ValueError("observer command must be status or doctor")
        support = _absolute_root(support_root, label="support root")
        return self.invoke((command, "--data-root", str(support), "--json"), timeout=timeout)


def _field(data: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = data.get(name)
        if value not in (None, ""):
            return value
    workspace = data.get("workspace")
    if isinstance(workspace, Mapping):
        for name in names:
            value = workspace.get(name)
            if value not in (None, ""):
                return value
    return None


def validate_selected_workspace(
    data: Mapping[str, Any],
    *,
    expected_realm_id: str,
    expected_realm_root: str | Path,
    expected_support_root: str | Path,
) -> None:
    """Fail closed unless a Runtime result names one exact UUID/root pair."""

    realm_id = _field(data, "realm_id", "workspace_id", "selected_realm_id")
    realm_root = _field(data, "realm_root", "data_root")
    support_root = _field(data, "support_root")
    expected_realm = _absolute_root(expected_realm_root, label="realm root")
    expected_support = _absolute_root(expected_support_root, label="support root")
    if str(realm_id or "") != expected_realm_id:
        raise RuntimeCLIError(
            f"Runtime selected workspace {realm_id!r}, expected {expected_realm_id!r}",
            code="workspace_identity_mismatch",
            result=data,
        )
    if realm_root is None or _absolute_root(str(realm_root), label="selected realm root") != expected_realm:
        raise RuntimeCLIError(
            f"Runtime selected a different realm root; expected {expected_realm}",
            code="workspace_identity_mismatch",
            result=data,
        )
    if support_root is not None and _absolute_root(str(support_root), label="selected support root") != expected_support:
        raise RuntimeCLIError(
            f"Runtime selected a different support root; expected {expected_support}",
            code="workspace_identity_mismatch",
            result=data,
        )


def validate_started_workspace(
    data: Mapping[str, Any],
    *,
    expected_realm_id: str,
    expected_realm_root: str | Path,
    expected_support_root: str | Path,
) -> None:
    """Validate an ``up`` result while allowing its root to be omitted.

    The pre-start ``workspace inspect`` result is the authority for the exact
    realm-root pair.  ``astrid-runtime up --json`` may return only the realm
    UUID, so an omitted root is accepted only when the UUID matches and any
    optional discovery/credential paths remain beneath the already-validated
    support root.
    """

    if _field(data, "realm_root", "data_root") is not None:
        validate_selected_workspace(
            data,
            expected_realm_id=expected_realm_id,
            expected_realm_root=expected_realm_root,
            expected_support_root=expected_support_root,
        )
        return

    realm_id = _field(data, "realm_id", "workspace_id", "selected_realm_id")
    if str(realm_id or "") != expected_realm_id:
        raise RuntimeCLIError(
            f"Runtime selected workspace {realm_id!r}, expected {expected_realm_id!r}",
            code="workspace_identity_mismatch",
            result=data,
        )
    support = _absolute_root(expected_support_root, label="support root")
    reported_support = _field(data, "support_root")
    if reported_support is not None and _absolute_root(str(reported_support), label="selected support root") != support:
        raise RuntimeCLIError(
            f"Runtime selected a different support root; expected {support}",
            code="workspace_identity_mismatch",
            result=data,
        )
    for key in ("discovery_path", "credential_file"):
        value = data.get(key)
        if value in (None, ""):
            continue
        if not _absolute_root(str(value), label=f"Runtime {key}").is_relative_to(support):
            raise RuntimeCLIError(
                f"Runtime {key} is outside the selected support root {support}",
                code="workspace_identity_mismatch",
                result=data,
            )


def _wrapper_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="astrid-runtime",
        description="Selected-workspace facade over astrid-runtime (Runtime remains lifecycle authority).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("up", "status", "doctor", "restart"):
        command = sub.add_parser(name)
        command.add_argument("--profile", choices=(PROFILE,), default=PROFILE)
        command.add_argument("--data-root", type=Path)
        command.add_argument("--json", action="store_true")
    backup = sub.add_parser("backup")
    backup.add_argument("--data-root", type=Path)
    backup.add_argument("--destination", required=True, type=Path)
    backup.add_argument("--json", action="store_true")
    restore = sub.add_parser("restore")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--data-root", type=Path)
    restore.add_argument("--destination", required=True, type=Path)
    restore.add_argument("--json", action="store_true")
    relocate = sub.add_parser("relocate")
    relocate.add_argument("--data-root", type=Path)
    relocate.add_argument("--destination", required=True, type=Path)
    relocate.add_argument("--backup", type=Path)
    relocate.add_argument("--plan", action="store_true")
    relocate.add_argument("--confirm")
    relocate.add_argument("--json", action="store_true")
    recovery = sub.add_parser("recovery")
    recovery.add_argument("--data-root", type=Path)
    recovery.add_argument("--expected-realm-id", required=True)
    recovery.add_argument("--expected-version", required=True, type=int)
    recovery.add_argument("--confirm")
    recovery.add_argument("--non-interactive", action="store_true")
    recovery.add_argument("--json", action="store_true")
    return parser


def _emit(data: Mapping[str, Any], *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(dict(data), indent=2, sort_keys=True, default=str))
        return
    for key in sorted(data):
        value = data[key]
        if isinstance(value, (dict, list)):
            value = json.dumps(value, sort_keys=True)
        print(f"{key}: {value}")


def main(argv: list[str] | None = None) -> int:
    args = _wrapper_parser().parse_args(argv)
    try:
        support = _absolute_root(args.data_root or resolve_runtime_data_root(), label="support root")
        ensure_no_unmigrated_runtime(support)
        runtime = RuntimeCLI()
        if args.command in {"status", "doctor"}:
            result = runtime.observe(args.command, support_root=support)
        elif args.command in {"up", "restart"}:
            if args.command == "up":
                selected, _ = runtime._inspect_for_up(support)
            else:
                selected = runtime.inspect(support_root=support)
            if not selected.ok:
                result = selected
            else:
                realm_id = str(_field(selected.data, "realm_id", "workspace_id", "selected_realm_id") or "")
                realm_root = _field(selected.data, "realm_root", "data_root")
                if not realm_id or realm_root is None:
                    raise RuntimeCLIError("Runtime workspace inspection did not return one selected UUID/root", code="workspace_ambiguous")
                validate_selected_workspace(
                    selected.data,
                    expected_realm_id=realm_id,
                    expected_realm_root=str(realm_root),
                    expected_support_root=support,
                )
                if args.command == "up":
                    result = runtime.up(support_root=support, expected_realm_id=realm_id, realm_root=str(realm_root))
                else:
                    result = runtime.invoke(("restart", "--profile", PROFILE, "--data-root", str(support), "--json"))
        else:
            forwarded = [args.command, "--data-root", str(support)]
            if args.command == "backup":
                forwarded.extend(("--destination", str(_absolute_root(args.destination, label="backup destination"))))
            elif args.command == "restore":
                forwarded.extend((str(_absolute_root(args.backup, label="backup path")), "--destination", str(_absolute_root(args.destination, label="restore destination"))))
            elif args.command == "relocate":
                forwarded.extend(("--profile", PROFILE, "--destination", str(_absolute_root(args.destination, label="relocation destination"))))
                if args.backup:
                    forwarded.extend(("--backup", str(_absolute_root(args.backup, label="backup destination"))))
                if args.plan:
                    forwarded.append("--plan")
                if args.confirm:
                    forwarded.extend(("--confirm", args.confirm))
            elif args.command == "recovery":
                forwarded.extend(("--expected-realm-id", args.expected_realm_id, "--expected-version", str(args.expected_version)))
                if bool(args.confirm) == bool(args.non_interactive):
                    raise ValueError("recovery requires exactly one of --confirm or --non-interactive")
                if args.confirm:
                    forwarded.extend(("--confirm", args.confirm))
                else:
                    forwarded.append("--non-interactive")
            forwarded.append("--json")
            result = runtime.invoke(forwarded)
        _emit(result.data, json_mode=args.json)
        return result.returncode
    except (RuntimeCLIError, ValueError) as exc:
        payload = {
            "ok": False,
            "problem_code": getattr(exc, "code", "runtime_unavailable"),
            "error": str(exc),
            "next_action": "run the Astrid install line again",
        }
        _emit(payload, json_mode=getattr(args, "json", False))
        return 1


__all__ = [
    "OBSERVE_EFFECTS",
    "PROFILE",
    "RUNTIME_COMMAND_ENV",
    "RuntimeCLI",
    "RuntimeCLIError",
    "RuntimeResult",
    "main",
    "validate_selected_workspace",
    "validate_started_workspace",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
