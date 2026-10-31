"""Private, bounded observations from an owned action interpreter.

An inherited channel associates observations with a host launch. The host alone
supplies launch/task/source identity. File hashes describe bytes at observation
time, not already-loaded bytecode or proof that a module served bridge traffic.
No module is imported to fill a missing observation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import struct
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .process_group import process_birth_identity

_FD_ENV = "_ASTRID_CHILD_IMPORT_FD"
_MAX_PACKET = 24576
_MAX_FILE = 8 * 1024 * 1024
_UNAVAILABLE = "unavailable"
_MODULES = (
    "__main__", "astrid.core.execution.generic_host",
    "astrid.core.execution.generic_host_worker", "astrid.core.execution._child_bridge",
    "astrid.sdk._child_bridge", "astrid.packs.h3_av.actions.transform.run",
    "astrid.packs.vibecomfy.actions.validate.run",
    "astrid.core.execution.orchestrator.runner",
)
_ACTIONS = {
    "h3_av.transform": "astrid.packs.h3_av.actions.transform.run",
    "vibecomfy.validate": "astrid.packs.vibecomfy.actions.validate.run",
}
_BINDING_KEYS = (
    "task_id", "run_id", "attempt_id", "fence", "runtime_epoch",
    "parent_task_id", "parent_attempt_id", "operation_id", "submission_id",
)
_HOST_KEYS = (
    "source_checkout", "source_checkout_digest", "source_closure_digest",
    "source_inventory_identity", "execution_profile", "boot_manifest_hash",
    "executor_incarnation", "profile_id", "runtime_instance_id", "target_digest",
    "readiness_profile_hash", "source_manifest_digest",
)


def _bounded(value, limit=1024):
    return value if type(value) is str and 0 < len(value) <= limit and not any(
        ord(c) < 32 for c in value) else _UNAVAILABLE


def _birth(pid):
    try:
        if sys.platform == "darwin":
            # Reuse the existing selected-PID native observation, without a
            # system-wide census or any change to lifecycle/custody decisions.
            from .custody_broker import _darwin_process_start

            start = _darwin_process_start(pid)
            return "darwin:%s:%s:%s" % (start["start_sec"], start["start_usec"], start["start_abstime"])
        return _bounded(process_birth_identity(pid), 256)
    except Exception:
        return _UNAVAILABLE


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _module(name):
    module = sys.modules.get(name)
    row = dict(module_name=name, status="not_loaded", actual_file=_UNAVAILABLE,
               resolved_path=_UNAVAILABLE, sha256=_UNAVAILABLE,
               spec_name=_UNAVAILABLE, import_mode="not_loaded")
    if module is None:
        return row
    row.update(status="unavailable", import_mode="already_loaded")
    try:
        spec_name = getattr(getattr(module, "__spec__", None), "name", None)
        row["spec_name"] = spec_name if spec_name in _MODULES else _UNAVAILABLE
        row["actual_file"] = _bounded(getattr(module, "__file__", None))
        if row["actual_file"] == _UNAVAILABLE:
            return row
        resolved = Path(row["actual_file"]).resolve(strict=True)
        row["resolved_path"] = _bounded(str(resolved))
        if row["resolved_path"] == _UNAVAILABLE:
            return row
        # Nonblocking open + fstat keeps a replaced module path from opening a
        # FIFO/device, and an explicit byte cap bounds file-hashing work.
        fd = os.open(resolved, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE:
                return row
            digest = hashlib.sha256()
            remaining = _MAX_FILE + 1
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                digest.update(chunk)
            if remaining == 0:
                return row
        row.update(status="loaded", sha256=digest.hexdigest())
    except Exception:
        pass
    return row


def _snapshot(phase):
    return {"phase": phase, "pid": os.getpid(), "process_birth_id": _birth(os.getpid()),
            "observed_at_utc": datetime.now(timezone.utc).isoformat(),
            "modules": [_module(name) for name in _MODULES]}


def observe_action_entry(phase="action_entry"):
    """Best-effort observation in the actual action (or runpy worker) process."""
    raw = os.environ.get(_FD_ENV)
    if raw is None:
        return
    if phase == "action_entry":
        os.environ.pop(_FD_ENV, None)
    channel = None
    try:
        fd = int(raw)
        os.set_inheritable(fd, False)
        # dup lets worker_entry leave the original descriptor for action_entry.
        channel = socket.socket(fileno=os.dup(fd))
        channel.settimeout(0.05)
        packet = _encode(_snapshot(phase))
        if len(packet) <= _MAX_PACKET:
            channel.sendall(len(packet).to_bytes(4, "big") + packet)
    except Exception:
        # Absence/invalidity is explicit in the host receipt; observation must
        # never change the operation's original success/failure.
        pass
    finally:
        if channel is not None:
            channel.close()
        if phase == "action_entry":
            try:
                os.close(int(raw))
            except (OSError, ValueError):
                pass


def _validate(packet, pid, birth):
    """Closed schema. In particular no child-supplied host/task claims survive."""
    value = json.loads(packet)
    if type(value) is not dict or set(value) != {
            "phase", "pid", "process_birth_id", "observed_at_utc", "modules"}:
        raise ValueError("observation schema")
    if (value["phase"] not in {"worker_entry", "action_entry"}
            or type(value["pid"]) is not int or value["pid"] != pid
            or value["process_birth_id"] != birth or birth == _UNAVAILABLE):
        raise ValueError("observation process")
    stamp = value["observed_at_utc"]
    if type(stamp) is not str or len(stamp) > 64 or datetime.fromisoformat(stamp).utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("observation timestamp")
    rows = value["modules"]
    if type(rows) is not list or len(rows) != len(_MODULES):
        raise ValueError("observation modules")
    for name, row in zip(_MODULES, rows):
        if type(row) is not dict or set(row) != {
                "module_name", "status", "actual_file", "resolved_path", "sha256", "spec_name", "import_mode"}:
            raise ValueError("module schema")
        if (row["module_name"] != name or row["status"] not in {"loaded", "not_loaded", "unavailable"}
                or row["spec_name"] not in (*_MODULES, _UNAVAILABLE)
                or any(_bounded(row[key]) != row[key] for key in ("actual_file", "resolved_path"))):
            raise ValueError("module fields")
        if row["status"] == "not_loaded":
            if row != dict(module_name=name, status="not_loaded", actual_file=_UNAVAILABLE,
                           resolved_path=_UNAVAILABLE, sha256=_UNAVAILABLE,
                           spec_name=_UNAVAILABLE, import_mode="not_loaded"):
                raise ValueError("absent module")
        elif row["import_mode"] != "already_loaded":
            raise ValueError("module import mode")
        if row["status"] == "loaded":
            if (type(row["sha256"]) is not str or not re.fullmatch("[0-9a-f]{64}", row["sha256"])
                    or row["actual_file"] == _UNAVAILABLE or not Path(row["resolved_path"]).is_absolute()):
                raise ValueError("module hash")
        elif row["sha256"] != _UNAVAILABLE:
            raise ValueError("unavailable hash")
    return value


class ChildImportReceipt:
    """One bounded private channel per owned H3/validator launch, never authority."""

    def __init__(self, host, capability_id, binding=None):
        self.host = host
        self.capability_id = capability_id
        self.launch_id = uuid.uuid4().hex
        binding = binding or {}
        self.binding = {key: (binding[key] if key in {"fence", "runtime_epoch"}
                             and type(binding.get(key)) is int else _bounded(binding.get(key), 256))
                        for key in _BINDING_KEYS}
        self.parent = self.child = None
        self.pid = None
        self.birth = _UNAVAILABLE
        self.reference = None
        self.host_observation = _snapshot("host_launch")

    def prepare(self, env):
        # Never consume an ambient fd/PYTHONPATH or caller-supplied receipt.
        env.pop(_FD_ENV, None)
        if os.name == "nt":
            # The owned command API has no private pass_fds transport here.
            # Report unavailable without changing Windows launch/custody.
            return ()
        try:
            self.parent, self.child = socket.socketpair()
            if sys.platform.startswith("linux"):
                self.parent.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            self.child.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2 * (_MAX_PACKET + 4))
            self.parent.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * (_MAX_PACKET + 4))
            self.parent.setblocking(False)
            env[_FD_ENV] = str(self.child.fileno())
            return (self.child.fileno(),)
        except Exception:
            self.close()
            return ()

    def launched(self, process):
        self.pid = process.pid
        self.birth = _birth(process.pid)
        if self.child is not None:
            try:
                self.child.close()
            except OSError:
                pass
            self.child = None

    def close(self):
        for channel in (self.parent, self.child):
            if channel is not None:
                try:
                    channel.close()
                except OSError:
                    pass
        self.parent = self.child = None

    def _persist(self, data):
        status_path = self.host.evidence_status_path
        if status_path is None:
            raise OSError("no support evidence root")
        root = status_path.parent / "child-import-receipts"
        root.mkdir(mode=0o700, exist_ok=True)
        if root.is_symlink() or stat.S_IMODE(root.stat().st_mode) != 0o700:
            raise OSError("private receipt directory unavailable")
        path = root / (self.launch_id + ".json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def finish(self):
        if self.reference is not None:
            return self.reference
        ref = dict(schema_version=1, launch_id=self.launch_id, sha256=_UNAVAILABLE,
                   observation="unavailable", persistence="failed")
        self.reference = ref
        observations = []
        try:
            if self.parent is not None:
                wire = bytearray()
                # Two framed snapshots plus one byte to detect overflow.
                budget = 2 * (_MAX_PACKET + 4)
                while len(wire) <= budget:
                    try:
                        size = min(65536, budget + 1 - len(wire))
                        if sys.platform.startswith("linux"):
                            chunk, credentials, _, _ = self.parent.recvmsg(size, socket.CMSG_SPACE(12))
                            if chunk and not any(
                                level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS
                                and len(data) == 12 and struct.unpack("3i", data)[0] == self.pid
                                for level, kind, data in credentials
                            ):
                                raise ValueError("observation sender")
                        else:
                            chunk = self.parent.recv(size)
                    except BlockingIOError:
                        break
                    if not chunk:
                        break
                    wire.extend(chunk)
                if len(wire) > budget:
                    raise ValueError("observation budget")
                while wire:
                    size = int.from_bytes(wire[:4], "big")
                    if len(wire) < 4 or size > _MAX_PACKET or len(wire) < 4 + size or len(observations) == 2:
                        raise ValueError("observation budget")
                    packet = bytes(wire[4:4 + size])
                    del wire[:4 + size]
                    observation = _validate(packet, self.pid, self.birth)
                    if observation["phase"] in {o["phase"] for o in observations}:
                        raise ValueError("duplicate observation")
                    observations.append(observation)
                action = next((o for o in observations if o["phase"] == "action_entry"), None)
                if action is not None:
                    if action["modules"][0]["spec_name"] != _ACTIONS[self.capability_id]:
                        raise ValueError("action identity")
                    ref["observation"] = "recorded"
        except Exception:
            observations = []
            ref["observation"] = "invalid"
        finally:
            self.close()
        try:
            startup = getattr(self.host, "_child_import_host_context", {})
            context = {key: _bounded(startup.get(key)) for key in _HOST_KEYS}
            context.update(pid=os.getpid(), process_birth_id=_birth(os.getpid()),
                           executor_id=_bounded(self.host.executor_id, 256),
                           source_epoch=_bounded(self.host.source_epoch, 256),
                           pack_roots=[_bounded(str(p)) for p in self.host.pack_roots[:16]])
            for key in ("runtime_instance_id", "runtime_session_id", "realm_id"):
                context[key] = _bounded(self.host.runtime_state.get(key) or context.get(key), 256)
            receipt = dict(schema_version=1, launch_id=self.launch_id,
                           capability_id=self.capability_id, host=context, binding=self.binding,
                           child={"pid": self.pid if self.pid is not None else _UNAVAILABLE,
                                  "process_birth_id": self.birth},
                           observation=ref["observation"], observations=observations,
                           host_observation=self.host_observation)
            data = _encode(receipt)
            if len(data) > 65536:
                raise ValueError("receipt budget")
            self._persist(data)
            ref.update(sha256=hashlib.sha256(data).hexdigest(), persistence="recorded")
        except Exception:
            # The bounded reference explicitly reports failure, even when the
            # private storage cannot record its own failure. No exception text.
            pass
        return ref
