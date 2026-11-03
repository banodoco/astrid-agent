#!/usr/bin/env python3
"""Stop one exact, preobserved orphaned R25 tree; never delete task data."""
import errno
import fcntl
import hashlib
import json
import os
import pwd
import re
import signal
import stat
import shutil
import subprocess
import sys
import time
import tempfile
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
    if result.returncode != 0 or not result.stdout.strip():
        # A failed ps query is not evidence that the PID is absent.
        if result.returncode != 1 or result.stdout.strip() or result.stderr.strip():
            raise RuntimeError("Process absence could not be verified")
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except OSError:
            raise RuntimeError("Process absence could not be verified")
        raise RuntimeError("Process exists despite empty ps query")
    row = result.stdout.strip()
    match = re.fullmatch(r"(\d+)\s+(\d+)\s+(\S+)\s+(\w+\s+\w+\s+\d+\s+\d\d:\d\d:\d\d\s+\d{4})\s+(.*)", row)
    if not match: raise RuntimeError("Process identity format is unrecognized")
    uid, parent, state, start, command = match.groups()
    return {"uid": int(uid), "ppid": int(parent), "state": state,
            "start": " ".join(start.split()), "command_sha256": hashlib.sha256(command.encode()).hexdigest()}


def descendants(roots):
    result = subprocess.run(["/bin/ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=15, check=True)
    pairs = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not all(value.isdecimal() for value in fields):
            raise RuntimeError("Process relation census is unrecognized")
        pairs.append(tuple(map(int, fields)))
    wanted = set(roots)
    while True:
        expanded = wanted | {pid for pid, ppid in pairs if ppid in wanted}
        if expanded == wanted: return wanted
        wanted = expanded


def tree():
    return descendants({86213})


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
    fd, name = tempfile.mkstemp(prefix=".audit-", dir=str(path.parent))
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(audit, fh, indent=2, sort_keys=True); fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
    os.replace(name, str(path))
    dirfd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(dirfd)
    finally: os.close(dirfd)


class Cancelled(Exception):
    pass


def cancel_handler(signum, frame):
    raise Cancelled("Action interrupted")


def silence_cancel():
    # Block delivery while switching handlers, then discard pending cancellations.
    signals = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}
    signal.pthread_sigmask(signal.SIG_BLOCK, signals)
    try:
        for sig in signals: signal.signal(sig, signal.SIG_IGN)
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, signals)


