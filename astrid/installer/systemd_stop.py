"""Direct managed-update delegation to the retained original user manager.

The native private peer identifies the supervisor, not the Runtime. C1 and
fresh original-parent/invocation checks admit one Unit.Stop("fail"). Its exact
job, service drain and independent owner/known-child absence must all settle.
Authorized manager policy is not frozen after admission. A changed policy or
lost session prevents further C2 actions; it never permits a PID/label retry.
"""
from __future__ import annotations

import ctypes as C
import ctypes.util
import json
import os
import re
import socket
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from runtime_protocol import platform

MANAGER = "/org/freedesktop/systemd1"
MANAGER_INTERFACE = "org.freedesktop.systemd1.Manager"
UNIT = "org.freedesktop.systemd1.Unit"
SERVICE = "org.freedesktop.systemd1.Service"
PROPERTIES = "org.freedesktop.DBus.Properties"
STOP_SECONDS = 90
ADMISSION_SECONDS = 5
MAX_EVENTS = 4096
STOP_RELATIONSHIPS = ("OnFailure", "OnSuccess", "PropagatesStopTo", "ConsistsOf", "BoundBy", "RequiredBy", "RequisiteOf")
STOP_ACTIONS = ("FailureAction", "SuccessAction", "JobTimeoutAction", "StartLimitAction")


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


_Handler = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p, C.POINTER(_Error))


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
        self.deadline = time.monotonic() + ADMISSION_SECONDS
        self.events = []
        self.slots = []
        self.handlers = []
        self.fault = None
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
                "sd_bus_close": ([C.c_void_p], None),
                "sd_bus_unref": ([C.c_void_p], C.c_void_p),
                "sd_bus_set_method_call_timeout": ([C.c_void_p, C.c_uint64], C.c_int),
                "sd_bus_add_match": ([C.c_void_p, C.POINTER(C.c_void_p), C.c_char_p, _Handler, C.c_void_p], C.c_int),
                "sd_bus_slot_unref": ([C.c_void_p], C.c_void_p),
                "sd_bus_process": ([C.c_void_p, C.POINTER(C.c_void_p)], C.c_int),
                "sd_bus_wait": ([C.c_void_p, C.c_uint64], C.c_int),
                "sd_bus_message_get_path": ([C.c_void_p], C.c_char_p),
                "sd_bus_message_get_interface": ([C.c_void_p], C.c_char_p),
                "sd_bus_message_get_member": ([C.c_void_p], C.c_char_p),
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
            self._check(self.library.sd_bus_set_method_call_timeout(self.bus, int(self.remaining() * 1_000_000)))
            self._check(self.library.sd_bus_start(self.bus))
            fd = self._check(self.library.sd_bus_get_fd(self.bus))
            from runtime_protocol import process_binding
            with socket.socket(fileno=os.dup(fd)) as connection:
                self.peer = process_binding.retain_peer(connection)
            self.identity = dict(self.peer.identity)
            self._verify_peer()
            self.call(MANAGER, "org.freedesktop.DBus.Peer", "Ping")
            self._subscribe()
            self.remaining()
            self.deadline = None
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
        self.remaining()
        if self.fault is not None:
            raise SystemdStopError(self.fault)
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
        self.remaining()

    def remaining(self):
        remaining = ADMISSION_SECONDS if self.deadline is None else self.deadline - time.monotonic()
        if remaining <= 0:
            raise SystemdStopError("The retained systemd operation deadline expired; its outcome is uncertain.")
        return remaining

    @contextmanager
    def bounded(self, *, seconds=ADMISSION_SECONDS, deadline=None):
        previous = self.deadline
        requested = time.monotonic() + seconds if deadline is None else deadline
        self.deadline = min(previous, requested) if previous is not None else requested
        try:
            self.remaining()
            yield
            self.remaining()
        finally:
            self.deadline = previous

    def _body(self, message):
        values = []
        code, contents = C.c_char(), C.c_char_p()
        while self._check(self.library.sd_bus_message_peek_type(message, C.byref(code), C.byref(contents))):
            if len(values) >= 32:
                raise SystemdStopError("Oversized systemd message body.")
            values.append(self._read(message))
        return values

    def _subscribe(self):
        # Direct private connections have no bus-daemon sender name. Matches
        # are installed before any action, with callback/slot lifetimes retained.
        matches = [f"type='signal',path='{MANAGER}',interface='{MANAGER_INTERFACE}',member='{member}'"
                   for member in ("JobRemoved", "JobNew", "UnitNew", "UnitRemoved", "Reloading")]
        matches += ["type='signal',interface='org.freedesktop.DBus.Local',member='Disconnected'",
                    f"type='signal',path_namespace='{MANAGER}/unit',interface='{PROPERTIES}',member='PropertiesChanged'"]

        @_Handler
        def receive(message, _userdata, _error):
            try:
                member = self.library.sd_bus_message_get_member(message).decode()
                path = self.library.sd_bus_message_get_path(message).decode()
                interface = self.library.sd_bus_message_get_interface(message).decode()
                values = self._body(message)
                if len(self.events) >= MAX_EVENTS:
                    raise SystemdStopError("The retained systemd event history overflowed.")
                self.events.append((member, path, interface, values))
                if member == "Disconnected":
                    self.fault = "The original systemd manager connection was lost."
            except Exception as exc:
                self.fault = f"The systemd event could not be verified: {exc}"
            return 1

        self.handlers.append(receive)
        for match in matches:
            self.remaining()
            slot = C.c_void_p()
            self._check(self.library.sd_bus_add_match(self.bus, C.byref(slot), match.encode(), receive, None))
            self.slots.append(slot)

    def poll(self, *, wait=False):
        self._verify_peer()
        for _ in range(256):
            self.remaining()
            if not self._check(self.library.sd_bus_process(self.bus, None)):
                break
        else:
            raise SystemdStopError("The systemd event stream did not settle within its bounded batch.")
        self._verify_peer()
        if wait:
            self._check(self.library.sd_bus_wait(self.bus, int(min(0.1, self.remaining()) * 1_000_000)))
            self._verify_peer()

    def mark(self):
        self.poll()
        return len(self.events)

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
        if isinstance(raw, bytes) and len(raw) > 1_048_576:
            raise SystemdStopError("Oversized systemd string.")
        return raw.decode("utf-8") if kind in (b"s", b"o", b"g") else bool(raw) if kind == b"b" else raw

    def call(self, path, interface, method, *args, timeout=None):
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
            remaining = self.remaining() if timeout is None else min(timeout, self.remaining())
            result = self.library.sd_bus_call(self.bus, request, max(1, int(remaining * 1_000_000)),
                                               C.byref(error), C.byref(reply))
            if result < 0:
                raise _BusError((error.name or b"unavailable").decode(),
                                (error.message or os.strerror(-result).encode()).decode())
            self._verify_peer()
            values = self._body(reply)
            if method in {"Ping", "Reload"}:
                if values:
                    raise SystemdStopError("Unexpected nonempty systemd completion reply.")
                return None
            if len(values) != 1:
                raise SystemdStopError("Unexpected systemd method reply arity.")
            return values[0]
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
                for slot in self.slots:
                    self.library.sd_bus_slot_unref(slot)
                self.slots.clear()
                self.handlers.clear()
                self.library.sd_bus_close(self.bus)
                self.library.sd_bus_unref(self.bus)
                self.bus = C.c_void_p()
        finally:
            if self.peer is not None:
                self.peer.close()
                self.peer = None


