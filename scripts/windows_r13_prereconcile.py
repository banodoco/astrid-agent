"""Diagnostic-only R13 Task Scheduler pre-reconcile for Windows journey rows.

This is a NON-PROOF bypass: it conditions an owned R13 task before the real
candidate installer starts. It does not replace or edit the public installer.
"""
from __future__ import annotations

import hashlib
import argparse
import json
import ntpath
import os
from pathlib import Path
import subprocess
import sys
import tempfile

PINNED_AGENT_SHA = "d33995c1c357bb762bfe64e99ebe81403222a72a"
FAILURE_STAGE = "candidate_origin"
TASK_QUERY = r"""
$ErrorActionPreference='Stop'
$t=Get-ScheduledTask -TaskName 'Astrid Runtime' -TaskPath '\' -ErrorAction Stop
@{task_path=[string]$t.TaskPath; task_name=[string]$t.TaskName;
  actions=@($t.Actions | ForEach-Object {@{Execute=[string]$_.Execute;Arguments=[string]$_.Arguments}})} |
  ConvertTo-Json -Compress -Depth 5
""".strip()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _preflight_error(stage: str) -> RuntimeError:
    error = RuntimeError("R13 prereconcile preflight failed")
    error.safe_diagnostic = {"failure_stage": stage, "failure_code": stage.upper(), "failure_category": "RuntimeError"}
    return error


