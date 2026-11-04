"""Native job control for copy updates. Never use a PID as stop authority.

These observations bind the named job to its installed configuration. They are
not writer exclusion, and do not authenticate against a hostile same-user job
replacement in the native manager between its last query and control request.
"""
from __future__ import annotations

import hashlib
import os
import plistlib
import re
import stat
import subprocess

from runtime_protocol import platform

from .core import absolute

LABEL = "haus.astrid.runtime"
UNIT = "astrid.service"


def binding(path, *, optional=False):
    path = absolute(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        if optional:
            return {"path": str(path), "absent": True}
        raise
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.getuid() or before.st_mode & 0o022
                or before.st_size > 1024 * 1024):
            raise ValueError("The previous service configuration is not a private owned regular file.")
        raw = b""
        while chunk := os.read(descriptor, 65536):
            raw += chunk
            if len(raw) > 1024 * 1024:
                raise ValueError("The previous service configuration is too large.")
        keys = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
                "st_size", "st_mtime_ns", "st_ctime_ns")
        identity = lambda value: [getattr(value, key) for key in keys]
        if identity(before) != identity(os.fstat(descriptor)) or identity(before) != identity(path.lstat()):
            raise ValueError("The previous service configuration changed while being read.")
        return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                "stat": identity(before)}
    finally:
        os.close(descriptor)


def files(service):
    return [binding(service.path), binding(service.selection), binding(service.env_file, optional=True)]


def _selection(service, data):
    from .copy_update import selected_data
    record = service.recorded()
    if (record.get("owner") != LABEL or record.get("program_root") != str(service.root)
            or selected_data(record) != str(data) or record.get("uninstalled")
            or record.get("foreground")):
        raise ValueError("The previous service selection does not authorize native job control.")


def _systemd_file(service, data):
    # Parse only the canonical quoting emitted by Astrid. Reject all extra
    # lifecycle actions (including ExecStop) and configuration sections.
    from .service import systemd_quote
    text = service.path.read_text(encoding="utf-8")
    allowed = {"[Unit]": {"Description"}, "[Service]": {
        "ExecStart", "Environment", "Restart", "RestartSec", "KillMode", "TimeoutStopSec",
        "StandardOutput", "StandardError"}, "[Install]": {"WantedBy"}}
    section, seen, action = None, set(), None
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith("["):
            if line not in allowed or line in seen:
                raise ValueError("Unknown previous systemd configuration.")
            seen.add(line)
            section = line
            continue
        key, sep, value = line.partition("=")
        if not sep or section not in allowed or key not in allowed[section]:
            raise ValueError("Unknown previous systemd action.")
        if key == "ExecStart":
            if action is not None:
                raise ValueError("Ambiguous previous systemd action.")
            tokens = re.findall(r'"(?:\\.|[^"\\])*"', value)
            def decode(token):
                return re.sub(r'\\([\\"n])', lambda m: "\n" if m[1] == "n" else m[1], token[1:-1]).replace("%%", "%").replace("$$", "$")
            action = [decode(token) for token in tokens]
            if " ".join(systemd_quote(arg).replace("$", "$$") for arg in action) != value:
                raise ValueError("Unknown previous systemd quoting.")
    script = action[-1] if action and action[1:5] == ["-X", "utf8", "-I", "-c"] else None
    if not action or not service._known_launch_action(action, data, windows=False, script=script):
        raise ValueError("The previous systemd action does not belong to this installation.")


def _fields(value):
    result = {}
    for line in value.splitlines():
        key, sep, content = line.partition("=")
        if not sep or key in result:
            raise ValueError("Ambiguous systemd service identity.")
        result[key] = content
    return result


