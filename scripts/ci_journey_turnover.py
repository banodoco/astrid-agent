"""Bounded Windows turnover diagnostics; never an ownership or stop predicate.

The driver must pass the opaque return from before-stop to both later calls.
The subprocess bounds potentially stalled file operations and writes only projections.
"""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

PHASES = ("before-stop", "owner-absent", "after-start-request")
ACTIONS = ("seed", "snapshot")
LIMIT = 32 * 1024
ENTRY_LIMIT = 64
TIMEOUT = 3
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


def _record(data, phase, reference):
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
    if len(encoded) > 16 * 1024:
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
    record, reference = _record(data, phase, request.get("reference"))
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