def _file_snapshot(path):
    st = path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > 1_048_576
            or st.st_mode & 0o022):
        raise SystemdStopError("The systemd unit file is not privately owned by this installation.")
    raw = path.read_bytes()
    after = path.lstat()
    fields = ("st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    before_key = tuple(getattr(st, key) for key in fields)
    if before_key != tuple(getattr(after, key) for key in fields):
        raise SystemdStopError("The systemd unit changed during observation.")
    return before_key, raw


def _process_cgroup(pid):
    """Read the systemd hierarchy as a consistency observation, never custody."""
    entries = {}
    for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
        number, controllers, path = line.split(":", 2)
        if (not number.isascii() or not number.isdigit() or not path.startswith("/")
                or any(part in {".", ".."} for part in path.split("/"))):
            raise SystemdStopError("The process cgroup observation is malformed.")
        if controllers in {"", "name=systemd"}:
            if controllers in entries:
                raise SystemdStopError("The process cgroup observation is ambiguous.")
            entries[controllers] = path
    if "name=systemd" in entries:
        return entries["name=systemd"]
    if "" in entries:
        return entries[""]
    raise SystemdStopError("The process has no observable systemd cgroup hierarchy.")


def _inside(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def _present(pid, *, timeout=1.0):
    # process_identity deliberately reports an exited zombie as absent. A
    # stop drain also waits for its retained /proc entry to be reaped. Unknown
    # permissions are errors; no numeric PID is opened or signalled here.
    observed = platform.process_identity(pid, timeout=timeout)
    if observed is not None:
        return observed
    try:
        Path(f"/proc/{pid}").stat()
    except FileNotFoundError:
        return None
    return {"exited_unreaped": True}


class _JobEvents:
    """Bounded exact-job history on one retained connection, including early events."""
    def __init__(self, bus, start, label_path, *, allow_new=False):
        self.bus, self.cursor, self.label_path = bus, start, label_path
        self.job = None
        self.result = None
        self.pending_jobs = []
        self.reloading = False
        self.reloads = 0
        self.removed = False
        self.replaced = False
        self.allow_new = allow_new

    def bind(self, path):
        if not isinstance(path, str) or not re.fullmatch(MANAGER + r"/job/[1-9][0-9]*", path):
            raise SystemdStopError("The manager did not return a verifiable job path.")
        self.job = int(path.rsplit("/", 1)[1]), path
        self.scan()

    def scan(self):
        self.bus.poll()
        for member, path, interface, values in self.bus.events[self.cursor:]:
            self.cursor += 1
            if member == "Disconnected":
                raise SystemdStopError("The retained manager disconnected during its operation.")
            if interface != MANAGER_INTERFACE or path != MANAGER:
                continue  # Policy properties are freshly read outside callbacks.
            if member == "Reloading":
                if len(values) != 1 or type(values[0]) is not bool:
                    raise SystemdStopError("Malformed manager reload event.")
                self.reloading = values[0]
                self.reloads += int(values[0])
            elif member in {"UnitNew", "UnitRemoved"}:
                if len(values) != 2 or not all(isinstance(v, str) for v in values):
                    raise SystemdStopError("Malformed manager unit event.")
                if values[0] == "astrid.service":
                    if values[1] != self.label_path:
                        raise SystemdStopError("The manager changed the selected unit path.")
                    if not self.reloading:
                        self.removed |= member == "UnitRemoved"
                        self.replaced |= member == "UnitNew" and not self.allow_new
            elif member in {"JobNew", "JobRemoved"}:
                length = 4 if member == "JobRemoved" else 3
                if (len(values) != length or type(values[0]) is not int or values[0] <= 0
                        or not all(isinstance(v, str) for v in values[1:])
                        or values[1] != f"{MANAGER}/job/{values[0]}"):
                    raise SystemdStopError("Malformed manager job event.")
                if values[2] == "astrid.service":
                    self.pending_jobs.append((member, values))
        if self.job is not None:
            for member, values in self.pending_jobs:
                if (values[0], values[1]) != self.job:
                    raise SystemdStopError("A different job acted on the selected service; outcome is uncertain.")
                if member == "JobRemoved":
                    if self.result is not None and self.result != values[3]:
                        raise SystemdStopError("The stop job has conflicting terminal results.")
                    self.result = values[3]
                    if self.result != "done":
                        raise SystemdStopError(f"The exact systemd job finished with {self.result}; activation refused.")
            self.pending_jobs.clear()
        if self.replaced:
            raise SystemdStopError("The selected unit was replaced during the manager operation.")


class _ChangingObservation(SystemdStopError):
    pass


class SystemdInvocationStop:
    """One stop of the original manager's admitted direct-child invocation."""
    def __init__(self, service, version, data, owner, bus, *, recovery=None, session=None):
        self.service, self.version, self.data, self.bus = service, version, data, bus
        self.session = session
        self.owner = dict(owner) if owner is not None else None
        self.known = {}
        self.submitted = False
        self.completed = False
        self.stop_deadline = None
        self.events = None
        with bus.bounded():
            self.event_start = bus.mark()
            self._initialize(recovery)

    def _initialize(self, recovery):
        from .core import current

        service, version, data, owner, bus = self.service, self.version, self.data, self.owner, self.bus
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
        path = bus.call(MANAGER, MANAGER_INTERFACE, "GetUnit", "astrid.service")
        if path != f"{MANAGER}/unit/astrid_2eservice":
            raise SystemdStopError("The manager did not return the canonical selected service.")
        self.label_path = path
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
        self.group = self.snapshot["ControlGroup"]
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
        path = path or self.path
        before = (self.bus.property(path, UNIT, "InvocationID"), self.bus.property(path, SERVICE, "MainPID"))
        properties = {
            UNIT: ("Id", "FragmentPath", "DropInPaths", "LoadState", "InvocationID", "UnitFileState", "ActiveState", "Job",
                   "SubState", "NeedDaemonReload", "StopWhenUnneeded", "RefuseManualStop", *STOP_RELATIONSHIPS, *STOP_ACTIONS),
            SERVICE: ("MainPID", "ControlPID", "Type", "PIDFile", "NotifyAccess", "ControlGroup", "Delegate", "Restart",
                      "Result", "ExecStart", "ExecStop", "ExecStopPost", "KillMode", "KillSignal",
                      "FinalKillSignal", "SendSIGKILL", "TimeoutStopUSec", "TimeoutStopFailureMode"),
        }
        result = {key: self.bus.property(path, interface, key)
                  for interface, names in properties.items() for key in names}
        after = (self.bus.property(path, UNIT, "InvocationID"), self.bus.property(path, SERVICE, "MainPID"))
        if before != after or after != (result["InvocationID"], result["MainPID"]):
            raise _ChangingObservation("The systemd owner changed within its bounded observation sequence.")
        # ExecStart's execution timestamps/status change at exit, but its
        # executable, argv and ignore-failure flag must stay byte-for-byte.
        starts = result["ExecStart"]
        if not isinstance(starts, list) or len(starts) != 1 or len(starts[0]) < 3:
            raise SystemdStopError("The loaded service has an unknown start action.")
        result["ExecStart"] = starts[0][:3]
        return result

    def validate(self, owner):
        with self.bus.bounded():
            self._validate(owner)

    def _validate(self, owner):
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
                or value["SubState"] not in ({"running"} if owner is not None else {"dead", "failed"})
                or value["Job"] != [0, "/"]
                or value["UnitFileState"] not in {"enabled", "disabled"}
                or not isinstance(argv, list) or not argv or executable != argv[0]
                or executable != str(self.loaded_version.python) or ignore_failure
                or not self.service._known_launch_action(argv, self.data, windows=False, script=script,
                                                         version=self.loaded_version)
                or value["ExecStop"] or value["ExecStopPost"]
                or value["Type"] != "simple" or value["PIDFile"] or value["NotifyAccess"] != "none"
                or value["ControlPID"] != 0 or value["Delegate"] is not False or value["Restart"] != "on-failure"
                or value["KillMode"] != "mixed" or value["KillSignal"] != 15
                or value["FinalKillSignal"] != 9 or value["SendSIGKILL"] is not True
                or value["TimeoutStopFailureMode"] != "terminate"
                or type(value["TimeoutStopUSec"]) is not int
                or not 0 < value["TimeoutStopUSec"] <= STOP_SECONDS * 1_000_000):
            raise SystemdStopError("The loaded systemd action does not match the bounded owned Runtime invocation.")
        if owner is not None:
            observed = self._identity(owner["pid"])
            if (observed is None or observed.get("pid") != owner["pid"] or observed.get("uid") != os.getuid()
                    or observed.get("parent_pid") != self.bus.identity["pid"]
                    or platform.process_birth_matches(owner["process_birth_id"], observed.get("birth_id", "")) is not True):
                raise SystemdStopError("The systemd MainPID is not the original manager's authenticated Runtime incarnation.")
            self._scope(observed)
            self.known[owner["pid"]] = observed
        members = self._members(self.path)
        if owner is None and members:
            raise SystemdStopError("The supposedly stopped service still owns processes.")
        if owner is None and self.known and not self._absent():
            raise SystemdStopError("A known owner or child is still present before cold admission.")
        if owner is not None and owner["pid"] not in members:
            raise SystemdStopError("The authenticated Runtime is absent from its supervisor's service scope.")
        history = _JobEvents(self.bus, self.event_start, self.label_path)
        history.scan()
        if history.reloads or history.reloading or history.removed or history.pending_jobs:
            raise SystemdStopError("The manager's service lifecycle changed before final admission.")
        # Sequential Gets are observations, never an atomic definition lock.
        if self.bus.property(self.path, UNIT, "InvocationID") != self.snapshot["InvocationID"]:
            raise SystemdStopError("The systemd invocation changed at the stop fence.")
        self.bus._verify_peer()

    def _identity(self, pid):
        observed = platform.process_identity(pid, timeout=min(1.0, self.bus.remaining()))
        self.bus.remaining()
        return observed

    def _cgroup(self, pid):
        self.bus.remaining()
        group = _process_cgroup(pid)
        self.bus.remaining()
        return group

    def _scope(self, owner):
        group = self.group
        if (not isinstance(group, str) or not group.startswith("/") or not group.endswith("/astrid.service")
                or any(part in {".", ".."} for part in group.split("/"))):
            raise SystemdStopError("The Runtime has no exact direct service cgroup.")
        manager_group = self._cgroup(self.bus.identity["pid"])
        manager_root = manager_group.rsplit("/", 1)[0] if manager_group.endswith("/init.scope") else manager_group
        if (not _inside(group, manager_root) or _inside(manager_group, group)
                or self._cgroup(owner["pid"]) != group
                or _inside(self._cgroup(os.getpid()), group)):
            raise SystemdStopError("The original manager, Runtime or updater cgroup relationship is unqualified.")

    def _members(self, path):
        rows = self.bus.call(path, SERVICE, "GetProcesses")
        if not isinstance(rows, list) or len(rows) > 4096:
            raise SystemdStopError("The manager's service process observation is incomplete.")
        members = set()
        for row in rows:
            self.bus.remaining()
            if (not isinstance(row, list) or len(row) != 3 or not isinstance(row[0], str)
                    or type(row[1]) is not int or row[1] <= 0 or not isinstance(row[2], str)
                    or not self.group or not _inside(row[0], self.group)):
                raise SystemdStopError("A service child is outside the admitted drain scope.")
            pid = row[1]
            members.add(pid)  # Even an exited/unreaped row prevents full drain.
            observed = self._identity(pid)
            if observed is None:
                continue  # Exit raced the manager's enumeration; absence is rechecked below.
            if (observed.get("uid") != os.getuid() or not observed.get("birth_id")
                    or not _inside(self._cgroup(pid), self.group)):
                raise SystemdStopError("A service child identity or cgroup is unknown.")
            previous = self.known.get(pid)
            if previous is not None and platform.process_birth_matches(previous["birth_id"], observed["birth_id"]) is not True:
                raise SystemdStopError("A service child PID changed or was reused during drain.")
            self.known[pid] = observed
        return members

    def _absent(self):
        absent = True
        for pid, expected in self.known.items():
            observed = _present(pid, timeout=min(1.0, self.bus.remaining()))
            self.bus.remaining()
            if observed is None:
                continue
            absent = False
            if observed.get("exited_unreaped"):
                continue
            if (observed.get("uid") != expected["uid"]
                    or platform.process_birth_matches(expected["birth_id"], observed.get("birth_id", "")) is not True):
                raise SystemdStopError("An old owner or child PID is present with a changed identity.")
            if not _inside(self._cgroup(pid), self.group):
                raise SystemdStopError("A known service child escaped its qualified drain scope.")
        return absent

    def _unchanged_files(self):
        if not getattr(self, "c1_completed", False):
            self._validate_recovery()
        elif any(_file_snapshot(path) != value for path, value in self.retained_files.items()):
            raise SystemdStopError("The retained recovery files changed after C1 admission.")
        if self.service.recorded() != self.selection or _file_snapshot(self.service.path) != self.file:
            raise SystemdStopError("The selected service or registration changed after admission.")

    def _drained(self):
        self._unchanged_files()
        self.bus._verify_peer()
        if self.events is not None:
            self.events.scan()
            if self.events.reloading:
                return False
        try:
            path = self.bus.call(MANAGER, MANAGER_INTERFACE, "GetUnit", "astrid.service")
        except _BusError as exc:
            if exc.name != "org.freedesktop.systemd1.NoSuchUnit" or self.events is None:
                raise
            try:
                self.bus.call(MANAGER, MANAGER_INTERFACE, "GetUnitByInvocationID", self.invocation)
            except _BusError as missing:
                if missing.name != "org.freedesktop.systemd1.NoUnitForInvocationID":
                    raise
            else:
                raise SystemdStopError("The manager's label and invocation lookup disagree.")
            # GC is a correlated manager event plus exact successful job and
            # independent absence, never a missing alias/transport exception.
            enabled = self.bus.call(MANAGER, MANAGER_INTERFACE, "GetUnitFileState", "astrid.service")
            if enabled != self.snapshot["UnitFileState"]:
                raise SystemdStopError("The unloaded service's enablement changed.")
            return (self.events.result == "done" and self.events.removed and not self.events.reloading
                    and self._absent())
        if path != self.label_path:
            raise SystemdStopError("A replacement unit appeared during service drain.")
        try:
            observed = self._snapshot(path)
        except _BusError as exc:
            if exc.name not in {"org.freedesktop.DBus.Error.UnknownObject", "org.freedesktop.systemd1.NoSuchUnit"}:
                raise
            # GC/reload may race the sequential property reads. Try the real
            # manager lookups and correlated history again within the same
            # budget; an UnknownObject reply itself proves no absence.
            raise _ChangingObservation("The loaded unit disappeared during drain observation.") from exc
        dynamic = {"InvocationID", "MainPID", "ControlPID", "ActiveState", "SubState", "Job", "ControlGroup", "Result"}
        if any(observed[key] != value for key, value in self.snapshot.items() if key not in dynamic):
            raise SystemdStopError("The loaded systemd definition changed during stop; activation refused.")
        if (observed["InvocationID"] not in (list(self.invocation), [], [0] * 16)
                or observed["ControlGroup"] not in (self.group, "")
                or observed["MainPID"] not in (0, self.owner["pid"] if self.owner else 0)):
            raise SystemdStopError("The stopped systemd invocation was replaced.")
        if self.events is not None and self.events.job is not None:
            if observed["Job"] not in ([0, "/"], list(self.events.job)):
                raise SystemdStopError("A conflicting job replaced the admitted stop.")
        elif observed["Job"] != [0, "/"]:
            raise SystemdStopError("A job appeared on the quiescent service.")
        members = self._members(path)
        if self.owner is not None and observed["Result"] != "success":
            raise SystemdStopError(f"The stopped service result is {observed['Result']}; it was not a successful stop.")
        return (observed["ActiveState"] in ({"inactive"} if self.owner else {"inactive", "failed"})
                and observed["SubState"] in ({"dead"} if self.owner else {"dead", "failed"})
                and observed["MainPID"] == observed["ControlPID"] == 0
                and observed["Job"] == [0, "/"] and not members and self._absent())

    def stop(self, owner):
        if self.submitted or self.owner is None or owner != self.owner:
            raise SystemdStopError("The invocation stop capability is absent or already spent.")
        self.stop_deadline = time.monotonic() + STOP_SECONDS
        self.submitted = True  # Lost replies must never cause a retry.
        with self.bus.bounded(deadline=self.stop_deadline):
            self._validate(owner)
            self.events = _JobEvents(self.bus, self.event_start, self.label_path)
            job = self.bus.call(self.path, UNIT, "Stop", "fail")
            self.events.bind(job)  # Includes JobRemoved buffered before the reply.
            while True:
                self.events.scan()
                if not self.events.reloading:
                    try:
                        drained = self._drained()
                    except _ChangingObservation:
                        drained = False
                    if self.events.result == "done" and drained:
                        self.completed = self.stopped = True
                        return
                self.bus.poll(wait=True)

    def assert_stopped(self):
        if self.owner is not None and not self.completed:
            raise SystemdStopError("The exact invocation stop has not completed.")
        deadline = self.stop_deadline if not getattr(self, "c1_completed", False) else None
        with self.bus.bounded(deadline=deadline):
            while True:
                try:
                    drained = self._drained()
                except _ChangingObservation:
                    self.bus.poll(wait=True)
                    continue
                if not drained:
                    raise SystemdStopError("The original service owner/children have not independently drained.")
                return


class SystemdUpdateSession:
    """Ephemeral manager continuity across this one direct C2 transaction.

    C1 independently admits each stop. This session is not a reusable stop
    capability and is never reconstructed from an operation id or journal.
    """
    def __init__(self, service, data, bus):
        self.service, self.data, self.bus = service, data, bus
        self.enabled_state = None
        self.last = None
        self.running = None
        self.operation = None
        self.registering = None
        self.failed = None

    def check(self):
        if self.failed is not None:
            raise SystemdStopError(f"The admitted manager session is retired: {self.failed}")
        with self.bus.bounded():
            self.bus.poll()

    @contextmanager
    def protected(self):
        try:
            self.check()
            yield
        except Exception as exc:
            self.failed = str(exc)
            failure = SystemdStopError(f"The admitted systemd update is uncertain: {exc}")
            failure.details = {"cause_details": getattr(exc, "details", None), "manager_session_uncertain": True}
            raise failure from exc

    def admit_capability(self, capability):
        if self.enabled_state is None:
            self.enabled_state = capability.enabled_state
        elif self.enabled_state is not capability.enabled_state:
            raise SystemdStopError("The original service enablement changed during this transaction.")
        if self.running is not None:
            running = self.running
            running["events"].scan()
            if (capability.version != running["version"]
                    or capability.invocation not in (running["invocation"], bytes(16))
                    or capability.owner is not None and capability.invocation != running["invocation"]):
                raise SystemdStopError("Rollback cannot chase a replacement candidate invocation.")
            for pid, previous in running["known"].items():
                observed = capability.known.get(pid)
                if observed is not None and platform.process_birth_matches(
                        previous["birth_id"], observed["birth_id"]) is not True:
                    raise SystemdStopError("A previously observed candidate child PID was reused.")
                capability.known[pid] = previous
            if capability.owner is None and not capability._absent():
                raise SystemdStopError("The previously started candidate still has an owner or child.")

    def c1_complete(self, capability):
        if capability.session is not self:
            raise SystemdStopError("C1 completion belongs to another manager session.")
        capability.assert_stopped()
        capability.c1_completed = True
        self.last, self.running = capability, None

    def _transaction(self, version, data, transaction):
        from .core import Version, current, read_json

        if (data != self.data or not isinstance(transaction, dict)
                or transaction.get("transaction_kind") not in {None, "migration"}
                or not re.fullmatch(r"[0-9a-f]{32}", transaction.get("operation_id", ""))
                or transaction.get("data_root") != str(data)
                or Version.load(transaction["old"]).release != 15
                or version.managed_by is not None or current(self.service.root) != version
                or version.document() not in (Version.load(transaction["old"]).document(), transaction.get("candidate"))
                or read_json(self.service.root / "update-transaction.json") != transaction):
            raise SystemdStopError("The selected C2 transaction changed before manager activation.")
        if self.operation is not None and self.operation != transaction["operation_id"]:
            raise SystemdStopError("The manager session cannot authorize a different update operation.")
        self.operation = transaction["operation_id"]

    def enablement(self):
        observed = self.bus.call(MANAGER, MANAGER_INTERFACE, "GetUnitFileState", "astrid.service")
        if observed != ("enabled" if self.enabled_state else "disabled"):
            raise SystemdStopError("The original service enablement changed before activation.")
        return observed

    def guard(self):
        with self.protected(), self.bus.bounded():
            if self.running is not None:
                self._check_running()
            elif self.last is not None:
                self.last.assert_stopped()
            else:
                raise SystemdStopError("No independent C1 stop completed for this manager session.")

    def begin_registration(self, version, data, transaction):
        with self.protected(), self.bus.bounded():
            self._transaction(version, data, transaction)
            if transaction.get("step") not in {"switched", "rollback_selected"}:
                raise SystemdStopError("The transaction is not ready for service registration.")
            if self.running is not None or self.last is None:
                raise SystemdStopError("Registration requires the independently completed C1 stop.")
            self.last.assert_stopped()
            self.enablement()
            self.registering = version, data, transaction

    def reload_definition(self, version, data):
        with self.protected(), self.bus.bounded():
            if self.registering is None or self.registering[:2] != (version, data):
                raise SystemdStopError("No selected registration authorizes this manager reload.")
            self._transaction(version, data, self.registering[2])
            self.enablement()
            # Reload's empty reply is sent after reconstruction completes.
            # This is our admitted configuration action, not a definition lock.
            self.bus.call(MANAGER, MANAGER_INTERFACE, "Reload")
            self.bus.poll()
            self.enablement()

    def finish_registration(self, version, data):
        with self.protected(), self.bus.bounded():
            if self.registering is None or self.registering[:2] != (version, data):
                raise SystemdStopError("The registered version changed during C2 activation.")
            self._transaction(version, data, self.registering[2])
            try:
                self.bus.call(MANAGER, MANAGER_INTERFACE, "GetUnit", "astrid.service")
            except _BusError as exc:
                if exc.name != "org.freedesktop.systemd1.NoSuchUnit":
                    raise
                # The successful stop may have allowed unit GC. LoadUnit only
                # loads this newly registered definition; it installs no start
                # job (systemd v252 dbus-manager.c:method_load_unit). This action
                # is after C1, never an admission or absence shortcut.
                self.bus.call(MANAGER, MANAGER_INTERFACE, "LoadUnit", "astrid.service")
            observed = SystemdInvocationStop(self.service, version, data, None, self.bus, session=self)
            observed.assert_stopped()
            self.admit_capability(observed)
            # Old-owner absence was proved by C1 before the owned definition
            # changed. This fresh observation only guards the new activation.
            self.last = observed
            self.registering = None

    def start(self, version, data, transaction):
        with self.protected(), self.bus.bounded(seconds=STOP_SECONDS):
            self._transaction(version, data, transaction)
            if (self.registering is not None or self.last is None or self.last.version != version
                    or transaction.get("step") not in {"switched", "rollback_selected"}):
                raise SystemdStopError("The selected registered service is not ready to start.")
            self.last.assert_stopped()
            self.enablement()
            window = _JobEvents(self.bus, self.bus.mark(), self.last.label_path, allow_new=True)
            # Unlike Stop, this label action is authorized by the selected,
            # freshly registered candidate definition after independent absence.
            path = self.bus.call(MANAGER, MANAGER_INTERFACE, "StartUnit", "astrid.service", "fail")
            window.bind(path)
            while True:
                window.scan()
                if not window.reloading:
                    try:
                        value = self.last._snapshot(self.last.label_path)
                    except _ChangingObservation:
                        self.bus.poll(wait=True)
                        continue
                    self._registered_policy(value)
                    if (window.result == "done" and value["ActiveState"] == "active"
                            and value["SubState"] == "running" and value["MainPID"] > 0
                            and value["ControlPID"] == 0 and value["Job"] == [0, "/"]):
                        invocation = bytes(value["InvocationID"])
                        if len(invocation) != 16 or not any(invocation) or value["Result"] != "success":
                            raise SystemdStopError("The new service has no successful exact invocation.")
                        owner = self.last._identity(value["MainPID"])
                        if (owner is None or owner.get("uid") != os.getuid()
                                or owner.get("parent_pid") != self.bus.identity["pid"]):
                            raise SystemdStopError("The activated service is not the retained manager's direct child.")
                        self.last.group = value["ControlGroup"]
                        self.last._scope(owner)
                        members = self.last._members(self.last.label_path)
                        if owner["pid"] not in members:
                            raise SystemdStopError("The new Runtime is outside its manager's service scope.")
                        self.running = {"version": version, "invocation": invocation, "owner": owner,
                                        "known": dict(self.last.known), "events": window}
                        window.allow_new = False  # Later UnitNew is replacement history, not our activation.
                        self._check_running()
                        return True
                    if value["ActiveState"] == "failed":
                        raise SystemdStopError(f"The candidate service failed to start: {value['Result']}")
                self.bus.poll(wait=True)

    def _registered_policy(self, observed):
        self.last._unchanged_files()
        dynamic = {"InvocationID", "MainPID", "ControlPID", "ActiveState", "SubState", "Job", "ControlGroup", "Result"}
        if any(observed[key] != value for key, value in self.last.snapshot.items() if key not in dynamic):
            raise SystemdStopError("The newly registered service policy changed before readiness.")

    def _check_running(self):
        running = self.running
        running["events"].scan()
        if running["events"].reloading:
            raise SystemdStopError("Manager reload makes candidate readiness uncertain.")
        value = self.last._snapshot(self.last.label_path)
        self._registered_policy(value)
        observed = self.last._identity(running["owner"]["pid"])
        if (bytes(value["InvocationID"]) != running["invocation"]
                or value["MainPID"] != running["owner"]["pid"] or value["ActiveState"] != "active"
                or value["SubState"] != "running" or value["Job"] != [0, "/"]
                or value["ControlPID"] != 0 or value["Result"] != "success"
                or value["ControlGroup"] != self.last.group
                or observed is None or observed.get("uid") != self.bus.identity["uid"]
                or observed.get("parent_pid") != self.bus.identity["pid"]
                or platform.process_birth_matches(running["owner"]["birth_id"], observed.get("birth_id", "")) is not True):
            raise SystemdStopError("The candidate invocation changed before C2 readiness completed.")
        self.last._scope(observed)
        if observed["pid"] not in self.last._members(self.last.label_path):
            raise SystemdStopError("The candidate left its admitted service scope before readiness.")
        running["known"].update(self.last.known)


@contextmanager
def prepare_systemd_stop(service, version, data, owner, *, recovery=None, session=None):
    original = session is None
    if original:
        session = SystemdUpdateSession(service, data, _ManagerBus())
    try:
        with session.bus.bounded():
            session.check()
            observed = SystemdInvocationStop(service, version, data, owner, session.bus, recovery=recovery, session=session)
            session.admit_capability(observed)
        yield observed
    finally:
        if original:
            session.bus.close()
