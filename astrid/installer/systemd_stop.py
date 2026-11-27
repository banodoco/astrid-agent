"""Read-only inspection of one authenticated Linux systemd invocation.

The private manager connection and its socket-derived native peer stay alive
for the entire operation. GetUnitByInvocationID selects an invocation-specific
object (present in systemd v252). That pins the invocation, but daemon-reload
can still replace its loaded stop hooks/policy. Public systemd has no atomic
compare-definition-and-stop operation. Consequently the production live route
refuses before Runtime's first write; there is no override or PID/label
fallback. Only independently observed quiescence can continue an R15 update.
Runtime's generic fenced capability interface is reserved for a supervisor
that can actually provide the required atomic authority contract.
"""
from __future__ import annotations

import ctypes as C
import ctypes.util
import json
import os
import re
import socket
import stat
from contextlib import contextmanager
from pathlib import Path

from runtime_protocol import platform

MANAGER = "/org/freedesktop/systemd1"
UNIT = "org.freedesktop.systemd1.Unit"
SERVICE = "org.freedesktop.systemd1.Service"
PROPERTIES = "org.freedesktop.DBus.Properties"
STOP_SECONDS = 90
STOP_RELATIONSHIPS = ("OnFailure", "OnSuccess", "PropagatesStopTo", "ConsistsOf", "BoundBy", "RequiredBy", "RequisiteOf")
STOP_ACTIONS = ("FailureAction", "SuccessAction", "JobTimeoutAction", "StartLimitAction")
WARM_REFUSAL = (
    "Legacy systemd warm update is unqualified: the manager cannot atomically fence "
    "both the old invocation and its loaded stop definition. Let the old Runtime "
    "exit through its normal lifecycle, then retry the update after quiescence."
)


class SystemdStopError(RuntimeError):
    """The selected systemd invocation cannot be safely stopped."""
    def __init__(self, message):
        super().__init__(message)
        self.details = {"safe": False}


class _BusError(SystemdStopError):
    def __init__(self, name, message):
        self.name = name
        super().__init__(f"systemd invocation request failed ({name}): {message}")


class _Error(C.Structure):
    _fields_ = [("name", C.c_char_p), ("message", C.c_char_p), ("need_free", C.c_int)]


