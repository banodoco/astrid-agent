"""Bounded Windows turnover diagnostics; never an ownership or stop predicate.

The driver must pass the opaque return from before-stop to both later calls.
The subprocess bounds potentially stalled operations; task XML and SDDL are retained when available.
"""
import datetime
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

PHASES = ("before-stop", "owner-absent", "after-start-request")
ACTIONS = ("seed", "snapshot")
LIMIT = 32 * 1024
ENTRY_LIMIT = 64
TIMEOUT = 12
LAUNCH = re.compile(r"[0-9a-f]{32}\Z")


def _read(path):
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return "unsafe", None
        if info.st_size > LIMIT:
            return "oversized", None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return "unsafe", None
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(LIMIT + 1)
        finally:
            os.close(fd)
        if len(raw) > LIMIT:
            return "oversized", None
        value = json.loads(raw)
        return ("observed", value) if isinstance(value, dict) else ("malformed", None)
    except FileNotFoundError:
        return "absent", None
    except (ValueError, UnicodeError, RecursionError):
        return "malformed", None
    except OSError:
        return "unreadable", None


def _launch(value):
    return value if isinstance(value, str) and LAUNCH.fullmatch(value) else None


def _native_windows():
    return os.name == "nt"


def _native_file_security(path):
    """Read owner/group/DACL with GetNamedSecurityInfoW; never alter a descriptor."""
    if not _native_windows():
        return {"status": "unavailable", "reason": "not-windows"}
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"status": "absent"}
    except OSError as exc:
        return {"status": "unavailable", "stage": "lstat", "winerror": getattr(exc, "winerror", None),
                "error": type(exc).__name__}
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        return {"status": "unavailable", "reason": "unsafe-leaf"}
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get = advapi.GetNamedSecurityInfoW
    get.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p)]
    get.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    code = get(str(path), 1, 7, None, None, None, None, ctypes.byref(descriptor))
    if code:
        return {"status": "unavailable", "stage": "GetNamedSecurityInfoW", "winerror": code}
    kernel.LocalFree(descriptor)
    return {"status": "observed", "stage": "GetNamedSecurityInfoW", "winerror": 0,
            "meaning": "read succeeded; no owner-only or consumer-time conclusion"}


_TASK_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
function failure($stage, $record) {
  $e = $record.Exception
  $chain = @()
  while ($null -ne $e -and $chain.Count -lt 5) {
    $chain += @{type=$e.GetType().FullName; hresult=[int]$e.HResult}
    $e = $e.InnerException
  }
  return @{ status='unavailable'; stage=$stage; exceptions=$chain; error_id=$record.FullyQualifiedErrorId }
}
$r = @{ definition=@{status='unavailable'; reason='task-not-opened'};
        security=@{status='unavailable'; reason='task-not-opened'} }
