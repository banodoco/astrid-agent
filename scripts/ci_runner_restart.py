#!/usr/bin/env python3
"""One-shot dedicated runner restart during an externally coordinated quiet window."""
import hashlib
import json
import os
import plistlib
import pwd
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

UID = 503
LABEL_SHA = "3f6fe4220199df84d5797dbb0833ff2f0cd5b5cef4a6bbd1bbe37ca84508102e"
PLIST_SHA = "12c62b1401ad71b66c240262fb6687c81d334121021ea0d6ba04ac8bc6ecf278"


def owned(path, directory=False):
    st = path.lstat()
    kind = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
    if not kind or st.st_uid != UID or path != path.resolve():
        raise RuntimeError("Unexpected runner-owned path")


def configured_identity(home):
    runner = home / "actions-runner"
    owned(home, True); owned(runner, True)
    service = runner / ".service"
    owned(service)
    path = Path(service.read_text().strip())
    if path.parent != home / "Library/LaunchAgents" or path.suffix != ".plist":
        raise RuntimeError("Unexpected service record")
    owned(path)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != PLIST_SHA:
        raise RuntimeError("Service plist changed")
    plist = plistlib.loads(data)
    label = plist.get("Label")
    if (not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", label)
            or hashlib.sha256(label.encode()).hexdigest() != LABEL_SHA
            or path.name != label + ".plist"
            or str(runner / "runsvc.sh") not in plist.get("ProgramArguments", [])):
        raise RuntimeError("Service identity changed")
    return "gui/503/" + label, path


def identity(home):
    target, _ = configured_identity(home)
    runner = home / "actions-runner"
    loaded = subprocess.run(["/bin/launchctl", "print", target], capture_output=True, text=True, timeout=15, check=True)
    if str(runner / "runsvc.sh") not in loaded.stdout:
        raise RuntimeError("Loaded service command changed")
    return target


def workers_exist():
    output = subprocess.run(["/bin/ps", "-axo", "uid=,command="], capture_output=True, text=True, timeout=10, check=True).stdout
    return any(re.match(r"^\s*503\s", line) and "/Runner.Worker" in line for line in output.splitlines())


def listener_pid(home):
    executable = str(home / "actions-runner/bin/Runner.Listener")
    output = subprocess.run(["/bin/ps", "-axo", "uid=,pid=,command="], capture_output=True,
                            text=True, timeout=10, check=True).stdout
    pids = []
    for line in output.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[0] == "503" and fields[2].split(None, 1)[0] == executable:
            pids.append(int(fields[1]))
    if len(pids) != 1:
        return None
    try:
        os.kill(pids[0], 0)
    except OSError:
        return None
    return pids[0]


def missing_service(result, label):
    expected = f'Could not find service "{label}" in domain for user gui: {UID}'
    actual = result.stderr.strip().replace("\r\n", "\n")
    return result.returncode == 113 and actual in {expected, "Bad request.\n" + expected}


def recover(home):
    target, plist = configured_identity(home)
    probe = subprocess.run(["/bin/launchctl", "print", target], capture_output=True, text=True, timeout=15)
    if probe.returncode != 0:
        # Bootstrapping this exact pinned plist is safe only if it is absent.
        if not missing_service(probe, target.rsplit("/", 1)[-1]):
            raise RuntimeError("Cannot prove service is absent")
        subprocess.run(["/bin/launchctl", "bootstrap", "gui/503", str(plist)], capture_output=True, text=True, timeout=30, check=True)
    else:
        if str(home / "actions-runner/runsvc.sh") not in probe.stdout:
            raise RuntimeError("Loaded service command changed")
    subprocess.run(["/bin/launchctl", "kickstart", target], capture_output=True, text=True, timeout=30, check=True)