def guard_fingerprint():
    return hashlib.sha256(json.dumps(EXPECTED, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def terminal(pid, row):
    # A zombie belongs to this target only if UID and original birth still match.
    return row is None or (row["state"].startswith("Z") and row["uid"] == UID and row["start"] == EXPECTED[pid][1])


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
            before[str(pid)] = dict(row)
        audit["before"] = before; audit["stage"] = "verified"; audit["stop_intents"] = []; save(audit)
        for pid in FREEZE_ORDER:
            current = observe(pid)
            if not matches(pid, current) or current["state"][0] not in "SRI":
                raise RuntimeError("R25 identity changed while freezing")
            # Persist intent first so a hard interruption cannot hide a sent STOP.
            audit["stage"] = "freezing"; audit["stop_intents"].append(pid); save(audit)
            try:
                os.kill(pid, signal.SIGSTOP)
            except OSError:
                # The kernel rejected the signal; remove this unsent intent.
                audit["stop_intents"].pop(); save(audit)
                raise
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
        audit["stage"] = "kill-started"; audit["killed_pids"] = []; save(audit)
        kill_started = True
        for pid in KILL_ORDER:
            current = observe(pid)
            if not matches(pid, current) or not current["state"].startswith("T"):
                raise RuntimeError("R25 identity changed before direct kill")
            os.kill(pid, signal.SIGKILL)
            audit["killed_pids"].append(pid); save(audit)
        deadline = time.monotonic() + 10
        while True:
            after = {pid: observe(pid) for pid in FREEZE_ORDER}
            if all(terminal(pid, row) for pid, row in after.items()): break
            if time.monotonic() >= deadline: raise RuntimeError("Some R25 PIDs remain alive after direct kill")
            time.sleep(0.2)
        audit["after"] = {str(pid): ("absent" if row is None else "zombie") for pid, row in after.items()}
        audit["stage"] = "stopped"; save(audit)
    except Exception as exc:
        silence_cancel()
        audit["error"] = str(exc) if isinstance(exc, RuntimeError) else "System error during guarded action"
        audit["error_type"] = type(exc).__name__
        if not kill_started:
            audit["resume"] = {}
            for pid in reversed(audit.get("stop_intents", [])):
                try:
                    row = observe(pid)
                    if matches(pid, row, require_parent=False) and not row["state"].startswith("Z"):
                        # SIGCONT also cancels a still-pending SIGSTOP.
                        os.kill(pid, signal.SIGCONT); audit["resume"][str(pid)] = "sent"
                    else: audit["resume"][str(pid)] = "identity-or-state-changed"
                except Exception: audit["resume"][str(pid)] = "failed"
            audit["stage"] = ("pre-kill-recovery-required" if any(value != "sent" for value in audit["resume"].values())
                              else "refused-pre-kill")
        else:
            # Post-kill partial failures are explicitly left for review; do not signal extras.
            audit["stage"] = "partial-kill-review-required"
        save(audit)
        return False
    return True


def recovery(audit, save):
    """Explicit recovery: resume pre-kill stops or finish a partially executed stop."""
    stage = audit.get("stage")
    intents = audit.get("stop_intents")
    before = audit.get("before")
    if (audit.get("schema") != "r25-exact-stop/v2" or audit.get("guard_sha256") != guard_fingerprint()
            or audit.get("uid") != UID or audit.get("runner") != "hannah-mac" or audit.get("deletion") != "none"
            or not isinstance(intents, list) or intents != FREEZE_ORDER[:len(intents)]
            or not isinstance(before, dict) or set(before) != {str(pid) for pid in EXPECTED}
            or any(not matches(pid, before[str(pid)]) or before[str(pid)]["state"][0] not in "SRI" for pid in EXPECTED)):
        raise RuntimeError("Recovery audit does not match this exact guard")
    if stage in {"verified", "freezing", "frozen-and-revalidated", "pre-kill-recovery-required", "recovery-resuming"}:
        candidates = []
        already_resumed = set(audit.get("recovery_resumed", [])) if stage == "recovery-resuming" else set()
        if stage == "pre-kill-recovery-required":
            already_resumed |= {int(pid) for pid, value in audit.get("resume", {}).items() if value == "sent" and pid.isdecimal()}
        for pid in reversed(intents):
            if pid in already_resumed: continue
            row = observe(pid)
            if terminal(pid, row): continue
            if not matches(pid, row, require_parent=False) or row["ppid"] not in {EXPECTED[pid][0], 1}:
                raise RuntimeError("Recovery target identity differs")
            candidates.append(pid)
        audit["stage"] = "recovery-resuming"; audit["recovery_resumed"] = sorted(already_resumed); save(audit)
        for pid in candidates:
            row = observe(pid)
            if not matches(pid, row, require_parent=False) or row["ppid"] not in {EXPECTED[pid][0], 1}:
                raise RuntimeError("Recovery target changed before resume")
            os.kill(pid, signal.SIGCONT)
            audit["recovery_resumed"].append(pid); save(audit)
        audit["stage"] = "recovered-pre-kill"; save(audit)
        return True
    if stage in {"kill-started", "partial-kill-review-required", "recovery-killing"}:
        if intents != FREEZE_ORDER or audit.get("frozen_pids") != FREEZE_ORDER:
            raise RuntimeError("Partial-stop audit lacks a fully verified frozen tree")
        survivors = []
        for pid in KILL_ORDER:
            row = observe(pid)
            if terminal(pid, row): continue
            if (not matches(pid, row, require_parent=False) or row["ppid"] not in {EXPECTED[pid][0], 1}
                    or not row["state"].startswith("T")):
                raise RuntimeError("Partial-stop target changed or is not frozen")
            survivors.append(pid)
        if survivors and not descendants(set(survivors)).issubset(set(EXPECTED)):
            raise RuntimeError("Partial-stop target has an unlisted descendant")
        audit["stage"] = "recovery-killing"; audit["recovery_killed"] = []; save(audit)
        for pid in survivors:
            row = observe(pid)
            if (not matches(pid, row, require_parent=False) or row["ppid"] not in {EXPECTED[pid][0], 1}
                    or not row["state"].startswith("T")):
                raise RuntimeError("Partial-stop target changed before direct kill")
            os.kill(pid, signal.SIGKILL)
            audit["recovery_killed"].append(pid); save(audit)
        deadline = time.monotonic() + 10
        while True:
            after = {pid: observe(pid) for pid in EXPECTED}
            if all(terminal(pid, row) for pid, row in after.items()): break
            if time.monotonic() >= deadline: raise RuntimeError("Recovery could not verify all targets stopped")
            time.sleep(0.2)
        audit["after"] = {str(pid): ("absent" if row is None else "zombie") for pid, row in after.items()}
        audit["stage"] = "recovered-stop"; save(audit)
        return True
    raise RuntimeError("Audit stage does not permit recovery")


def owned_private_directory(path, *, create=True):
    if not path.exists() and not path.is_symlink():
        if not create: raise RuntimeError("Private audit directory is absent")
        path.mkdir(mode=0o700)
    st = path.lstat()
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != UID or path != path.resolve()
            or stat.S_IMODE(st.st_mode) != 0o700):
        raise RuntimeError("Private audit directory is unsafe")


def acquire_lock(parent):
    path = parent / "action.lock"
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != UID or st.st_nlink != 1:
        os.close(fd); raise RuntimeError("Action lock is unsafe")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = path.lstat()
        if (current.st_dev, current.st_ino, current.st_uid) != (st.st_dev, st.st_ino, UID):
            raise RuntimeError("Action lock changed")
    except Exception:
        os.close(fd); raise RuntimeError("Another guarded action is active or lock changed")
    return fd


def load_audit(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != UID or st.st_nlink != 1 or st.st_size > 1_000_000:
            raise RuntimeError("Recovery audit is unsafe")
        return json.load(fh)


def main():
    if os.getuid() != UID or os.environ.get("RUNNER_NAME") != "hannah-mac" or sys.platform != "darwin":
        raise RuntimeError("Unexpected runner identity")
    home = Path(pwd.getpwuid(UID).pw_dir)
    if home.is_symlink() or home.resolve() != home or home.stat().st_uid != UID:
        raise RuntimeError("Unexpected runner home")
    temp = Path(os.environ["RUNNER_TEMP"])
    if (temp != home / "actions-runner/_work/_temp" or temp.resolve() != temp or temp.is_symlink()
            or not temp.is_dir() or temp.stat().st_uid != UID):
        raise RuntimeError("Unexpected runner temp")
    parent = home / ".astrid-r25-stop-audits"
    owned_private_directory(parent)
    lockfd = acquire_lock(parent)
    try:
        phase = os.environ.get("R25_STOP_PHASE", "")
        own_run_id = os.environ.get("GITHUB_RUN_ID", "") + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "")
        audit_id = own_run_id if phase == "stop" else os.environ.get("R25_STOP_AUDIT_ID", "")
        if phase not in {"stop", "recover"} or not re.fullmatch(r"[0-9]+-[0-9]+", audit_id):
            raise RuntimeError("Invalid action phase or audit id")
        audit_dir = parent / audit_id
        if phase == "stop":
            audit_dir.mkdir(mode=0o700)
        owned_private_directory(audit_dir, create=False)
        path = audit_dir / "audit.json"
        output = temp / "r25-exact-stop"; output.mkdir(mode=0o700)
        with open(os.environ["GITHUB_ENV"], "a") as fh: fh.write("R25_STOP_UPLOAD=" + str(output) + "\n")
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP): signal.signal(sig, cancel_handler)
        if phase == "stop":
            audit = {"schema": "r25-exact-stop/v2", "runner": "hannah-mac", "uid": UID,
                     "guard_sha256": guard_fingerprint(), "audit_id": audit_id,
                     "deletion": "none", "port_before": port_state(), "stage": "started"}
            checkpoint(path, audit)
            if audit["port_before"] != "no-listener":
                audit["stage"] = "refused-port-unavailable"; checkpoint(path, audit)
                success = False
            else:
                success = freeze_and_stop(audit, lambda value: checkpoint(path, value))
        else:
            audit = load_audit(path)
            if audit.get("audit_id") != audit_id: raise RuntimeError("Recovery audit id differs")
            try: success = recovery(audit, lambda value: checkpoint(path, value))
            except Exception as exc:
                silence_cancel()
                audit["recovery_error"] = str(exc) if isinstance(exc, RuntimeError) else "Recovery system error"
                checkpoint(path, audit)
                success = False
        try: audit["port_after"] = port_state()
        except Exception: audit["port_after"] = "unverified"
        checkpoint(path, audit)
        shutil.copyfile(path, output / "audit.json")
        (output / "SHA256SUMS").write_text(hashlib.sha256((output / "audit.json").read_bytes()).hexdigest() + "  audit.json\n")
        print(json.dumps({"stage": audit["stage"], "audit_id": audit_id, "port_before": audit.get("port_before"), "port_after": audit["port_after"]}))
        if not success: raise SystemExit(1)
    finally:
        os.close(lockfd)


if __name__ == "__main__":
    try: main()
    except Exception:
        print(json.dumps({"stage": "preflight-failed"}))
        raise SystemExit(1)
