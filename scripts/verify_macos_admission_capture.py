"""Validate this diagnostic schema without echoing any capture content or path."""
import json
import os
from pathlib import Path
import re
import stat
import sys

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


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def reject_constant(_value):
    raise ValueError


def verify():
    directory = Path(os.environ["ASTRID_MAC_ADMISSION_CAPTURE_DIR"])
    st = directory.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise ValueError
    files = list(directory.iterdir())
    if not files or len(files) > 256:
        raise ValueError
    admission_observed = False
    for path in files:
        info = path.lstat()
        if not re.fullmatch(r"admission-[0-9a-f]{32}\.json", path.name) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1 or info.st_size > 2_000_000:
            raise ValueError
        with path.open("rb") as stream:
            value = json.load(stream, object_pairs_hook=unique_object, parse_constant=reject_constant)
        if (not isinstance(value, dict) or set(value) != ROOT_KEYS or
                value["format"] != "astrid-macos-admission-diagnostic/v1" or
                value["source_sha256"] != EXPECTED or value["truncated"] is not False or
                value["outcome"] not in ("returned", "raised") or
                value["exception_category"] not in (None, "os_error", "value_error", "type_error", "other") or
                type(value["current_uid"]) is not int or type(value["effective_uid"]) is not int or
                type(value["pid"]) is not int or value["pid"] <= 0 or
                type(value["started_ns"]) is not int or value["started_ns"] <= 0 or
                value["current_uid"] < 0 or value["effective_uid"] < 0 or
                (value["outcome"] == "returned" and value["exception_category"] is not None) or
                (value["outcome"] == "raised" and value["exception_category"] is None)):
            raise ValueError
        checks = value["checks"]
        if not isinstance(checks, list) or len(checks) > 4096:
            raise ValueError
        for item in checks:
            if not isinstance(item, list) or len(item) != 2 or item[0] not in CHECKS or type(item[1]) is not bool:
                raise ValueError
            admission_observed |= item == ["selection_read_started", True]
        if (not isinstance(value["stats"], list) or len(value["stats"]) > 4096 or
                not isinstance(value["digests"], list) or len(value["digests"]) > 4096):
            raise ValueError
        for item in value["stats"]:
            if not isinstance(item, dict) or set(item) != STAT_KEYS | {"phase"} or item["phase"] not in ("publication", "recheck") or any(type(item[key]) is not int for key in STAT_KEYS):
                raise ValueError
        for item in value["digests"]:
            if not isinstance(item, dict) or set(item) != {"phase", "sha256"} or item["phase"] not in ("publication", "recheck") or not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                raise ValueError
    if not admission_observed:
        raise ValueError
    print("Validated redacted admission capture files:", len(files))


try:
    verify()
except Exception:
    print("Capture unavailable, incomplete, unsafe, or source mismatch", file=sys.stderr)
    sys.exit(1)
