"""Bounded Windows receipt observation using the selected installed R0 Python.

Diagnostic only: no producer witness, consumer matcher, repair, or admission.
The caller must already have admitted the run-owned support root. All authority
opens omit creation/truncation; the existing native lock is tried once. Even a
brief exclusive lock can affect scheduling. This is not timing-neutral proof.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time


BYTE_LIMIT = 262144
ENTRY_LIMIT = 64
STRING_LIMIT = 4096
PHASES = ("before-stop", "owner-exited-before-helper-and-restart")
SAFE_FIELDS = (
    "version", "schema_version", "service_name", "support_root", "pid",
    "process_birth_id", "runtime_service_launch_id", "runtime_instance_id",
    "runtime_epoch", "coordinator_epoch", "active_realm", "job_drained",
)
AUTHORITY_NAMES = ("generic-host.json", "pack-host-job-cleanup.json",
                   ".pack-host-job-cleanup.lock", "discovery.json")


def clock() -> dict:
    return {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "monotonic_ns": time.monotonic_ns()}


def error(exc: Exception) -> dict:
    # Never dump exception messages containing arbitrary product file contents.
    return {"status": "error", "type": type(exc).__name__,
            "errno": getattr(exc, "errno", None), "winerror": getattr(exc, "winerror", None)}


def metadata(path: Path) -> dict:
    try:
        info = path.lstat()
        return {"status": "observed", "regular": stat.S_ISREG(info.st_mode),
                "directory": stat.S_ISDIR(info.st_mode),
                "reparse": stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400),
                "bytes": info.st_size, "device": info.st_dev, "inode": info.st_ino,
                "mtime_ns": info.st_mtime_ns}
    except FileNotFoundError:
        return {"status": "absent"}
    except OSError as exc:
        return error(exc)


def projection(value: object) -> dict:
    if not isinstance(value, dict):
        return {"json_type": type(value).__name__, "complete": False}
    fields = {}
    omitted = 0
    for key in SAFE_FIELDS:
        if key not in value:
            continue
        item = value[key]
        if item is None or type(item) in (bool, int) or (isinstance(item, str) and len(item) <= STRING_LIMIT):
            fields[key] = item
        else:
            omitted += 1
    result = {"fields": fields, "unknown_field_count": len(set(value) - set(SAFE_FIELDS) - {"receipts"}),
              "unsafe_or_oversize_field_count": omitted}
    if "receipts" in value:
        entries = value["receipts"]
        result["receipts_type"] = type(entries).__name__
        if isinstance(entries, list):
            result.update(receipt_count=len(entries), entries_omitted=max(0, len(entries) - ENTRY_LIMIT),
                          receipts=[projection(item) for item in entries[:ENTRY_LIMIT]
                                    if not isinstance(item, dict) or "receipts" not in item])
            result["nested_entries_omitted"] = sum(isinstance(item, dict) and "receipts" in item
                                                    for item in entries[:ENTRY_LIMIT])
    result["complete"] = not (result["unknown_field_count"] or omitted or result.get("entries_omitted")
                               or result.get("nested_entries_omitted"))
    return result


def read_json(path: Path, native_security) -> dict:
    result = {"open": "not-attempted", "read": "not-attempted", "parse": "not-attempted",
              "complete_bytes_read": False}
    info = metadata(path)
    if info.get("reparse") or (info.get("status") == "observed" and not info.get("regular")):
        return {**result, "reason": "existing-regular-nonreparse-file-required"}
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        result["open"] = "ok"
        opened = os.fstat(fd)
        result["opened_identity"] = {"device": opened.st_dev, "inode": opened.st_ino,
                                     "bytes": opened.st_size, "mtime_ns": opened.st_mtime_ns}
        if not stat.S_ISREG(opened.st_mode):
            return {**result, "reason": "opened-file-not-regular"}
        try:
            native_security.validate(fd)
            result["opened_native_acl"] = {"status": "accepted-by-installed-native-validator"}
        except OSError as exc:
            result["opened_native_acl"] = error(exc)
        # ACL rejection is recorded independently of the diagnostic read; it
        # grants no trust. Only the fixed safe projection ever leaves this child.
        raw = bytearray()
        while len(raw) <= BYTE_LIMIT:
            block = os.read(fd, min(65536, BYTE_LIMIT + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        if len(raw) > BYTE_LIMIT:
            return {**result, "read": "byte-limit-exceeded", "byte_limit": BYTE_LIMIT}
        result.update(read="ok", bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                      complete_bytes_read=True)
        try:
            result.update(parse="ok", projection=projection(json.loads(raw.decode("utf-8"))))
        except (ValueError, UnicodeError, RecursionError) as exc:
            result["parse"] = error(exc)
    except OSError as exc:
        result["open" if fd is None else "read"] = error(exc)
    finally:
        if fd is not None:
            os.close(fd)
    return result


def capture(support_root: Path, phase: str, native_platform, native_security) -> dict:
    if phase not in PHASES:
        raise ValueError("unknown observation boundary")
    report = {"schema_version": 1, "kind": "windows-original-launch-receipt-observation",
              "phase": phase, "start": clock(), "support_root_argument": str(support_root),
              "interpreter": sys.executable, "observer_pid": os.getpid(),
              "admission": "not-evaluated", "producer_events": "not-observed",
              "consumer_matcher_read_1": "not-observed", "consumer_matcher_read_2": "not-observed",
              "atomicity": "sequential; path/reparse replacement races not excluded",
              "owner_exit_proof": "caller boundary only; no retained Job-zero proof",
              "completeness": {"snapshot_finished": False, "producer_trace": False,
                               "actual_consumer_reads": False}, "module_provenance": {}}
    for module in (native_platform, native_security):
        path = Path(module.__file__)
        with path.open("rb") as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
        report["module_provenance"][module.__name__] = {
            "path": str(path), "sha256": hashlib.sha256(raw).hexdigest() if len(raw) <= 2 * 1024 * 1024 else None,
            "complete": len(raw) <= 2 * 1024 * 1024}
    # Do not follow known junctions/symlinks, including directory ancestors.
    for parent in (support_root, *support_root.parents):
        info = metadata(parent)
        if info.get("reparse") or not info.get("directory"):
            return {**report, "support_path": {"status": "refused", "path": str(parent), "metadata": info},
                    "end": clock()}
    paths = {name: support_root / name for name in AUTHORITY_NAMES}
    report["files_before"] = {name: metadata(path) for name, path in paths.items()}
    report["native_acl"] = {}
    for name, path in paths.items():
        if report["files_before"][name].get("reparse"):
            report["native_acl"][name] = {"status": "reparse-refused"}
            continue
        try:
            native_security.validate(path)
            report["native_acl"][name] = {"status": "accepted-by-installed-native-validator"}
        except OSError as exc:
            report["native_acl"][name] = error(exc)
    report["registration"] = read_json(paths["generic-host.json"], native_security)
    report["selected_discovery"] = read_json(paths["discovery.json"], native_security)
    report["receipt_read_under_lock"] = {"status": "not-attempted"}
    report["lock"] = {"open": "not-attempted", "acquire": "not-attempted", "release": "not-attempted"}
    lock = report["lock"]
    info = report["files_before"][".pack-host-job-cleanup.lock"]
    fd, locked = None, False
    try:
        if not info.get("regular") or info.get("reparse"):
            lock["reason"] = "existing-regular-nonreparse-lock-required"
        else:
            fd = os.open(paths[".pack-host-job-cleanup.lock"], os.O_RDWR | getattr(os, "O_BINARY", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
            lock["open"] = "ok"
            opened = os.fstat(fd)
            lock["opened_identity"] = {"device": opened.st_dev, "inode": opened.st_ino}
            if not stat.S_ISREG(opened.st_mode):
                lock["reason"] = "opened-lock-not-regular"
            else:
                try:
                    native_security.validate(fd)
                    lock["opened_native_acl"] = {"status": "accepted-by-installed-native-validator"}
                except OSError as exc:
                    lock["opened_native_acl"] = error(exc)
                native_platform.lock_file(fd, exclusive=True, blocking=False)
                locked = True
                lock["acquire"] = "ok-nonblocking"
                report["receipt_read_under_lock"] = read_json(paths["pack-host-job-cleanup.json"], native_security)
    except (OSError, ValueError) as exc:
        lock["open" if fd is None else "acquire"] = error(exc)
    finally:
        if locked:
            try:
                native_platform.unlock_file(fd)
                lock["release"] = "ok"
            except OSError as exc:
                lock["release"] = error(exc)
        if fd is not None:
            os.close(fd)
    report["files_after"] = {name: metadata(path) for name, path in paths.items()}
    report["completeness"]["snapshot_finished"] = True
    report["end"] = clock()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--support-root", required=True, type=Path)
    parser.add_argument("--phase", required=True, choices=PHASES)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("requires Windows and the selected installed R0 interpreter")
    sys.dont_write_bytecode = True
    from runtime_protocol import platform, windows_security
    print(json.dumps(capture(args.support_root, args.phase, platform, windows_security), sort_keys=True))
    # Zero means observation completed, regardless of any captured errors.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