def _ps_file(script: str, *, shell: str, timeout: int = 30) -> tuple[str, dict]:
    """Run one script via a private temp file and return stdout plus safe evidence."""
    # Windows PowerShell 5.1 requires a BOM to reliably read a UTF-8 script;
    # explicitly select UTF-8 for captured native stdout as well.
    payload = b"\xef\xbb\xbf" + ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); " + script).encode("utf-8")
    info = {"script_bytes": len(payload), "script_sha256": _sha256(payload),
            "transport": "powershell-file", "exit_code": None, "stdout_bytes": None,
            "stdout_sha256": None, "exception_class": None}
    # Windows' user-scoped TEMP is private by default; the file is removed even
    # when CreateProcess or PowerShell fails. No script content is retained.
    temp_root = Path(tempfile.gettempdir()).resolve()
    fd, name = tempfile.mkstemp(prefix="astrid-r13-prereconcile-", suffix=".ps1", dir=temp_root)
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        completed = subprocess.run(
            [str(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(path)],
            capture_output=True, timeout=timeout, check=False)
        output = completed.stdout
        info.update(exit_code=completed.returncode, stdout_bytes=len(output), stdout_sha256=_sha256(output))
        if completed.returncode:
            raise RuntimeError("PowerShell diagnostic operation failed")
        return output.decode("utf-8-sig", errors="strict").strip(), info
    except BaseException as exc:
        info["exception_class"] = type(exc).__name__
        try:
            exc.safe_evidence = info
        except Exception:
            pass
        raise
    finally:
        path.unlink(missing_ok=True)


def _task_document(raw: str, root: Path, data: Path) -> list[dict]:
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise RuntimeError("The Task Scheduler query returned malformed data")
    actions = document.get("actions")
    if (document.get("task_name") != "Astrid Runtime" or document.get("task_path") != "\\"
            or not isinstance(actions, list) or len(actions) != 1
            or not isinstance(actions[0], dict)):
        raise RuntimeError("The owned R13 Task Scheduler task is missing or ambiguous")
    action = actions[0]
    execute = action.get("Execute")
    arguments = action.get("Arguments")
    root_norm = ntpath.normcase(ntpath.normpath(str(root.resolve())))
    execute_norm = ntpath.normcase(ntpath.normpath(execute)) if isinstance(execute, str) else ""
    try:
        executable_owned = ntpath.commonpath((root_norm, execute_norm)) == root_norm
    except ValueError:
        executable_owned = False
    if (not executable_owned
            or not isinstance(arguments, str)):
        raise RuntimeError("The existing Task Scheduler action is not owned by this R13 run")
    return actions


def _child(args) -> dict:
    """Run product selection and Scheduler preconditioning inside the installed R13 Python."""
    global FAILURE_STAGE
    FAILURE_STAGE = "candidate_origin"
    checkout = Path(args.agent).resolve(strict=True)
    rows = {"direct-windows-r13-update", "direct-windows-r13-killed-update"}
    root, data, marker = Path(args.root), Path(args.data), Path(args.marker)
    if (sys.platform != "win32" or os.environ.get("GITHUB_ACTIONS", "").casefold() != "true"
            or not os.environ.get("ImageOS") or os.environ.get("ASTRID_CI_SANDBOX_PROFILE") == "1"
            or os.environ.get("ASTRID_CI_EXPECTED_RUNNER") != "windows-2022"
            or os.environ.get("GITHUB_REF") != "refs/heads/release/14/fix17-diag"
            or os.environ.get("ASTRID_FIX17_DIAGNOSTIC") != "1" or args.row not in rows
            or Path(os.environ.get("RUNNER_TEMP", "")).resolve() / "f17" != root.parent.resolve()
            or root.name != "program" or data.name != "data" or data.parent.resolve() != root.parent.resolve()
            or root.is_symlink() or data.is_symlink() or not root.is_dir() or not data.is_dir()):
        raise RuntimeError("Hosted Windows 2022 f17 diagnostic scope mismatch")
    FAILURE_STAGE = "install_selection"
    current = (root / "current").read_text(encoding="utf-8").strip()
    if not current or Path(current).name != current or current in {".", ".."}:
        raise RuntimeError("Current version pointer is not a basename")
    selected_path = root / "versions" / current / "version.json"
    selected_document = json.loads(selected_path.read_text(encoding="utf-8"))
    if _sha256(selected_path.read_bytes()) != args.version_sha256:
        raise RuntimeError("Current selected R13 version mismatch")
    git_head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                              capture_output=True, check=True).stdout.decode().strip()
    if git_head != PINNED_AGENT_SHA:
        raise RuntimeError("Candidate Agent pin mismatch")
    sys.path.insert(0, str(checkout))
    from astrid.installer import service as module
    if (not Path(module.__file__).resolve().is_relative_to(checkout)
            or not Path(module.__file__).name == "service.py"):
        raise RuntimeError("Candidate Service origin mismatch")
    version = module.Version.load(selected_document)
    if version.release != args.release or args.release != 13 or selected_document.get("identity", {}).get("release") != 13:
        raise RuntimeError("Selected release mismatch")
    if Path(sys.executable).resolve() != Path(selected_document["python"]).resolve():
        raise RuntimeError("Selected Python does not match sys.executable")
    if marker.is_symlink() or not marker.resolve().is_relative_to(data.resolve()):
        raise RuntimeError("Seed marker scope mismatch")
    marker_hash = _sha256(marker.read_bytes())
    if marker_hash != args.digest:
        raise RuntimeError("Seed marker hash mismatch")
    service = module.Service(root, module.platform.log_dir() / "install.log", lambda _argv: "",
                             home=Path.home(), system="win32")
    service.activation_lock_path = module.absolute(module.installer_lock_path(service.home, "win32"))
    selection = service.recorded()
    if (selection.get("owner") != module.LABEL or selection.get("program_root") != str(root)
            or selection.get("data_root") != str(data) or service.autostart_enabled() is not True):
        raise RuntimeError("Selection owner or autostart mismatch")
    settings = service.settings()
    if not service.env_file.is_file() or not service.path.is_file():
        raise RuntimeError("Service prerequisites missing")
    calls = []
    def powershell(script):
        output, info = _ps_file(script, shell=args.shell)
        calls.append(info)
        return output
    service.powershell = powershell
    FAILURE_STAGE = "task_query"
    old_actions = _task_document(powershell(TASK_QUERY), root, data)
    FAILURE_STAGE = "reconcile_write"
    service._reconcile_launch_action(version, data)
    FAILURE_STAGE = "readback"
    after_actions = _task_document(powershell(TASK_QUERY), root, data)
    argv = module.command(version, data, windows=True, log=service.log, env_file=service.env_file,
                          expected_lock=service.activation_lock_path, home=service.home)
    if after_actions != [{"Execute": argv[0], "Arguments": subprocess.list2cmdline(argv[1:])}]:
        raise RuntimeError("Canonical R13 task action mismatch")
    if (marker.is_symlink() or not marker.resolve().is_relative_to(data.resolve())
            or (root / "current").read_text(encoding="utf-8").strip() != current
            or _sha256(selected_path.read_bytes()) != args.version_sha256
            or marker.is_symlink() or not marker.is_file() or _sha256(marker.read_bytes()) != args.digest):
        raise RuntimeError("R13 current selection or marker changed during prereconcile")
    return {"status": "pass", "mark": "NON-PROOF DIAGNOSTIC BYPASS", "candidate_agent_sha": git_head,
            "row": args.row, "selection_owner_checked": True, "autostart_enabled": True,
            "old_task_present_owned": True, "service_settings_keys": sorted(settings),
            "old_task_action_count": len(old_actions), "canonical_action_matches": True,
            "activation_lock_path_sha256": _sha256(str(service.activation_lock_path).encode()),
            "seed_marker_sha256": marker_hash, "powershell_substeps": calls}


