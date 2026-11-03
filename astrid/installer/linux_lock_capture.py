"""Opt-in, bounded observation before launching a Runtime upgrade on Linux."""

import json
import os
import stat
import time
from pathlib import Path


def _fact(value):
    return {"device": value.st_dev, "inode": value.st_ino,
            "uid": value.st_uid, "mode": stat.S_IMODE(value.st_mode),
            "type": stat.S_IFMT(value.st_mode)}


def capture(path):
    """Capture metadata only; a match is a snapshot, not an ownership grant."""
    result = {"schema": "installer-lock-parent-v1", "time_ns": time.time_ns(), "pid": os.getpid(),
              "ppid": os.getppid(), "path_role": "installer_lifetime_lock",
              "path": "unavailable", "fds": "unavailable", "locks": "unavailable",
              "pid_namespace": "unknown", "start_ticks": "unknown"}
    try:
        result["pid_namespace"] = os.stat("/proc/self/ns/pid").st_ino
        proc_stat = Path("/proc/self/stat").read_text()
        result["start_ticks"] = int(proc_stat[proc_stat.rfind(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        pass
    try:
        file_stat = path.lstat()
        result["path"] = _fact(file_stat)
    except OSError:
        return result
    try:
        matches = []
        with os.scandir("/proc/self/fd") as entries:
            for entry in entries:
                try:
                    fd = int(entry.name)
                    info = os.fstat(fd)
                except (ValueError, OSError):
                    continue
                if (info.st_dev, info.st_ino) == (file_stat.st_dev, file_stat.st_ino):
                    matches.append({"fd": fd, **_fact(info)})
                    if len(matches) == 16:
                        break
        result["fds"] = matches
    except OSError:
        pass
    try:
        with open("/proc/locks", "rb") as stream:
            data = stream.read(1048577)
        matches = []
        for line in data[:1048576].decode("ascii", "replace").splitlines():
            fields = line.split()
            if len(fields) < 8 or fields[1] != "FLOCK":
                continue
            try:
                major, minor, inode = fields[5].split(":")
                same = (int(major, 16), int(minor, 16), int(inode)) == (
                    os.major(file_stat.st_dev), os.minor(file_stat.st_dev), file_stat.st_ino)
                if same:
                    matches.append({"mode": fields[3], "kernel_pid": int(fields[4])})
                    if len(matches) == 16:
                        break
            except ValueError:
                continue
        result["locks"] = {"matches": matches, "truncated": len(data) > 1048576 or len(matches) == 16,
                           "pid_namespace_relation": "unknown"}
    except OSError:
        pass
    return result


def record(path, log, owner_only):
    """Never replace the upgrade outcome if the diagnostic is unavailable."""
    try:
        observation = capture(path)
        with log.open("a", encoding="utf-8") as stream:
            owner_only(stream.fileno())
            stream.write("installer-lock-parent " + json.dumps(observation, sort_keys=True) + "\n")
    except Exception:
        pass