try {
  $scheduler = New-Object -ComObject 'Schedule.Service'
  $scheduler.Connect()
  $folder = $scheduler.GetFolder('\')
  $task = $folder.GetTask('Astrid Runtime')
} catch {
  $r.definition = failure 'open-task' $_
  $r.security = failure 'open-task' $_
  $r | ConvertTo-Json -Compress -Depth 6
  exit 0
}
try {
  $xml = [string]$task.Xml
  if ($xml.Length -le 8192) { $r.definition = @{status='observed'; xml=$xml} }
  else { $r.definition = @{status='unavailable'; reason='oversized'; characters=$xml.Length} }
} catch { $r.definition = failure 'get-xml' $_ }
try {
  $sddl = [string]$task.GetSecurityDescriptor(7)
  if ($sddl.Length -le 4096) { $r.security = @{status='observed'; sddl=$sddl} }
  else { $r.security = @{status='unavailable'; reason='oversized'; characters=$sddl.Length} }
} catch { $r.security = failure 'GetSecurityDescriptor' $_ }
$r | ConvertTo-Json -Compress -Depth 6
"""


def _selected_task():
    if not _native_windows():
        return {"status": "unavailable", "reason": "not-windows"}
    try:
        env = {k: v for k, v in os.environ.items() if k.casefold() != "psmodulepath"}
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _TASK_SCRIPT],
                                   capture_output=True, text=True, encoding="utf-8", timeout=8, env=env, check=False)
        if completed.returncode:
            return {"status": "unavailable", "reason": "powershell-failed", "exit_code": completed.returncode}
        value = json.loads(completed.stdout)
        if not isinstance(value, dict) or not all(isinstance(value.get(k), dict) for k in ("definition", "security")):
            raise ValueError("malformed task observation")
        return {"status": "queried", **value}
    except subprocess.TimeoutExpired:
        return {"status": "unavailable", "reason": "timeout"}
    except (OSError, ValueError) as exc:
        return {"status": "unavailable", "reason": "query-error", "error": type(exc).__name__,
                "winerror": getattr(exc, "winerror", None)}


def _identity(value, pattern):
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def _bounded_query(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:
        return {"status": "unavailable", "reason": "observer-error", "error": type(exc).__name__,
                "winerror": getattr(exc, "winerror", None)}


def _native_observation(data, phase, action):
    started_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    started_monotonic = time.monotonic_ns()
    support = data / "runtime"
    _, host = _read(support / "generic-host.json")
    _, runtime = _read(support / "discovery.json")
    def owner(value):
        value = value or {}
        pid = value.get("pid")
        return {"pid": pid if type(pid) is int and pid > 1 else None,
                "process_birth_id": _identity(value.get("process_birth_id"), r"windows-creation:[0-9]+"),
                "runtime_instance_id": _launch(value.get("runtime_instance_id")),
                "runtime_service_launch_id": _launch(value.get("runtime_service_launch_id"))}
    result = {"started_utc": started_utc, "started_monotonic_ns": started_monotonic, "action": action, "phase": phase,
            "run_id": _identity(os.environ.get("GITHUB_RUN_ID"), r"[0-9]+"),
            "run_attempt": _identity(os.environ.get("GITHUB_RUN_ATTEMPT"), r"[0-9]+"),
            "job": _identity(os.environ.get("GITHUB_JOB"), r"[A-Za-z0-9_-]{1,100}"),
            "task": {"path": "\\", "name": "Astrid Runtime",
                     "observation": _bounded_query(_selected_task) if phase == "before-stop" else
                                    {"status": "not-queried", "reason": "before-stop-only"}},
            "host": owner(host), "runtime": owner(runtime),
            "receipt_acl": _bounded_query(_native_file_security, support / "pack-host-job-cleanup.json"),
            "lock_acl": _bounded_query(_native_file_security, support / ".pack-host-job-cleanup.lock"),
            "meaning": "sequential read-only diagnostics, not consumer validation or authority"}
    result["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result["finished_monotonic_ns"] = time.monotonic_ns()
    return result


def _record(data, phase, reference, action):
    support = data / "runtime"
    registration_status, registration = _read(support / "generic-host.json")
    current = _launch(registration.get("runtime_service_launch_id")) if registration else None
    if phase == "before-stop":
        reference = current
    else:
        reference = _launch(reference)
    receipt_status, receipt = _read(support / "pack-host-job-cleanup.json")
    comparison = "unavailable"
    entries = []
    truncated = False
    if receipt is not None:
        rows = receipt.get("receipts")
        if not isinstance(rows, list):
            receipt_status = "malformed"
        else:
            truncated = len(rows) > ENTRY_LIMIT
            for row in rows[:ENTRY_LIMIT]:
                launch = _launch(row.get("runtime_service_launch_id")) if isinstance(row, dict) else None
                entries.append({"launch_id": launch, "job_drained": isinstance(row, dict) and row.get("job_drained") is True,
                                "matches_reference": launch == reference if launch and reference else None})
            if reference:
                if any(entry["matches_reference"] for entry in entries):
                    comparison = "matching-id-observed"
                elif truncated:
                    comparison = "incomplete"
                else:
                    comparison = "no-matching-id-observed"
    record = {
        "schema_version": 1, "phase": phase, "status": "observed",
        "native": _native_observation(data, phase, action),
        "meaning": "diagnostic content only; receipt presence is not authentication",
        "registration": {"status": registration_status, "launch_id": current},
        "reference": {"status": "observed" if reference else "unavailable", "launch_id": reference},
        "receipt": {"status": receipt_status, "comparison": comparison, "entries": entries,
                    "entries_truncated": truncated},
    }
    return record, reference


def _name(action, phase):
    return f"runtime-service-service-turnover-{action}-{phase}.log"


def _write(evidence, action, phase, record):
    encoded = (json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
    if len(encoded) > 256 * 1024:
        raise ValueError("projection exceeds bound")
    evidence.mkdir(parents=True, exist_ok=True)
    # Each phase has a distinct fixed filename; no producer file is written.
    target = evidence / _name(action, phase)
    temporary = evidence / (_name(action, phase) + ".tmp")
    temporary.write_bytes(encoded)
    os.replace(temporary, target)


def _worker(request):
    action, phase = request["action"], request["phase"]
    evidence, data = Path(request["evidence"]), Path(request["data"])
    if request.get("error"):
        _write(evidence, action, phase, {"schema_version": 1, "phase": phase, "status": "error",
                                         "reason": request["error"]})
        return None
    if phase == "before-stop":
        for later in PHASES[1:]:
            _write(evidence, action, later, {"schema_version": 1, "phase": later, "status": "not-reached"})
    record, reference = _record(data, phase, request.get("reference"), action)
    _write(evidence, action, phase, record)
    return reference


def _run(request):
    return subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker"],
                          input=json.dumps(request), text=True, capture_output=True,
                          timeout=TIMEOUT, check=True)


def observe_turnover(*, action: str, phase: str, evidence: Path, data: Path, reference: object | None) -> object | None:
    """Best-effort callback; failures never escape or influence driver execution."""
    if action not in ACTIONS or phase not in PHASES:
        return None
    try:
        request = {"action": action, "phase": phase, "evidence": str(evidence), "data": str(data),
                   "reference": _launch(reference)}
        completed = _run(request)
        result = json.loads(completed.stdout)
        return _launch(result) if phase == "before-stop" else None
    except Exception:
        # A bounded second attempt records failure when the evidence path is usable.
        try:
            _run({"action": action, "phase": phase, "evidence": str(evidence), "data": str(data),
                  "error": "observer-failed"})
        except Exception:
            pass
        return None


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    print(json.dumps(_worker(json.load(sys.stdin))))
elif __name__ == "__main__" and sys.argv[1:] == ["--file-security"]:
    print(json.dumps(_bounded_query(_native_file_security, Path(json.load(sys.stdin)))))