def remove_own_helper(run_dir):
    label = "haus.astrid.runner-restart." + run_dir.name
    target = "gui/503/" + label
    result = subprocess.run(["/bin/launchctl", "print", target], capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        if missing_service(result, label):
            return "already-absent"
        raise RuntimeError("Cannot inspect helper label")
    if ("/usr/bin/python3" not in result.stdout
            or str(run_dir / "helper.py") not in result.stdout
            or re.search(r"^\s*pid = [0-9]+\s*$", result.stdout, re.MULTILINE)):
        raise RuntimeError("Helper label is mismatched or still running")
    subprocess.run(["/bin/launchctl", "remove", label], capture_output=True, text=True, timeout=15, check=True)
    return "removed"


def restart(home, timeout=1200):
    """Caller must coordinate a scheduling quiet window; polling does not enforce it."""
    identity(home)
    if not workers_exist():
        raise RuntimeError("Scheduling job was not observed")
    deadline = time.monotonic() + timeout
    idle_since = None
    while time.monotonic() < deadline:
        if workers_exist():
            idle_since = None
        elif idle_since is None:
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since >= 15:
            break
        time.sleep(2)
    else:
        raise RuntimeError("Timed out waiting for runner worker")
    target = identity(home)
    if workers_exist():
        raise RuntimeError("Runner worker appeared")
    before = listener_pid(home)
    if before is None:
        raise RuntimeError("Expected one running listener")
    try:
        subprocess.run(["/bin/launchctl", "kickstart", "-k", target], capture_output=True, text=True, timeout=30, check=True)
        wait_until = time.monotonic() + 45
        while time.monotonic() < wait_until:
            after = listener_pid(home)
            if after is not None and after != before:
                identity(home)
                return {"status": "restart-verified", "old_listener_pid": before,
                        "new_listener_pid": after, "coordinated_quiet_window_required": True}
            time.sleep(2)
        raise RuntimeError("New listener not observed")
    except Exception:
        # Recovery is restricted to the same already-pinned runner plist and GUI domain.
        recover(home)
        raise


def main():
    if os.getuid() != UID:
        raise RuntimeError("Unexpected runner UID")
    home = Path(pwd.getpwuid(UID).pw_dir)
    run_dir = Path(__file__).parent
    owned(home, True); owned(run_dir.parent, True); owned(run_dir, True); owned(Path(__file__))
    if run_dir.parent != home / ".astrid-runner-restart" or not re.fullmatch(r"[0-9]+-[0-9]+", run_dir.name):
        raise RuntimeError("Unexpected helper location")
    if sys.argv[1:] == ["schedule"]:
        identity(home)
        if not workers_exist():
            raise RuntimeError("Expected current scheduling job")
        label = "haus.astrid.runner-restart." + run_dir.name
        subprocess.run(["/bin/launchctl", "submit", "-l", label, "--", "/usr/bin/python3", str(Path(__file__)), "helper"], capture_output=True, text=True, timeout=20, check=True)
        print(json.dumps({"status": "scheduled", "quiet_window_seconds": 120, "helper_label": label}))
    elif sys.argv[1:] == ["verify"]:
        identity(home)
        report = run_dir / "result.json"
        owned(report)
        result = json.loads(report.read_bytes())
        if result.get("status") != "restart-verified":
            raise RuntimeError("Helper did not verify restart")
        current = listener_pid(home)
        if current != result.get("new_listener_pid"):
            raise RuntimeError("Verified listener changed or is absent")
        cleanup = remove_own_helper(run_dir)
        print(json.dumps({"status": "runner-online", "old_listener_pid": result["old_listener_pid"],
                          "new_listener_pid": current, "helper_cleanup": cleanup}))
    elif sys.argv[1:] == ["helper"]:
        report = run_dir / "result.json"
        try:
            result = restart(home)
        except Exception:
            result = {"status": "restart-refused", "reason": "Identity, worker, timeout, or restart check failed"}
        fd = os.open(str(report), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(result, out); out.write("\n"); out.flush(); os.fsync(out.fileno())
    else:
        raise RuntimeError("Unknown restart phase")


if __name__ == "__main__":
    try: main()
    except Exception:
        print(json.dumps({"status": "refused", "reason": "Runner restart helper identity or scheduling check failed"}))
        raise SystemExit(1)
