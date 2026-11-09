"""Disabled R1 registration and durable rollback at the frozen R0 boundary.

The public installer replays this journal before Piece A's stop recovery.
BoundaryHandoff owns adoption, selection and activation authority.
All calls require the per-user installer lock, just like r0_stop.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from . import r0_stop
from .core import Version, absolute, current, read_json, separate_roots, version_id
from .service import SERVICE_SETTINGS, install_selection, systemd_quote

R0_LINUX = "astrid.service"
R1_LINUX = r0_stop.R1_UNIT
R0_MAC = "haus.astrid.runtime"
R1_MAC = r0_stop.R1_LABEL
R0_WINDOWS = "Astrid Runtime"
R1_WINDOWS = "Astrid Runtime R1"


def journal_path(service):
    return r0_stop._marker_path(service).with_name("astrid-r1-registration.json")


def retained_roots(service):
    value = read_json(absolute(journal_path(service)))
    return [absolute(value[k]) for k in ("program_root", "r0_data_root", "r1_data_root")] if value else []


def _regular(path):
    path = absolute(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"Expected a private regular file: {path}")
    return path


def _publication_alias(path, value):
    token = value.get("publication_id")
    if token is None:  # Older journals still require private, single-link files.
        return None
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("Invalid R1 publication identity; recovery intent retained.")
    return path.with_name(f".{path.name}.astrid-r1-{token}")


def _finish_publication(path, value, content, *, mode=0o600):
    """Unlink only the journal-bound publisher alias after an interrupted link.

    A name/prefix or matching bytes alone is not ownership evidence. There must
    be exactly two links to this inode, at the final name and the exact alias
    reserved by this transaction, with the publisher's owner/mode and bytes.
    """
    info = path.lstat()
    if stat.S_ISREG(info.st_mode) and info.st_nlink == 2:
        alias = _publication_alias(path, value)
        peer = alias.lstat() if alias is not None and os.path.lexists(alias) else None
        if (peer is not None and stat.S_ISREG(peer.st_mode)
                and (peer.st_dev, peer.st_ino, peer.st_nlink) == (info.st_dev, info.st_ino, 2)
                and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == mode
                and path.read_text(encoding="utf-8") == content):
            alias.unlink()
            r0_stop._sync_directory(path.parent)
    return _regular(path)  # Unknown aliases, extra links and changed files refuse.


def _publish(path, content, *, exclusive=True, mode=0o600, alias=None):
    """Publish complete bytes, including directory durability, never partial files."""
    path = absolute(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if alias is None:
        fd, name = tempfile.mkstemp(prefix=".astrid-r1-", dir=path.parent)
        temporary = Path(name)
    else:
        temporary = absolute(alias)
        if not exclusive or temporary.parent != path.parent:
            raise ValueError("Publication alias must be an exclusive sibling.")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path, follow_symlinks=False)
        else:
            _regular(path)
            os.replace(temporary, path)
        r0_stop._sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
        r0_stop._sync_directory(path.parent)


def _wrapper():
    return ('#!/bin/sh\nset -eu\n'
            'root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)\n'
            'version=$(cat "$root/current")\n'
            'case "$version" in ""|[!A-Za-z0-9]*|*[!A-Za-z0-9._-]*) exit 2;; esac\n'
            '[ ${#version} -le 64 ] || exit 2\n'
            'exec "$root/versions/$version/bin/runtime" "$@"\n')


def stage_runtime_launcher(version: Version, *, replace: bool = False):
    """Materialize the executable selected by the fixed launcher, in real staging.

    The script admits the handoff's records and their normalised direct form
    (R1 as ``data_root``, R0 kept as ``retained_r0_data_root``). ``replace``
    regenerates it for an already staged version before normalisation.
    """
    if version.release < 1 or version.managed_by is not None or os.name == "nt":
        return  # Windows adoption/extensionless launcher is not qualified.
    script = launcher_script(version)
    script_path, launcher_path = version.root / "bin/runtime.py", version.root / "bin/runtime"
    _publish(script_path, script, exclusive=not (replace and os.path.lexists(script_path)))
    launcher = ('#!/bin/sh\nset -eu\nexec ' + shlex.quote(str(version.python))
                + ' -X utf8 -I ' + shlex.quote(str(version.root / "bin/runtime.py")) + ' "$@"\n')
    _publish(launcher_path, launcher, mode=0o755,
             exclusive=not (replace and os.path.lexists(launcher_path)))


def launcher_script(version: Version) -> str:
    """The R1 launcher script this installer generates for ``version`` (pure)."""
    program = absolute(version.root).parent.parent
    return f'''import json, os, re, sys
from pathlib import Path
root = Path({str(program)!r})
version = {version.document()!r}
args = sys.argv[1:]
selection = json.loads(Path({str(install_selection(Path.home(), sys.platform))!r}).read_text(encoding="utf-8"))
marker = json.loads((root / "install.json").read_text(encoding="utf-8"))
legacy = "r1_data_root" in selection
data = selection.get("r1_data_root") if legacy else selection.get("data_root")
source = selection.get("data_root") if legacy else selection.get("retained_r0_data_root")
realm = selection.get("r1_realm_root") if legacy else selection.get("realm_root")
if (selection.get("owner") != "haus.astrid.runtime" or selection.get("program_root") != str(root)
        or not isinstance(data, str) or not Path(data).is_absolute()
        or not isinstance(source, str) or data != source + "-r1"
        or marker.get("owner") != "haus.astrid.runtime"
        # Marker and selection are normalised one after the other; either may
        # still be in the handoff's form after an interrupted normalisation.
        or not ((marker.get("data_root") == source and marker.get("r1_data_root") == data
                 and marker.get("identity") == version["identity"])
                or (marker.get("data_root") == data and marker.get("retained_r0_data_root") == source
                    and "r1_data_root" not in marker))
        or not isinstance(selection.get("realm_id"), str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{{0,63}}", selection["realm_id"])
        or realm != str(Path(data) / "runtime/realms" / selection["realm_id"])
        or (legacy and (not isinstance(selection.get("realm_root"), str)
                        or not Path(selection["realm_root"]).is_relative_to(Path(selection["data_root"]))))
        or selection.get("identity") != version["identity"]
        or selection.get("service_label") != {R1_LINUX if sys.platform == 'linux' else R1_MAC!r}
        or args != ["run", "--profile", "astrid", "--data-root", data]
        or (root / "current").read_text(encoding="utf-8").rstrip("\\n") != Path(version["root"]).name):
    raise SystemExit("R1 adoption and selection have not been committed; run the install line again.")
settings_path = root / "service-env.json"
settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {{}}
if (not isinstance(settings, dict) or set(settings) - set({sorted(SERVICE_SETTINGS)!r})
        or any(not isinstance(v, str) for v in settings.values())):
    raise SystemExit("Invalid Astrid service-env.json.")
for key in ("PYTHONPATH", "PYTHONHOME", "ASTRID_WORKSPACE_ENDPOINT", "ASTRID_WORKSPACE_CREDENTIAL",
            "ASTRID_RUNTIME_SOURCE_MANIFEST", "ASTRID_RUNTIME_DATA_ROOT", "ASTRID_PACKS_PATH",
            "ASTRID_INSTALLER_OPERATION_ID", "ASTRID_TIMELINE_SCHEMA_PYTHONPATH"):
    os.environ.pop(key, None)
os.environ.update(version["env"])
os.environ.update(settings)
os.environ["ASTRID_RUNTIME_DATA_ROOT"] = data
os.environ["ASTRID_RUNTIME_SOURCE_MANIFEST"] = version["profile"]
os.environ["ASTRID_INSTALL_PROGRAM_ROOT"] = str(root)
from astrid_runtime.cli import main
raise SystemExit(main([*args, "--source-manifest", version["profile"]]))
'''


class BoundaryRegistration:
    def __init__(self, service):
        self.service = service
        self.root = absolute(service.root)
        self.system = service.system
        if self.system not in {"linux", "darwin"}:
            raise ValueError("R1 boundary registration is not qualified on this platform.")
        self.uid = r0_stop._launchd_uid() if self.system == "darwin" else None
        self.r1_path = absolute(service.path.with_name(
            R1_LINUX if self.system == "linux" else R1_MAC + ".plist"))
        self.launcher = self.root / "bin/runtime"
        self.journal = absolute(journal_path(service))

    def _definition(self, data, log):
        args = [str(self.launcher), "run", "--profile", "astrid", "--data-root", str(data)]
        if self.system == "darwin":
            return plistlib.dumps({"Label": R1_MAC, "ProgramArguments": args,
                "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ExitTimeOut": 90,
                "StandardOutPath": str(log), "StandardErrorPath": str(log)}).decode()
        action = " ".join(systemd_quote(x).replace("$", "$$") for x in args)
        output = systemd_quote("append:" + str(log))
        return ("[Unit]\nDescription=Astrid Runtime R1\n\n[Service]\n"
                f"ExecStart={action}\nRestart=on-failure\nRestartSec=5\nKillMode=mixed\n"
                f"TimeoutStopSec=90\nStandardOutput={output}\nStandardError={output}\n"
                "\n[Install]\nWantedBy=default.target\n")

    def _run(self, argv):
        return r0_stop._run(self.service, argv)

    def _save(self, value, *, first=False):
        _publish(self.journal, json.dumps(value, sort_keys=True) + "\n", exclusive=first,
                 alias=_publication_alias(self.journal, value) if first else None)

    def _check_files(self, value):
        expected = {self.launcher: _wrapper(), self.r1_path: self._definition(
            Path(value["r1_data_root"]), absolute(value["log"]))}
        for path, content in expected.items():
            if not os.path.lexists(path):
                continue
            mode = 0o755 if path == self.launcher else 0o600
            if _finish_publication(path, value, content, mode=mode).read_text(encoding="utf-8") != content:
                raise ValueError(f"R1 registration file changed; recovery intent retained: {path}")
        return expected

    def _linux_state(self):
        argv = ["systemctl", "--user", "show", R1_LINUX, "--property=LoadState",
                "--property=ActiveState", "--property=FragmentPath", "--property=DropInPaths",
                "--property=UnitFileState"]
        try:
            output = self._run(argv)
        except subprocess.CalledProcessError as exc:
            output = str(exc.output or "")
        fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if fields.get("LoadState") == "not-found" and fields.get("ActiveState") == "inactive":
            return None
        if (fields.get("LoadState") != "loaded" or fields.get("FragmentPath") != str(self.r1_path)
                or fields.get("DropInPaths") != "" or not self.r1_path.is_file()):
            raise ValueError("R1 manager entry is unknown or changed; recovery intent retained.")
        return fields

    def _mac_state(self, value):
        target = f"gui/{self.uid}/{R1_MAC}"
        output = r0_stop._launchd_print(self.service, target)
        if output is None:
            return False
        args = plistlib.loads(self._definition(Path(value["r1_data_root"]), Path(value["log"])).encode())["ProgramArguments"]
        head, sep, rest = output.partition("\targuments = {\n")
        paths = re.findall(r"(?m)^[ \t]*path = ([^\r\n]+?)[ \t]*$", head)
        expected = "".join("\t\t" + arg + "\n" for arg in args) + "\t}\n"
        if (not output.startswith(target + " = {\n") or paths != [str(self.r1_path)]
                or not sep or not rest.startswith(expected) or not self.r1_path.is_file()):
            raise ValueError("R1 launchd action changed; recovery intent retained.")
        return True

    def prepare(self, version: Version, r0_data: Path, realm_id: str, r0_realm: Path):
        """Publish a disabled registration. This is never adoption or launch authority."""
        data, realm = absolute(r0_data), absolute(r0_realm)
        destination = absolute(str(data) + "-r1")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", realm_id):
            raise ValueError("Invalid realm identifier.")
        adopted_realm = destination / "runtime/realms" / realm_id
        for target in (self.root, self.r1_path, self.journal, self.service.log):
            if not separate_roots(data, absolute(target)) or not separate_roots(destination, absolute(target)):
                raise ValueError("R1 registration files must be outside both data roots.")
        if not realm.is_relative_to(data):
            raise ValueError("R0 realm is outside its data root.")
        if r0_stop._selected_realm(data) != (realm_id, realm):
            raise ValueError("Registration does not match R0's selected realm.")
        old = current(self.root)
        if old is None or old.release != 0 or version.release < 1 or version.managed_by is not None:
            raise ValueError("An R0 selection and staged direct R1 candidate are required.")
        if (absolute(version.root).parent != self.root / "versions"
                or Version.load(read_json(version.root / "version.json")) != version
                or not _regular(version.root / "bin/runtime").is_file()):
            raise ValueError("The staged R1 launcher is missing or changed.")
        intent = read_json(_regular(r0_stop._marker_path(self.service)))
        if any(intent.get(k) != v for k, v in {
                "version": 1, "system": self.system, "program_root": str(self.root),
                "data_root": str(data), "unit": str(self.service.path),
                "unit_sha256": r0_stop._unit_digest(self.service)}.items()):
            raise ValueError("R0 stop intent does not bind this registration.")
        # A marker records intent before stop. Prove the stop actually finished.
        if self.system == "linux":
            fields = self._run(["systemctl", "--user", "show", R0_LINUX,
                                "--property=ActiveState", "--property=UnitFileState"])
            values = dict(line.split("=", 1) for line in fields.splitlines() if "=" in line)
            if values.get("ActiveState") not in {"inactive", "failed"} or values.get("UnitFileState") != "disabled":
                raise ValueError("R0 must be disabled and stopped before R1 registration.")
        else:
            if r0_stop._launchd_print(self.service, f"gui/{self.uid}/{R0_MAC}") is not None:
                raise ValueError("R0 must be unloaded before R1 registration.")
            disabled = self._run(["launchctl", "print-disabled", f"gui/{self.uid}"])
            if not re.search(r'"haus\.astrid\.runtime"\s*=>\s*(?:true|disabled)\b', disabled):
                raise ValueError("R0 must be disabled before R1 registration.")
        r0_stop._wait_for_owner_lock(realm, 0, .2)
        r0_stop._wait_for_owner_lock(adopted_realm, 0, .2)
        r0_stop._require_no_r1(self.service)
        for path in (self.journal, self.launcher, self.r1_path):
            if os.path.lexists(path):
                raise FileExistsError(path)
        value = {"version": 1, "phase": "registering", "system": self.system,
                 "publication_id": secrets.token_hex(16),
                 "program_root": str(self.root), "r0_data_root": str(data),
                 "r1_data_root": str(destination), "r0_realm": str(realm), "realm_id": realm_id,
                 "log": str(absolute(self.service.log)), "r0_intent": intent,
                 "old_pointer": (self.root / "current").read_text(encoding="utf-8"),
                 "candidate": version.document()}
        self._save(value, first=True)  # Must precede every service/file mutation.
        if self.system == "darwin":
            # Disable before publishing a plist launchd could load at next login.
            self._run(["launchctl", "disable", f"gui/{self.uid}/{R1_MAC}"])
        _publish(self.launcher, _wrapper(), mode=0o755,
                 alias=_publication_alias(self.launcher, value))
        _publish(self.r1_path, self._definition(destination, Path(value["log"])),
                 alias=_publication_alias(self.r1_path, value))
        if self.system == "linux":
            self._run(["systemctl", "--user", "daemon-reload"])
            self._linux_state()
            self._run(["systemctl", "--user", "disable", "--now", R1_LINUX])
            state = self._linux_state()
            if state is None or state.get("UnitFileState") != "disabled" or state.get("ActiveState") not in {"inactive", "failed"}:
                raise ValueError("R1 did not remain disabled; recovery intent retained.")
        value["phase"] = "disabled"
        self._save(value)
        return self.journal

    def activate(self):
        """Enable only the journal-bound new label; C owns selection and health."""
        value = read_json(_regular(self.journal))
        if value.get("phase") != "disabled" or value.get("program_root") != str(self.root):
            raise ValueError("R1 registration is not ready for activation.")
        expected = self._check_files(value)
        if any(not path.exists() for path in expected):
            raise ValueError("R1 registration is incomplete.")
        if self.system == "linux":
            if self._linux_state() is None:
                raise ValueError("R1 manager entry is missing.")
            self._run(["systemctl", "--user", "enable", "--now", R1_LINUX])
            state = self._linux_state()
            if state is None or state.get("UnitFileState") != "enabled" or state.get("ActiveState") != "active":
                raise ValueError("R1 activation is uncertain; recovery intent retained.")
        else:
            target = f"gui/{self.uid}/{R1_MAC}"
            loaded = self._mac_state(value)
            self._run(["launchctl", "enable", target])
            if not loaded:
                self._run(["launchctl", "bootstrap", f"gui/{self.uid}", self.r1_path])
            if not self._mac_state(value):
                raise ValueError("R1 activation is uncertain; recovery intent retained.")

    def set_autostart(self, enabled):
        """Change sign-in startup of the active R1 service without restarting it."""
        value = read_json(_regular(self.journal))
        if value.get("program_root") != str(self.root):
            raise ValueError("R1 registration does not bind this installer.")
        if any(not path.exists() for path in self._check_files(value)):
            raise ValueError("R1 registration is incomplete.")
        if self.system == "linux":
            if self._linux_state() is None:
                raise ValueError("R1 manager entry is missing.")
            self._run(["systemctl", "--user", "enable" if enabled else "disable", R1_LINUX])
            state = self._linux_state()
            if state is None or (state.get("UnitFileState") == "enabled") != enabled:
                raise ValueError("R1 autostart change is uncertain; recovery intent retained.")
        else:
            target = f"gui/{self.uid}/{R1_MAC}"
            loaded = self._mac_state(value)
            self._run(["launchctl", "enable" if enabled else "disable", target])
            if enabled and not loaded:
                self._run(["launchctl", "bootstrap", f"gui/{self.uid}", self.r1_path])

    def recover(self, *, restore_selection=None):
        """Disable owned R1, wait for its lock, remove owned files, restore R0."""
        if not os.path.lexists(self.journal):
            return False
        # Parse a two-link journal provisionally: it binds its own alias. Full
        # intent/alias validation below must precede every recovery mutation.
        info = self.journal.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink not in {1, 2}:
            _regular(self.journal)
        value = read_json(self.journal)
        if (value.get("version") != 1 or value.get("system") != self.system
                or value.get("program_root") != str(self.root)
                or value.get("phase") not in {"registering", "disabled", "restoring_r0"}):
            raise ValueError("R1 registration intent does not match this installer.")
        data = absolute(value["r0_data_root"])
        destination = absolute(value["r1_data_root"])
        if destination != Path(str(data) + "-r1"):
            raise ValueError("R1 destination does not match the retained source.")
        version_id(value["realm_id"])
        intent = value.get("r0_intent")
        if (not absolute(value["r0_realm"]).is_relative_to(data) or not isinstance(intent, dict)
                or any(intent.get(k) != v for k, v in {
                    "version": 1, "system": self.system, "program_root": str(self.root),
                    "data_root": str(data), "unit": str(self.service.path)}.items())
                or not re.fullmatch(r"[0-9a-f]{64}", str(intent.get("unit_sha256", "")))):
            raise ValueError("R1 registration has no bound R0 recovery intent.")
        for path in (self.journal, self.root, self.launcher, self.r1_path, absolute(value["log"])):
            if not separate_roots(data, path) or not separate_roots(destination, path):
                raise ValueError("R1 recovery files overlap retained data.")
        _publication_alias(self.journal, value)  # Validate even a single-link journal.
        _finish_publication(self.journal, value, json.dumps(value, sort_keys=True) + "\n")
        expected = self._check_files(value)  # No mutation of a changed definition.
        if self.system == "linux":
            self._run(["systemctl", "--user", "daemon-reload"])
            if self._linux_state() is not None:
                self._run(["systemctl", "--user", "disable", "--now", R1_LINUX])
                state = self._linux_state()
                if state is None or state.get("UnitFileState") != "disabled" or state.get("ActiveState") not in {"inactive", "failed"}:
                    raise ValueError("R1 stop is uncertain; R0 was not restarted.")
        else:
            loaded = self._mac_state(value)
            if loaded or self.r1_path.exists():
                target = f"gui/{self.uid}/{R1_MAC}"
                self._run(["launchctl", "disable", target])
                if loaded:
                    self._run(["launchctl", "bootout", target])
                if self._mac_state(value):
                    raise ValueError("R1 remained loaded; R0 was not restarted.")
        r0_stop._wait_for_owner_lock(destination / "runtime/realms" / value["realm_id"], 90, .2)
        with r0_stop.owner_lock(destination / "runtime/realms" / value["realm_id"]):
            if restore_selection is not None:
                # C's replayable callback runs only AFTER the owned new service is
                # disabled/stopped and its data lock proves it has released custody.
                restore_selection()
            if (self.root / "current").read_text(encoding="utf-8") != value["old_pointer"]:
                raise ValueError("Version pointer changed; preserve both roots for handoff recovery.")
            selection = read_json(self.service.selection)
            marker = read_json(self.root / "install.json")
            if selection.get("r1_data_root") or marker.get("r1_data_root"):
                raise ValueError("R1 selection was committed; use the adoption handoff recovery.")
            self._check_files(value)
            for path in expected:
                if os.path.lexists(path):
                    path.unlink()
                    r0_stop._sync_directory(path.parent)
            if self.system == "linux":
                self._run(["systemctl", "--user", "daemon-reload"])
            r0_stop._require_no_r1(self.service)
            intent_path = r0_stop._marker_path(self.service)
            if os.path.lexists(intent_path):
                if read_json(_regular(intent_path)) != value["r0_intent"]:
                    raise ValueError("R0 recovery intent changed; R0 was not restarted.")
                value["phase"] = "restoring_r0"
                self._save(value)
                r0_stop.recover_r0(self.service)  # Revalidates R0 bytes and manager.
            elif value["phase"] != "restoring_r0":
                raise ValueError("R0 recovery intent is missing; preserve both roots.")
            else:
                # Crash after A removed its intent but before B removed its journal.
                if r0_stop._unit_digest(self.service) != value["r0_intent"]["unit_sha256"]:
                    raise ValueError("R0 service changed after recovery.")
                r0_stop._validate_manager(self.service, self.uid)
            self.journal.unlink()
            r0_stop._sync_directory(self.journal.parent)
        return True
