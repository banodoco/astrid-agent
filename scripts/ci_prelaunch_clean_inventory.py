#!/usr/bin/env python3
"""Read-only inventory of task-owned runner paths and test process identities."""
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

UID = 503


def measure(path, max_entries=1000000, max_seconds=120):
    result = {"present": False, "owner_uid": None, "kind": None, "bytes": 0,
              "files": 0, "directories": 0, "symlinks": 0, "foreign_entries": 0,
              "inaccessible": 0, "mounts_skipped": 0, "truncated": False}
    try: st = path.lstat()
    except FileNotFoundError: return result
    except OSError:
        result["inaccessible"] = 1
        return result
    result.update(present=True, owner_uid=st.st_uid,
                  kind="directory" if stat.S_ISDIR(st.st_mode) else "symlink" if stat.S_ISLNK(st.st_mode) else "file")
    if st.st_uid != UID or not stat.S_ISDIR(st.st_mode):
        if stat.S_ISREG(st.st_mode) and st.st_uid == UID: result["bytes"] = st.st_size; result["files"] = 1
        return result
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try: rootfd = os.open(str(path), flags)
    except OSError:
        result["inaccessible"] += 1
        return result
    root_device = st.st_dev
    pending = [rootfd]; seen = set(); count = 0; deadline = time.monotonic() + max_seconds
    try:
        while pending:
            fd = pending.pop()
            try:
                with os.scandir(fd) as entries:
                    for entry in entries:
                        count += 1
                        if count > max_entries or time.monotonic() > deadline:
                            result["truncated"] = True
                            break
                        try: item = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                        except OSError:
                            result["inaccessible"] += 1
                            continue
                        if item.st_uid != UID:
                            result["foreign_entries"] += 1
                            continue
                        if item.st_dev != root_device:
                            result["mounts_skipped"] += 1
                            continue
                        if stat.S_ISLNK(item.st_mode): result["symlinks"] += 1
                        elif stat.S_ISDIR(item.st_mode):
                            result["directories"] += 1
                            try:
                                child = os.open(entry.name, flags, dir_fd=fd)
                                actual = os.fstat(child)
                                if (actual.st_dev, actual.st_ino, actual.st_uid) != (item.st_dev, item.st_ino, item.st_uid):
                                    os.close(child); result["inaccessible"] += 1
                                else: pending.append(child)
                            except OSError: result["inaccessible"] += 1
                        elif stat.S_ISREG(item.st_mode):
                            result["files"] += 1
                            key = (item.st_dev, item.st_ino)
                            if key not in seen:
                                seen.add(key); result["bytes"] += item.st_size
            finally: os.close(fd)
            if result["truncated"]: break
    finally:
        for fd in pending: os.close(fd)
    return result


def task_processes():
    out = subprocess.run(["/bin/ps", "-U", str(UID), "-o", "uid=,pid=,ppid=,etime=,command="],
                         capture_output=True, text=True, check=True, timeout=20).stdout
    records = []
    for line in out.splitlines():
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(.*)$", line)
        if not match: continue
        uid, pid, parent, elapsed, command = match.groups()
        if int(uid) != UID: continue
        if ("/r25-option-b-runs/" in command or "r25-option-b-macos-sequence.sh" in command
                or "r25-snapshot-runner.cjs" in command): category = "r25-test"
        elif "astrid-r0-capture-src." in command: category = "r0-capture-test"
        elif "/r18-" in command or "/.astrid-r18/" in command: category = "r18-test"
        else: continue
        record = {"uid": int(uid), "pid": int(pid), "ppid": int(parent), "elapsed": elapsed, "category": category}
        try:
            detail = subprocess.run(["/bin/ps", "-p", pid, "-o", "uid=,lstart="], capture_output=True,
                                    text=True, timeout=5, check=True).stdout.strip().split(None, 1)
            if len(detail) == 2 and detail[0] == uid: record["observed_start"] = detail[1]
            else: record["observed_start"] = "unavailable"
        except Exception: record["observed_start"] = "unavailable"
        records.append(record)
    return records


def child_roots(parent, pattern):
    if parent.is_symlink() or not parent.is_dir() or parent.stat().st_uid != UID:
        return []
    return sorted((p for p in parent.iterdir() if re.fullmatch(pattern, p.name)), key=lambda p: p.name)