class _ManagerBus:
    """Small synchronous sd-bus client on the manager's private Unix socket.

    No environment-selected session bus, subprocess, reconnect, proxy or
    user-supplied address. Reuse the process-binding provider for native peer
    retention; older kernels/providers that cannot retain it safely refuse.
    """
    def __init__(self):
        self.bus = C.c_void_p()
        self.peer = None
        self.library = None
        try:
            library = ctypes.util.find_library("systemd")
            if not library:
                raise SystemdStopError("libsystemd is unavailable; legacy warm upgrade is deferred.")
            self.library = C.CDLL(library)
            signatures = {
                "sd_bus_new": ([C.POINTER(C.c_void_p)], C.c_int),
                "sd_bus_set_address": ([C.c_void_p, C.c_char_p], C.c_int),
                "sd_bus_start": ([C.c_void_p], C.c_int),
                "sd_bus_get_fd": ([C.c_void_p], C.c_int),
                "sd_bus_flush_close_unref": ([C.c_void_p], C.c_void_p),
                "sd_bus_message_new_method_call": ([C.c_void_p, C.POINTER(C.c_void_p),
                    C.c_char_p, C.c_char_p, C.c_char_p, C.c_char_p], C.c_int),
                "sd_bus_message_append_basic": ([C.c_void_p, C.c_char, C.c_void_p], C.c_int),
                "sd_bus_message_append_array": ([C.c_void_p, C.c_char, C.c_void_p, C.c_size_t], C.c_int),
                "sd_bus_call": ([C.c_void_p, C.c_void_p, C.c_uint64, C.POINTER(_Error),
                                  C.POINTER(C.c_void_p)], C.c_int),
                "sd_bus_message_peek_type": ([C.c_void_p, C.POINTER(C.c_char),
                                               C.POINTER(C.c_char_p)], C.c_int),
                "sd_bus_message_read_basic": ([C.c_void_p, C.c_char, C.c_void_p], C.c_int),
                "sd_bus_message_enter_container": ([C.c_void_p, C.c_char, C.c_char_p], C.c_int),
                "sd_bus_message_exit_container": ([C.c_void_p], C.c_int),
                "sd_bus_message_unref": ([C.c_void_p], C.c_void_p),
                "sd_bus_error_free": ([C.POINTER(_Error)], None),
            }
            for name, (args, result) in signatures.items():
                function = getattr(self.library, name)
                function.argtypes, function.restype = args, result
            uid = os.getuid()
            address = Path(f"/run/user/{uid}/systemd/private")
            st = address.lstat()
            if not stat.S_ISSOCK(st.st_mode) or st.st_uid != uid:
                raise SystemdStopError("The private user systemd endpoint is not owned by this user.")
            self._check(self.library.sd_bus_new(C.byref(self.bus)))
            self._check(self.library.sd_bus_set_address(self.bus, f"unix:path={address}".encode()))
            self._check(self.library.sd_bus_start(self.bus))
            fd = self._check(self.library.sd_bus_get_fd(self.bus))
            from runtime_protocol import process_binding
            with socket.socket(fileno=os.dup(fd)) as connection:
                self.peer = process_binding.retain_peer(connection)
            self.identity = dict(self.peer.identity)
            self._verify_peer()
            self.call(MANAGER, "org.freedesktop.DBus.Peer", "Ping")
        except Exception as exc:
            self.close()
            if isinstance(exc, SystemdStopError):
                raise
            raise SystemdStopError(
                "The independently authenticated user systemd manager is unavailable; "
                "legacy warm update is refused."
            ) from exc
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(result):
        if result < 0:
            raise SystemdStopError(f"systemd connection unavailable: {os.strerror(-result)}")
        return result

    def _verify_peer(self):
        if self.peer is None:
            raise SystemdStopError("The systemd manager connection is closed.")
        self.peer.verify(self.identity)
        if self.identity["uid"] != os.getuid():
            raise SystemdStopError("The systemd manager belongs to another user.")
        pid = self.identity["pid"]
        executable = Path(f"/proc/{pid}/exe")
        st = executable.stat()
        trusted = False
        for candidate in (Path("/usr/lib/systemd/systemd"), Path("/lib/systemd/systemd")):
            try:
                expected = candidate.stat()
            except FileNotFoundError:
                continue
            if ((st.st_dev, st.st_ino) == (expected.st_dev, expected.st_ino)
                    and stat.S_ISREG(st.st_mode) and st.st_uid == 0 and not st.st_mode & 0o022):
                trusted = True
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if not trusted or b"--user" not in command:
            raise SystemdStopError("The private peer is not the independently verified user systemd manager.")
        self.peer.verify(self.identity)

    def _read(self, message, *, depth=0):
        if depth > 8:
            raise SystemdStopError("Unexpected nested systemd reply.")
        code, contents = C.c_char(), C.c_char_p()
        if self._check(self.library.sd_bus_message_peek_type(message, C.byref(code), C.byref(contents))) == 0:
            raise SystemdStopError("Incomplete systemd reply.")
        kind = code.value
        if kind in (b"a", b"r", b"v", b"e"):
            self._check(self.library.sd_bus_message_enter_container(message, kind, contents.value))
            values = []
            while self._check(self.library.sd_bus_message_peek_type(message, C.byref(code), C.byref(contents))):
                if len(values) >= 4096:
                    raise SystemdStopError("Oversized systemd reply.")
                values.append(self._read(message, depth=depth + 1))
            self._check(self.library.sd_bus_message_exit_container(message))
            if kind == b"v":
                if len(values) != 1:
                    raise SystemdStopError("Malformed systemd property.")
                return values[0]
            return values
        types = {b"s": C.c_char_p, b"o": C.c_char_p, b"g": C.c_char_p,
                 b"b": C.c_int, b"y": C.c_uint8, b"u": C.c_uint32,
                 b"i": C.c_int32, b"t": C.c_uint64, b"x": C.c_int64}
        if kind not in types:
            raise SystemdStopError("Unexpected systemd reply type.")
        value = types[kind]()
        self._check(self.library.sd_bus_message_read_basic(message, kind, C.byref(value)))
        raw = value.value
        return raw.decode("utf-8") if kind in (b"s", b"o", b"g") else bool(raw) if kind == b"b" else raw

    def call(self, path, interface, method, *args, timeout=5.0):
        self._verify_peer()
        request, reply, error = C.c_void_p(), C.c_void_p(), _Error()
        try:
            self._check(self.library.sd_bus_message_new_method_call(
                self.bus, C.byref(request), None, path.encode(), interface.encode(), method.encode()))
            for arg in args:
                if isinstance(arg, str):
                    encoded = C.c_char_p(arg.encode())
                    self._check(self.library.sd_bus_message_append_basic(request, b"s", encoded))
                elif isinstance(arg, bytes):
                    buffer = C.create_string_buffer(arg)
                    self._check(self.library.sd_bus_message_append_array(request, b"y", buffer, len(arg)))
                else:
                    raise TypeError("Unsupported systemd request argument")
            result = self.library.sd_bus_call(self.bus, request, int(timeout * 1_000_000),
                                               C.byref(error), C.byref(reply))
            if result < 0:
                raise _BusError((error.name or b"unavailable").decode(),
                                (error.message or os.strerror(-result).encode()).decode())
            self._verify_peer()
            if method == "Ping":
                return None
            return self._read(reply)
        finally:
            if request:
                self.library.sd_bus_message_unref(request)
            if reply:
                self.library.sd_bus_message_unref(reply)
            self.library.sd_bus_error_free(C.byref(error))

    def property(self, path, interface, name):
        return self.call(path, PROPERTIES, "Get", interface, name)

    def close(self):
        try:
            if self.bus and self.library is not None:
                # Do not flush a possibly blocked peer during unwinding.
                self.library.sd_bus_close.argtypes = [C.c_void_p]
                self.library.sd_bus_close(self.bus)
                self.library.sd_bus_unref.argtypes = [C.c_void_p]
                self.library.sd_bus_unref.restype = C.c_void_p
                self.library.sd_bus_unref(self.bus)
                self.bus = C.c_void_p()
        finally:
            if self.peer is not None:
                self.peer.close()
                self.peer = None