def prereconcile(*, journey, job_id: str, shell: str, agent_checkout: Path, root: Path, data: Path,
                 version_document: dict, marker: Path, marker_sha256: str) -> dict:
    """Bracket an isolated installed-Python child with the journey's current-selection checks."""
    rows = {"direct-windows-r13-update", "direct-windows-r13-killed-update"}
    if (os.environ.get("ASTRID_FIX17_DIAGNOSTIC") != "1" or job_id not in rows
            or os.environ.get("GITHUB_REF") != "refs/heads/release/14/fix17-diag"
            or Path(os.environ.get("RUNNER_TEMP", "")).resolve() / "f17" != root.parent.resolve()
            or sys.platform != "win32" or os.environ.get("ImageOS") is None
            or os.environ.get("ASTRID_CI_EXPECTED_RUNNER") != "windows-2022"
            or not journey.hosted_windows_runner()):
        raise _preflight_error("hosted_scope_guard")
    if (root.is_symlink() or data.is_symlink() or not root.is_dir() or not data.is_dir()
            or root.name != "program" or data.name != "data" or data.parent.resolve() != root.parent.resolve()):
        raise _preflight_error("isolated_root_guard")
    before = journey.installation(root, data)
    if (before[1] != data or before[2] != version_document
            or version_document.get("identity", {}).get("release") != 13
            or before[0].resolve() != (root / "versions" / before[0].parent.name / "version.json").resolve()):
        raise _preflight_error("selected_version_precheck")
    if marker.is_symlink() or not marker.resolve().is_relative_to(data.resolve()):
        raise _preflight_error("seed_marker_scope")
    if _sha256(marker.read_bytes()) != marker_sha256:
        raise _preflight_error("seed_marker_hash")
    version_path = before[0]
    version_sha256 = _sha256(version_path.read_bytes())
    child_argv = [str(version_document["python"]), "-I", str(Path(__file__).resolve()), "--child",
                  "--agent", str(agent_checkout), "--root", str(root), "--data", str(data),
                  "--marker", str(marker), "--digest", marker_sha256, "--release", "13",
                  "--row", job_id, "--shell", str(shell), "--version-sha256", version_sha256]
    completed = subprocess.run(child_argv, capture_output=True, timeout=120, check=False)
    try:
        receipt = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, ValueError):
        receipt = None
    if completed.returncode or not isinstance(receipt, dict) or receipt.get("status") != "pass":
        child_failure = receipt if isinstance(receipt, dict) else {}
        raise RuntimeError("R13 task prereconcile child failed") from _DiagnosticCause({
            "failure_stage": child_failure.get("failure_stage", "reconcile_write_or_readback"),
            "failure_code": child_failure.get("failure_code", "CHILD_FAILED"),
            "failure_category": "ChildProcessError", "child_exit_code": completed.returncode,
            "child_stdout_bytes": len(completed.stdout), "child_stdout_sha256": _sha256(completed.stdout),
            "child_stderr_bytes": len(getattr(completed, "stderr", b"")),
            "child_stderr_sha256": _sha256(getattr(completed, "stderr", b""))})
    after = journey.installation(root, data)
    if (after != before or marker.is_symlink() or not marker.is_file()
            or _sha256(marker.read_bytes()) != marker_sha256):
        raise _preflight_error("postcondition_selection_or_marker")
    receipt.update(current_unchanged=True, data_unchanged=True)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    for name in ("agent", "root", "data", "marker", "digest", "row", "shell", "version-sha256"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--release", type=int, required=True)
    args = parser.parse_args()
    stage = "candidate_origin"
    try:
        stage = "candidate_origin"
        result = _child(args)
    except BaseException as exc:
        result = {"status": "fail", "failure_category": type(exc).__name__,
                  "failure_stage": FAILURE_STAGE, "failure_code": FAILURE_STAGE.upper() + "_FAILED"}
        safe_evidence = getattr(exc, "safe_evidence", None)
        if isinstance(safe_evidence, dict):
            result["transport"] = safe_evidence
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())


class _DiagnosticCause(Exception):
    """Carry safe diagnostic fields without persisting an exception message."""

    def __init__(self, safe: dict):
        super().__init__("diagnostic metadata")
        self.safe_diagnostic = safe
