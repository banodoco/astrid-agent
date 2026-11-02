#!/usr/bin/env python3
"""Fail-closed cleanup of the dedicated macOS smoke's own launchd state."""
import json
import os
import re
import stat
import subprocess
from pathlib import Path

LABEL = "haus.astrid.runtime"
UID = 503


def launch_state():
    result = subprocess.run(["launchctl", "print", f"gui/{UID}/{LABEL}"],
                            capture_output=True, text=True, timeout=20, check=False)
    missing = f'Could not find service "{LABEL}" in domain for user gui: {UID}'
    absent_messages = {missing, f"Bad request.\n{missing}"}
    if result.returncode == 113 and result.stderr.strip().replace("\r\n", "\n") in absent_messages:
        return None
    if result.returncode != 0:
        raise RuntimeError("Cannot establish launchd service state")
    return result.stdout


def prove_service(state, root):
    """Require the exact loaded path and program, plus this test data root."""
    plist = root / "home/Library/LaunchAgents" / f"{LABEL}.plist"
    match_path = re.search(r"^\s*path = (.+?)\s*$", state, re.M)
    match_program = re.search(r"^\s*program = (.+?)\s*$", state, re.M)
    if not match_path or match_path.group(1) != str(plist) or not match_program:
        raise RuntimeError("Loaded service path does not match this smoke")
    pattern = re.escape(str(root / "program/versions")) + r"/[A-Za-z0-9._-]+/py/bin/python"
    if not re.fullmatch(pattern, match_program.group(1)):
        raise RuntimeError("Loaded service executable is not owned by this smoke")
    expected_data = str(root / "data")
    if (f"ASTRID_RUNTIME_DATA_ROOT => {expected_data}" not in state
            or "from astrid_runtime.cli import main" not in state
            or repr(expected_data) not in state):
        raise RuntimeError("Loaded service action does not match this smoke")


def stage_summary(root):
    """Expose only stage statuses, never raw logs or local paths."""
    receipt = root / "evidence/release0-to-release1/journey-result.json"
    if not receipt.is_file() or receipt.is_symlink():
        return {"receipt": "unavailable"}
    if receipt.stat().st_uid != UID:
        raise RuntimeError("Receipt is not owned by the runner")
    row = json.loads(receipt.read_text(encoding="utf-8"))
    if row.get("job_id") != "direct-macos" or row.get("scenario") != "release0-to-release1":
        raise RuntimeError("Receipt does not describe the smoke journey")
    checks = row.get("checks") if isinstance(row.get("checks"), dict) else {}
    def status(value):
        return value if value in {"pass", "fail", "not-run"} else "not-recorded"
    safe_checks = {name: status(checks.get(name)) for name in
                   ("release", "service", "health", "semantic_data", "task_settled", "one_runtime", "unrelated_sentinel")}
    return {"receipt": "available", "status": status(row.get("status")), "checks": safe_checks,
            "release0_bootstrap": bool(row.get("release0_fixture")),
            "release_transition": bool(row.get("release_transition")),
            "final_cleanup": status(row.get("final_cleanup"))}


def prove_root(root):
    """Authenticate the run marker and reject symlinks and wrong owners."""
    st = root.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != UID or root != root.resolve():
        raise RuntimeError("Smoke root is not a runner-owned real directory")
    marker = root / "tst-b1-owner.json"
    st = marker.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != UID:
        raise RuntimeError("Smoke ownership marker is not runner-owned")
    data = json.loads(marker.read_text(encoding="utf-8"))
    if (data.get("schema_version") != 1 or not re.fullmatch(r"[0-9a-f]{32}", str(data.get("run_id", "")))
            or data.get("root") != str(root / "program") or data.get("data_root") != str(root / "data")):
        raise RuntimeError("Smoke ownership marker does not match this root")


def active_smoke_process(root):
    output = subprocess.check_output(["ps", "-ww", "-axo", "uid=,pid=,command="], text=True)
    # Keep multiline Python -c continuations attached to their owning process.
    records = []
    for line in output.splitlines():
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+(.*)$", line)
        if match:
            records.append((int(match.group(1)), match.group(3)))
        elif records:
            uid, command = records[-1]
            records[-1] = (uid, command + "\n" + line)
    return any(uid == UID and str(root) in command for uid, command in records)


def main():
    """Observe only. The runner's own temp cleanup happens before this step."""
    if os.getuid() != UID or os.environ.get("RUNNER_NAME") != "hannah-mac":
        raise RuntimeError("This diagnostic must run as the dedicated runner")
    temp = Path(os.environ["RUNNER_TEMP"])
    if not temp.is_absolute() or temp != temp.resolve():
        raise RuntimeError("Runner temp is not an absolute real path")
    root = temp / "astrid-r1-direct-macos"
    root_status = "absent"
    stages = {"receipt": "unavailable"}
    if root.exists() or root.is_symlink():
        try:
            prove_root(root)
            root_status = "verified smoke-owned"
            stages = stage_summary(root)
        except (RuntimeError, OSError, ValueError):
            root_status = "unverified"
    state = launch_state()
    service = "absent"
    if state is not None:
        try:
            prove_service(state, root)
            service = "loaded, matching smoke identity"
        except RuntimeError:
            service = "loaded, identity does not match smoke"
    summary = {"status": "observed", "service": service,
               "service_domain_uid": UID, "root": root_status,
               "runner_process_references_root": active_smoke_process(root), "stages": stages}
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Messages are intentionally path-free; detailed host state stays local.
        reason = str(exc) if type(exc) is RuntimeError else "System or data error during cleanup"
        print(json.dumps({"status": "refused", "reason": reason}))
        raise SystemExit(1)