def _file_snapshot(path):
    st = path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
            or st.st_mode & 0o022):
        raise SystemdStopError("The systemd unit file is not privately owned by this installation.")
    raw = path.read_bytes()
    after = path.lstat()
    fields = ("st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    before_key = tuple(getattr(st, key) for key in fields)
    if before_key != tuple(getattr(after, key) for key in fields):
        raise SystemdStopError("The systemd unit changed during observation.")
    return before_key, raw


class SystemdInvocationStop:
    """Authenticated observation; live stop deliberately supplies no authority."""
    def __init__(self, service, version, data, owner, bus, *, recovery=None):
        from .core import current

        self.service, self.version, self.data, self.bus = service, version, data, bus
        self.owner = dict(owner) if owner is not None else None
        self.recovery = None
        self.retained_files = {}
        self.loaded_versions = (version,)
        self.selection = service.recorded()
        if service.handed_off:
            # The R1 indirection has a different loaded-action contract. Do not
            # infer it from an installer label or silently broaden this repair.
            raise SystemdStopError("Legacy handed-off systemd invocation is not qualified; defer until quiescent.")
        if current(service.root) != version:
            raise SystemdStopError("The selected version changed before systemd admission.")
        self.file = _file_snapshot(service.path)
        path = bus.call(MANAGER, "org.freedesktop.systemd1.Manager", "GetUnit", "astrid.service")
        invocation = bus.property(path, UNIT, "InvocationID")
        if owner is None and invocation == []:
            invocation = [0] * 16
        if (not isinstance(invocation, list) or len(invocation) != 16
                or any(type(value) is not int or not 0 <= value <= 255 for value in invocation)
                or (owner is not None and not any(invocation))):
            raise SystemdStopError("The service has no verifiable invocation identity.")
        self.invocation = bytes(invocation)
        self.path = path
        if owner is not None:
            self.path = bus.call(MANAGER, "org.freedesktop.systemd1.Manager", "GetUnitByInvocationID", self.invocation)
            label = self.invocation.hex()
            # systemd bus_label_escape also escapes a leading digit.
            if label[0].isdigit():
                label = f"_{ord(label[0]):02x}" + label[1:]
            if self.path != f"{MANAGER}/unit/{label}":
                raise SystemdStopError("The manager did not return an invocation-specific unit path.")
        self.snapshot = self._snapshot()
        # A stopped manager may still have the candidate definition loaded
        # after C2 reselected old. Pin one exact retained command, not whichever
        # version happens to match on a later read. This is recognition only.
        executable, argv, _ignore = self.snapshot["ExecStart"]
        script = argv[-1] if isinstance(argv, list) and len(argv) >= 6 and argv[1:5] == ["-X", "utf8", "-I", "-c"] else None
        if (recovery is not None and (executable != str(version.python)
                or not service._known_launch_action(argv, data, windows=False, script=script, version=version))):
            self._bind_recovery(recovery)
        matches = [item for item in self.loaded_versions
                   if executable == str(item.python)
                   and service._known_launch_action(argv, data, windows=False, script=script, version=item)]
        if len(matches) != 1:
            raise SystemdStopError("The loaded systemd action is not the exact selected recovery version.")
        self.loaded_version = matches[0]
        self.enabled_state = self.snapshot["UnitFileState"] == "enabled"
        self.stopped = False
        self.validate(owner)

    def _bind_recovery(self, recovery):
        from .core import Version, absolute, current, version_id

        transaction, recovering = recovery
        path = self.service.root / "update-transaction.json"
        snapshot = _file_snapshot(path)
        if (recovering is not True or self.owner is not None
                or json.loads(snapshot[1]) != transaction
                or transaction.get("format") != "astrid-install-update/v1"
                or not re.fullmatch(r"[0-9a-f]{32}", transaction.get("operation_id", ""))
                or transaction.get("transaction_kind") not in {None, "migration"}
                or transaction.get("step") not in {"recovering", "rollback_selected"}
                or absolute(transaction["data_root"]) != self.data
                or Version.load(transaction["old"]) != self.version
                or current(self.service.root) != self.version
                or self.version.release != 15):
            raise SystemdStopError("The cold recovery context does not match the selected installation.")
        realm = transaction["realm"]
        if not isinstance(realm.get("realm_id"), str) or not realm["realm_id"]:
            raise SystemdStopError("The cold recovery realm identity is invalid.")
        absolute(realm["realm_root"])
        candidate = Version.load(transaction["candidate"])
        if self.version.managed_by is not None or candidate.managed_by is not None:
            raise SystemdStopError("The cold recovery candidate belongs to another installation manager.")
        for item in (self.version, candidate):
            folder = self.service.root / "versions" / version_id(item.root.name)
            if (item.root != absolute(folder) or folder.is_symlink()
                    or not item.python.is_absolute() or not item.python.is_relative_to(folder)
                    or not item.profile.is_absolute() or not item.profile.is_relative_to(folder)
                    or not item.python.is_file() or item.profile.is_symlink() or not item.profile.is_file()):
                raise SystemdStopError("The cold recovery version is not retained by this installation.")
            retained = _file_snapshot(folder / "version.json")
            document = json.loads(retained[1])
            if (Version.load(document) != item
                    or item != self.version and document != item.document()):
                raise SystemdStopError("The cold recovery version metadata changed.")
            self.retained_files[folder / "version.json"] = retained
            self.retained_files[item.profile] = _file_snapshot(item.profile)
        self.recovery = path, snapshot
        self.loaded_versions = (self.version,) if candidate == self.version else (self.version, candidate)

    def _validate_recovery(self):
        if self.recovery is not None:
            path, snapshot = self.recovery
            if _file_snapshot(path) != snapshot:
                raise SystemdStopError("The cold recovery journal changed before admission.")
            if any(_file_snapshot(path) != value for path, value in self.retained_files.items()):
                raise SystemdStopError("The retained cold recovery inputs changed before admission.")

    def _snapshot(self, path=None):
        properties = {
            UNIT: ("Id", "FragmentPath", "DropInPaths", "LoadState", "InvocationID", "UnitFileState", "ActiveState", "Job",
                   "NeedDaemonReload", "StopWhenUnneeded", "RefuseManualStop", *STOP_RELATIONSHIPS, *STOP_ACTIONS),
            SERVICE: ("MainPID", "ExecStart", "ExecStop", "ExecStopPost", "KillMode", "KillSignal",
                      "FinalKillSignal", "SendSIGKILL", "TimeoutStopUSec", "TimeoutStopFailureMode"),
        }
        result = {key: self.bus.property(path or self.path, interface, key)
                  for interface, names in properties.items() for key in names}
        # ExecStart's execution timestamps/status change at exit, but its
        # executable, argv and ignore-failure flag must stay byte-for-byte.
        starts = result["ExecStart"]
        if not isinstance(starts, list) or len(starts) != 1 or len(starts[0]) < 3:
            raise SystemdStopError("The loaded service has an unknown start action.")
        result["ExecStart"] = starts[0][:3]
        return result

    def validate(self, owner):
        from .core import current
        from .service import LABEL

        self._validate_recovery()
        if owner != self.owner or self.stopped:
            raise SystemdStopError("The selected Runtime owner changed before systemd stop.")
        if (self.service.recorded() != self.selection or self.selection.get("owner") != LABEL
                or self.selection.get("program_root") != str(self.service.root)
                or self.selection.get("data_root") != str(self.data)
                or current(self.service.root) != self.version or _file_snapshot(self.service.path) != self.file):
            raise SystemdStopError("The selected installation or systemd unit changed before stop.")
        value = self._snapshot()
        if value != self.snapshot:
            raise SystemdStopError("The loaded systemd invocation changed before stop.")
        executable, argv, ignore_failure = value["ExecStart"]
        script = argv[-1] if isinstance(argv, list) and len(argv) >= 6 and argv[1:5] == ["-X", "utf8", "-I", "-c"] else None
        if (value["Id"] != "astrid.service" or value["FragmentPath"] != str(self.service.path)
                or value["DropInPaths"] or value["LoadState"] != "loaded"
                or value["NeedDaemonReload"] or value["StopWhenUnneeded"] or value["RefuseManualStop"]
                or any(value[key] for key in STOP_RELATIONSHIPS)
                or any(value[key] != "none" for key in STOP_ACTIONS)
                or value["InvocationID"] not in ([list(self.invocation), []] if owner is None else [list(self.invocation)])
                or value["MainPID"] != (owner["pid"] if owner is not None else 0)
                or value["ActiveState"] not in ({"active"} if owner is not None else {"inactive", "failed"})
                or value["Job"] != [0, "/"]
                or value["UnitFileState"] not in {"enabled", "disabled"}
                or not isinstance(argv, list) or not argv or executable != argv[0]
                or executable != str(self.loaded_version.python) or ignore_failure
                or not self.service._known_launch_action(argv, self.data, windows=False, script=script,
                                                         version=self.loaded_version)
                or value["ExecStop"] or value["ExecStopPost"]
                or value["KillMode"] != "mixed" or value["KillSignal"] != 15
                or value["FinalKillSignal"] != 9 or value["SendSIGKILL"] is not True
                or value["TimeoutStopFailureMode"] != "terminate"
                or type(value["TimeoutStopUSec"]) is not int
                or not 0 < value["TimeoutStopUSec"] <= STOP_SECONDS * 1_000_000):
            raise SystemdStopError("The loaded systemd action does not match the bounded owned Runtime invocation.")
        if owner is not None:
            observed = platform.process_identity(owner["pid"])
            if (observed is None or observed.get("uid") != os.getuid()
                    or platform.process_birth_matches(owner["process_birth_id"], observed.get("birth_id", "")) is not True):
                raise SystemdStopError("The systemd MainPID does not match the authenticated Runtime incarnation.")
        # Query the same immutable object again after file/process checks.
        if self.bus.property(self.path, UNIT, "InvocationID") != self.snapshot["InvocationID"]:
            raise SystemdStopError("The systemd invocation changed at the stop fence.")

    def stop(self, owner):
        # GetUnitByInvocationID does not freeze a unit's loaded definition.
        # Do not expose an apparently successful Stop/Kill path behind a flag,
        # version check or test-only qualification bit.
        raise SystemdStopError(WARM_REFUSAL)

    def assert_stopped(self):
        self._validate_recovery()
        # Label lookup here is a read-only replacement/enablement check. Never
        # send an action to this mutable path, even after the old owner exited.
        path = self.bus.call(MANAGER, "org.freedesktop.systemd1.Manager", "GetUnit", "astrid.service")
        invocation = self.bus.property(path, UNIT, "InvocationID")
        observed = self._snapshot(path)
        dynamic = {"InvocationID", "MainPID", "ActiveState", "Job"}
        if any(observed[key] != value for key, value in self.snapshot.items() if key not in dynamic):
            raise SystemdStopError("The loaded systemd definition changed during stop; activation refused.")
        if (invocation not in (list(self.invocation), [], [0] * 16)
                or observed["ActiveState"] not in ({"inactive"} if self.owner is not None else {"inactive", "failed"})
                or observed["Job"] != [0, "/"]
                or self.bus.property(path, SERVICE, "MainPID") != 0
                or (self.owner is not None and self.bus.property(path, SERVICE, "Result") != "success")
                or self.bus.property(path, UNIT, "UnitFileState") != self.snapshot["UnitFileState"]
                or self.service.recorded() != self.selection
                or _file_snapshot(self.service.path) != self.file):
            raise SystemdStopError("The stopped systemd invocation was replaced or its enablement changed.")
        if self.owner is not None and platform.process_identity(self.owner["pid"]) is not None:
            raise SystemdStopError("The old Runtime PID is still present or was reused after systemd stop.")


@contextmanager
def prepare_systemd_stop(service, version, data, owner, *, recovery=None):
    bus = _ManagerBus()
    try:
        observed = SystemdInvocationStop(service, version, data, owner, bus, recovery=recovery)
        if owner is not None:
            raise SystemdStopError(WARM_REFUSAL)
        yield observed
    finally:
        bus.close()
