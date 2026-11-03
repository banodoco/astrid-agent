#!/usr/bin/env python3
"""Stop one exact, preobserved orphaned R25 tree; never delete task data."""
import hashlib
import json
import os
import pwd
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

UID = 503
# [parent PID, normalized ps lstart, SHA256 of full ps command]
EXPECTED = {
  86213: [1, "Mon Nov 2 23:31:58 2026", "38f7320ce0d36e4883f66d1a91163cf03b8abd60c68fcb548a720c14b4d20b57"],
  86228: [86213, "Mon Nov 2 23:31:58 2026", "38f7320ce0d36e4883f66d1a91163cf03b8abd60c68fcb548a720c14b4d20b57"],
  86229: [86213, "Mon Nov 2 23:31:58 2026", "38f7320ce0d36e4883f66d1a91163cf03b8abd60c68fcb548a720c14b4d20b57"],
  86230: [86228, "Mon Nov 2 23:31:58 2026", "99b7a4bca0fbd46a92d54b371093ebcd8fc4898210db26020c8d11f44e6e18ca"],
  86231: [86229, "Mon Nov 2 23:31:58 2026", "a6799bd72fb57bb1a427120d19ec7c3c0a0e405c9663d25643348aafab541519"],
  88221: [86213, "Mon Nov 2 23:32:36 2026", "b16c108d7e3f07c1ded1ff79ada34e11b0ca12f72b39b78eade1f33223abf549"],
  8660: [88221, "Tue Nov 3 00:30:37 2026", "b16c108d7e3f07c1ded1ff79ada34e11b0ca12f72b39b78eade1f33223abf549"],
  8661: [8660, "Tue Nov 3 00:30:37 2026", "a49234356d371cf1b759b02896e8f643430d618cf69f72dd0dd98e8d2ec0369f"],
  8688: [8661, "Tue Nov 3 00:30:39 2026", "4151e6032d2ff5cf41f38b68d76adcc0cc02232292ca2c48548f610b14f2861a"],
}
FREEZE_ORDER = [86213, 86228, 86229, 88221, 86230, 86231, 8660, 8661, 8688]
KILL_ORDER = [8688, 8661, 8660, 88221, 86230, 86231, 86228, 86229, 86213]


def observe(pid):
    result = subprocess.run(["/bin/ps", "-ww", "-p", str(pid), "-o", "uid=,ppid=,state=,lstart=,command="],
                            capture_output=True, text=True, timeout=10)
    if result.returncode != 0 or not result.stdout.strip(): return None
    row = result.stdout.strip()
    match = re.fullmatch(r"(\d+)\s+(\d+)\s+(\S+)\s+(\w+\s+\w+\s+\d+\s+\d\d:\d\d:\d\d\s+\d{4})\s+(.*)", row)
    if not match: raise RuntimeError("Process identity format is unrecognized")
    uid, parent, state, start, command = match.groups()
    return {"uid": int(uid), "ppid": int(parent), "state": state,
            "start": " ".join(start.split()), "command_sha256": hashlib.sha256(command.encode()).hexdigest()}