def observe(service, data):
    """Return JSON-safe identity and current state; no process identity probe."""
    if service.system not in {"linux", "darwin"}:
        raise ValueError("Safe installer quit is unavailable for this service manager.")
    _selection(service, data)
    before = files(service)
    if service.system == "darwin":
        target = f"gui/{platform.current_user()}/{LABEL}"
        try:
            value = service.run(["launchctl", "print", target])
        except subprocess.CalledProcessError as exc:
            if exc.returncode == 113 and any(message in str(exc.output) for message in (
                    "Could not find service", "Could not find specified service")):
                identity, state = None, "absent"
            else:
                raise ValueError("The previous launchd state cannot be established.") from exc
        else:
            loaded, observation = service._launchd_stop_identity(value, data)
            document = plistlib.loads(service.path.read_bytes())
            if list(loaded["arguments"]) != document["ProgramArguments"]:
                raise ValueError("The loaded launchd action differs from the exact plist.")
            identity = {"target": target, "path": loaded["path"], "program": loaded["program"],
                        "arguments": list(loaded["arguments"])}
            state = observation["state"]
        # Validate the on-disk identity even for an unloaded job, before any
        # later bootstrap. A missing job is never a reason to trust a label.
        document = plistlib.loads(service.path.read_bytes())
        action = document.get("ProgramArguments")
        script = action[-1] if isinstance(action, list) and action[1:5] == ["-X", "utf8", "-I", "-c"] else None
        if (document.get("Label") != LABEL or not isinstance(action, list)
                or not service._known_launch_action(action, data, windows=False, script=script)):
            raise ValueError("The previous launchd plist is not owned by this installation.")
    elif service.system == "linux" and service.systemd():
        _systemd_file(service, data)
        value = service.run(["systemctl", "--user", "show", UNIT,
            "--property=Id,FragmentPath,LoadState,ActiveState,SubState,Job,DropInPaths,Transient,NeedDaemonReload"])
        fields = _fields(value)
        if (fields.get("Id") != UNIT or fields.get("FragmentPath") != str(service.path)
                or fields.get("LoadState") != "loaded" or fields.get("Job") != ""
                or fields.get("DropInPaths") != "" or fields.get("Transient") != "no"
                or fields.get("NeedDaemonReload") != "no"):
            raise ValueError("The previous systemd unit identity cannot be established.")
        states = {("active", "running"): "running", ("inactive", "dead"): "absent"}
        state = states.get((fields.get("ActiveState"), fields.get("SubState")))
        if state is None:
            raise ValueError("The previous systemd service is transitioning or uncertain.")
        target = UNIT
        identity = {"target": target, "path": fields["FragmentPath"]}
    else:
        raise ValueError("Safe installer quit is unavailable for this service manager.")
    if files(service) != before:
        raise ValueError("The previous service configuration changed during validation.")
    return {"format": "astrid-copy-service/v1", "system": service.system,
            "target": target, "data_root": str(data), "files": before,
            "identity": identity, "state": state}


def stop(service, data, expected):
    latest = observe(service, data)
    if latest != expected:
        raise ValueError("The previous native service changed before installer quit.")
    if latest["state"] == "absent":
        return
    if service.system == "darwin":
        service.run(["launchctl", "bootout", latest["target"]])
    else:
        service.run(["systemctl", "--user", "stop", UNIT])
    stopped = observe(service, data)
    if stopped["state"] != "absent" or stopped["files"] != expected["files"]:
        raise ValueError("The previous native service did not remain stopped.")


def restore(service, data, expected):
    """Restart only an originally loaded/running, still authentic R0 job.

    After file restoration inode identity can differ. Require exact original
    bytes/modes/owner and a fresh native observation before starting anything.
    No stop intent/receipt is reconstructed from a saved PID.
    """
    if (expected.get("format") != "astrid-copy-service/v1"
            or expected.get("system") != service.system or expected.get("data_root") != str(data)
            or expected.get("target") != (f"gui/{platform.current_user()}/{LABEL}" if service.system == "darwin" else UNIT)):
        raise ValueError("The retained native service identity is invalid.")
    if expected["state"] == "absent":
        return
    latest = observe(service, data)
    def content(entries):
        return [{k: v for k, v in entry.items() if k != "stat"} |
                ({"mode": entry["stat"][2], "uid": entry["stat"][3], "gid": entry["stat"][4]} if "stat" in entry else {})
                for entry in entries]
    if content(latest["files"]) != content(expected["files"]):
        raise ValueError("The previous service files changed; refusing automatic R0 restart.")
    if latest["state"] == "running":
        if latest["identity"] != expected["identity"]:
            raise ValueError("A different native service appeared; refusing automatic R0 restart.")
        return
    if service.system == "darwin":
        # Loaded but idle jobs retain an exact identity; bootstrap only an
        # absent job and let RunAtLoad/KeepAlive supply its original behavior.
        if latest["identity"] is not None:
            raise ValueError("An idle launchd job appeared; refusing automatic R0 restart.")
        service.run(["launchctl", "bootstrap", f"gui/{platform.current_user()}", service.path])
    else:
        service.run(["systemctl", "--user", "start", UNIT])
    restored = observe(service, data)
    if (restored["state"] != "running" or restored["identity"] != expected["identity"]
            or content(restored["files"]) != content(expected["files"])):
        raise ValueError("R0 service restoration could not be verified; both workspace roots are kept.")