def main():
    if os.getuid() != UID or os.environ.get("RUNNER_NAME") != "hannah-mac" or sys.platform != "darwin":
        raise RuntimeError("Unexpected runner identity")
    home = Path(pwd.getpwuid(UID).pw_dir)
    temp = Path(os.environ["RUNNER_TEMP"])
    if (temp != home / "actions-runner/_work/_temp" or temp.resolve() != temp or temp.is_symlink()
            or not temp.is_dir() or temp.stat().st_uid != UID):
        raise RuntimeError("Unexpected runner temp")
    output = temp / "prelaunch-clean-inventory"; output.mkdir(mode=0o700)
    with open(os.environ["GITHUB_ENV"], "a") as fh: fh.write("PRELAUNCH_UPLOAD=" + str(output) + "\n")
    fixed = {"runner_checkout": home / "actions-runner/_work/astrid-agent",
             "runner_tool_python_31118": home / "actions-runner/_work/_tool/Python/3.11.18",
             "runner_prereqs": home / ".astrid-runner-prereqs-v1",
             "runner_restart_helpers": home / ".astrid-runner-restart",
             "smoke_bundle_cache": home / ".astrid-mac-bundle-cache",
             "r1fix_tasks": home / ".astrid-r1fix-stopped",
             "r1fix_audits": home / ".astrid-r1fix-clean-audits",
             "prelaunch_audits": home / ".astrid-prelaunch-clean-audits",
             "stop_audits": home / ".astrid-prelaunch-stop-audits"}
    groups = (("r1fix", home / ".astrid-r1fix-stopped", r"task\.[A-Za-z0-9_]+"),
              ("r18", home / ".astrid-r18", r"[0-9]+-[0-9]+"),
              ("r25", home / "r25-option-b-runs", r"run\.[A-Za-z0-9]+"),
              ("r0_capture", Path("/private/tmp"), r"astrid-r0-capture-src\.[A-Za-z0-9_]+"))
    inventory = {label: measure(path) for label, path in fixed.items()}
    for label, parent, pattern in groups:
        # /private/tmp is shared, so filter only explicitly task-named and UID-owned roots.
        if label == "r0_capture":
            roots = []
            for p in parent.glob("astrid-r0-capture-src.*"):
                try:
                    if re.fullmatch(pattern, p.name) and p.lstat().st_uid == UID: roots.append(p)
                except OSError: pass
        else:
            roots = child_roots(parent, pattern)
        for p in sorted(roots, key=lambda x: x.name): inventory[label + "/" + p.name] = measure(p)
    children = {}
    for label, parent in (("cache", home / ".astrid-mac-bundle-cache"), ("checkout", home / "actions-runner/_work/astrid-agent"), ("prelaunch_audits", home / ".astrid-prelaunch-clean-audits"), ("r1fix_audits", home / ".astrid-r1fix-clean-audits")):
        if parent.is_dir() and not parent.is_symlink() and parent.stat().st_uid==UID:
            children[label] = {p.name: measure(p) for p in sorted(parent.iterdir())}
    checkout=home / "actions-runner/_work/astrid-agent/astrid-agent"
    checkout_identity={}
    if checkout.is_dir() and not checkout.is_symlink() and checkout.stat().st_uid==UID:
        env={"PATH":"/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM":"1", "GIT_CONFIG_GLOBAL":"/dev/null", "GIT_CONFIG_SYSTEM":"/dev/null", "GIT_NO_REPLACE_OBJECTS":"1"}
        def git(*args): return subprocess.run(["/usr/bin/git","-C",str(checkout),"-c","core.hooksPath=/dev/null",*args],env=env,capture_output=True,text=True,timeout=30,check=True).stdout.strip()
        try:
            checkout_identity={"head":git("rev-parse","HEAD"), "origin_expected":git("config","--get","remote.origin.url") in ("https://github.com/banodoco/astrid-agent", "https://github.com/banodoco/astrid-agent.git"), "dirty":bool(git("status","--porcelain","--untracked-files=all"))}
        except Exception: checkout_identity={"status":"unverified"}
    result = {"checkout_identity": checkout_identity, "children": children, "schema": "prelaunch-clean-inventory/v1", "uid": UID, "runner": "hannah-mac",
              "read_only": True, "paths": inventory, "task_processes": task_processes(),
              "notes": ["Sizes are logical bytes, deduplicated per path; symlinks, mount points and foreign-owned directories are not followed.",
                        "A truncated or inaccessible count is incomplete; process observations are a point-in-time sample."]}
    file = output / "inventory.json"
    file.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    (output / "SHA256SUMS").write_text(hashlib.sha256(file.read_bytes()).hexdigest() + "  inventory.json\n")
    print(json.dumps({"status": "observed", "path_count": len(inventory), "task_process_count": len(result["task_processes"])}))


if __name__ == "__main__":
    try: main()
    except Exception:
        print(json.dumps({"status": "inventory-failed"}))
        raise SystemExit(1)