def tree():
    result = subprocess.run(["/bin/ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=15, check=True)
    pairs = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not all(value.isdecimal() for value in fields):
            raise RuntimeError("Process relation census is unrecognized")
        pairs.append(tuple(map(int, fields)))
    wanted = {86213}
    while True:
        expanded = wanted | {pid for pid, ppid in pairs if ppid in wanted}
        if expanded == wanted: return wanted
        wanted = expanded


def matches(pid, actual, *, require_parent=True):
    if actual is None or actual["uid"] != UID: return False
    ppid, start, command_hash = EXPECTED[pid]
    return (actual["start"] == start and actual["command_sha256"] == command_hash
            and (not require_parent or actual["ppid"] == ppid))


def port_state():
    result = subprocess.run(["/usr/sbin/lsof", "-nP", "-iTCP:17340", "-sTCP:LISTEN"],
                            capture_output=True, text=True, timeout=20)
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip(): return "no-listener"
    if result.returncode == 0 and result.stdout.strip(): return "occupied"
    return "unverified"


def checkpoint(path, audit):
    temp = path.with_suffix(".next")
    with temp.open("w") as fh:
        json.dump(audit, fh, indent=2, sort_keys=True); fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
    os.replace(str(temp), str(path))


def freeze_and_stop(audit, save):
    frozen = []
    kill_started = False
    try:
        if tree() != set(EXPECTED): raise RuntimeError("Expected R25 descendant set differs")
        before = {}
        for pid in FREEZE_ORDER:
            row = observe(pid)
            if not matches(pid, row) or row["state"][0] not in "SRI":
                raise RuntimeError("Expected R25 process identity or running state differs")
            before[str(pid)] = row
        audit["before"] = before; audit["stage"] = "verified"; save(audit)
        for pid in FREEZE_ORDER:
            current = observe(pid)
            if not matches(pid, current) or current["state"][0] not in "SRI":
                raise RuntimeError("R25 identity changed while freezing")
            os.kill(pid, signal.SIGSTOP)
            frozen.append(pid)
        deadline = time.monotonic() + 5
        while True:
            rows = {pid: observe(pid) for pid in FREEZE_ORDER}
            if all(matches(pid, rows[pid]) and rows[pid]["state"].startswith("T") for pid in FREEZE_ORDER): break
            if time.monotonic() >= deadline: raise RuntimeError("R25 tree did not enter stopped state")
            time.sleep(0.1)
        if tree() != set(EXPECTED): raise RuntimeError("R25 descendant set changed while freezing")
        audit["stage"] = "frozen-and-revalidated"; audit["frozen_pids"] = frozen[:]; save(audit)
        # Direct SIGKILL only. SIGTERM is forbidden: pinned pinokiod handles it by signaling Caddy.
        kill_started = True
        audit["stage"] = "kill-started"; audit["killed_pids"] = []; save(audit)
        for pid in KILL_ORDER:
            current = observe(pid)
            if not matches(pid, current) or not current["state"].startswith("T"):
                raise RuntimeError("R25 identity changed before direct kill")
            os.kill(pid, signal.SIGKILL)
            audit["killed_pids"].append(pid); save(audit)
        deadline = time.monotonic() + 10
        while True:
            after = {pid: observe(pid) for pid in FREEZE_ORDER}
            if all(row is None or row["state"].startswith("Z") for row in after.values()): break
            if time.monotonic() >= deadline: raise RuntimeError("Some R25 PIDs remain alive after direct kill")
            time.sleep(0.2)
        audit["after"] = {str(pid): ("absent" if row is None else "zombie") for pid, row in after.items()}
        audit["stage"] = "stopped"; save(audit)
    except Exception as exc:
        audit["error"] = str(exc) if isinstance(exc, RuntimeError) else "System error during guarded action"
        audit["error_type"] = type(exc).__name__
        if not kill_started:
            audit["resume"] = {}
            for pid in reversed(frozen):
                try:
                    row = observe(pid)
                    if matches(pid, row, require_parent=False) and not row["state"].startswith("Z"):
                        # SIGCONT also cancels a still-pending SIGSTOP.
                        os.kill(pid, signal.SIGCONT); audit["resume"][str(pid)] = "sent"
                    else: audit["resume"][str(pid)] = "identity-or-state-changed"
                except Exception: audit["resume"][str(pid)] = "failed"
            audit["stage"] = "refused-pre-kill"
        else:
            # Post-kill partial failures are explicitly left for review; do not signal extras.
            audit["stage"] = "partial-kill-review-required"
        save(audit)
        return False
    return True


def main():
    if os.getuid() != UID or os.environ.get("RUNNER_NAME") != "hannah-mac" or sys.platform != "darwin":
        raise RuntimeError("Unexpected runner identity")
    home = Path(pwd.getpwuid(UID).pw_dir)
    temp = Path(os.environ["RUNNER_TEMP"])
    if (temp != home / "actions-runner/_work/_temp" or temp.resolve() != temp or temp.is_symlink()
            or not temp.is_dir() or temp.stat().st_uid != UID):
        raise RuntimeError("Unexpected runner temp")
    output = temp / "r25-exact-stop"; output.mkdir(mode=0o700)
    with open(os.environ["GITHUB_ENV"], "a") as fh: fh.write("R25_STOP_UPLOAD=" + str(output) + "\n")
    audit = {"schema": "r25-exact-stop/v1", "runner": "hannah-mac", "uid": UID,
             "deletion": "none", "port_before": port_state(), "stage": "started"}
    path = output / "audit.json"
    checkpoint(path, audit)
    success = freeze_and_stop(audit, lambda value: checkpoint(path, value))
    try: audit["port_after"] = port_state()
    except Exception: audit["port_after"] = "unverified"
    checkpoint(path, audit)
    (output / "SHA256SUMS").write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "  audit.json\n")
    print(json.dumps({"stage": audit["stage"], "port_before": audit["port_before"], "port_after": audit["port_after"]}))
    if not success: raise SystemExit(1)


if __name__ == "__main__":
    try: main()
    except Exception:
        print(json.dumps({"stage": "preflight-failed"}))
        raise SystemExit(1)
