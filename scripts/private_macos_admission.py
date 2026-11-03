#!/usr/bin/env python3
"""Transient, diagnostic-only staging; never uploads or alters journey evidence.

A reviewed private retention interface is still required. This wrapper deletes its
staging area on exit and is not wired into the qualifying journey workflow.
"""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

EXPECTED = "76257fe1b3939f0ce8fbaf5a8a21ef5e4e3b430a1badc933aefffd8f12f4f729"
CHECKS = set("""
action_is_list action_lookup_completed action_lookup_started action_script_form
canonical_action_match environment_is_dict historical_candidate_started
historical_environment_header historical_environment_mismatch
historical_environment_parse_completed historical_environment_parse_failed
historical_or_prior_match historical_script_available known_action_match
known_action_started module_action_match operation_id_absent plist_label_mismatch
plist_parse_completed plist_parse_started publication_lstat_started
publication_read_started recheck_bytes_mismatch recheck_lstat_started
recheck_read_started record_owner_mismatch replay_action_match replay_id_valid
replay_version_mismatch replay_version_parse_completed replay_version_parse_failed
retained_folder_started retained_folder_symlink retained_load_completed
retained_load_started retained_root_mismatch retained_version_file
selection_data_mismatch selection_owner_mismatch selection_program_mismatch
selection_read_completed selection_read_started service_exists service_owned
service_regular service_symlink transaction_read_completed transaction_read_started
r0_program_root_match r0_version_root_match
""".split())
STAT_KEYS = {"st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns"}
CHECKS.update("recheck_" + key + "_mismatch" for key in STAT_KEYS)
ROOT_KEYS = {"format", "checks", "stats", "digests", "truncated", "current_uid", "effective_uid", "source_sha256", "outcome", "exception_category", "pid", "started_ns"}
NAME = re.compile(r"admission-[0-9a-f]{32}\.json\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")


def _private(st, kind):
    return (kind(st.st_mode) and st.st_uid == os.geteuid()
            and stat.S_IMODE(st.st_mode) & 0o077 == 0)


def _acl_clear(path):
    subprocess.run(["chmod", "-N", str(path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _acl_check(path, directory=True)


def _acl_check(path, *, directory=False):
    result = subprocess.run(["ls", "-lde" if directory else "-le", str(path)],
                            check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if len(result.stdout.splitlines()) != 1:
        raise ValueError("ACL check failed")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _record(data):
    value = json.loads(data, object_pairs_hook=_unique_pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if (not isinstance(value, dict) or set(value) != ROOT_KEYS or
            value["format"] != "astrid-macos-admission-diagnostic/v1" or
            value["source_sha256"] != EXPECTED or value["truncated"] is not False or
            value["outcome"] not in ("returned", "raised") or
            value["exception_category"] not in (None, "os_error", "value_error", "type_error", "other") or
            any(type(value[k]) is not int or value[k] < 0 for k in ("current_uid", "effective_uid", "pid", "started_ns")) or
            value["pid"] == 0 or value["started_ns"] == 0):
        raise ValueError("invalid record")
    checks, stats, digests = value["checks"], value["stats"], value["digests"]
    if not isinstance(checks, list) or len(checks) > 4096:
        raise ValueError("invalid checks")
    for item in checks:
        if (not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str)
                or item[0] not in CHECKS or type(item[1]) is not bool):
            raise ValueError("invalid check")
    if not isinstance(stats, list) or len(stats) > 4096 or not isinstance(digests, list) or len(digests) > 4096:
        raise ValueError("invalid observations")
    for item in stats:
        if (not isinstance(item, dict) or set(item) != STAT_KEYS | {"phase"} or
                item["phase"] not in ("publication", "recheck") or
                any(type(item[key]) is not int for key in STAT_KEYS)):
            raise ValueError("invalid stat")
    for item in digests:
        if (not isinstance(item, dict) or set(item) != {"phase", "sha256"} or
                item["phase"] not in ("publication", "recheck") or
                not isinstance(item["sha256"], str) or not SHA.fullmatch(item["sha256"])):
            raise ValueError("invalid digest")
    return ["selection_read_started", True] in checks


def stage(capture, staging):
    """Validate every file before copying any bytes into fresh private staging."""
    if not _private(capture.lstat(), stat.S_ISDIR) or not _private(staging.lstat(), stat.S_ISDIR):
        raise ValueError("unsafe directory")
    _acl_check(capture, directory=True)
    _acl_check(staging, directory=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    cfd = os.open(capture, flags)
    sfd = os.open(staging, flags)
    try:
        if not _private(os.fstat(cfd), stat.S_ISDIR) or not _private(os.fstat(sfd), stat.S_ISDIR):
            raise ValueError("unsafe directory")
        names = os.listdir(cfd)
        if not names or len(names) > 256 or os.listdir(sfd):
            raise ValueError("missing or excessive capture")
        validated = []
        observed = False
        for name in names:
            if not NAME.fullmatch(name):
                raise ValueError("invalid filename")
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=cfd)
            try:
                info = os.fstat(fd)
                if not _private(info, stat.S_ISREG) or info.st_nlink != 1 or info.st_size > 2_000_000:
                    raise ValueError("unsafe file")
                _acl_check(capture / name)
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    data = stream.read(2_000_001)
                after = os.fstat(fd)
                stable = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
                          "st_size", "st_mtime_ns", "st_ctime_ns")
                if (len(data) != info.st_size or len(data) > 2_000_000 or
                        any(getattr(after, key) != getattr(info, key) for key in stable)):
                    raise ValueError("changed file")
                observed |= _record(data)
                validated.append((name, data))
            finally:
                os.close(fd)
        if not observed:
            raise ValueError("admission missing")
        for name, data in validated:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=sfd)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            _acl_check(staging / name)
        return len(validated)
    finally:
        os.close(cfd)
        os.close(sfd)


def run(command, runner_temp):
    """Run once with inherited opt-in; post-run diagnostics never replace its exit."""
    if sys.platform != "darwin" or not command or os.environ.get("ASTRID_MAC_ADMISSION_CAPTURE_DIR"):
        raise ValueError("invalid diagnostic setup")
    base = Path(tempfile.mkdtemp(prefix="astrid-private-admission-", dir=runner_temp))
    try:
        os.chmod(base, 0o700)
        _acl_clear(base)
        capture, staging = base / "capture", base / "staging"
        for path in (capture, staging):
            path.mkdir(mode=0o700)
            _acl_clear(path)
        env = os.environ.copy()
        env["ASTRID_MAC_ADMISSION_CAPTURE_DIR"] = str(capture)
        result = subprocess.run(command, env=env, check=False)
        try:
            count = stage(capture, staging)
            print(f"Diagnostic staging validated {count} record(s); private retention unavailable.", file=sys.stderr)
        except Exception:
            print("Diagnostic capture unavailable, incomplete, unsafe, or source mismatch.", file=sys.stderr)
        return result.returncode
    finally:
        try:
            shutil.rmtree(base)
        except OSError:
            print("Diagnostic cleanup failed; runner disposal required.", file=sys.stderr)


def main():
    if len(sys.argv) < 3 or sys.argv[1] != "--" or not os.environ.get("RUNNER_TEMP"):
        print("Diagnostic setup unavailable.", file=sys.stderr)
        return 1
    try:
        rc = run(sys.argv[2:], os.environ["RUNNER_TEMP"])
        return 128 - rc if rc < 0 else rc
    except Exception:
        print("Diagnostic setup unavailable.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
