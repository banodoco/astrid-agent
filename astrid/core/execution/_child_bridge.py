"""Private GenericPackHost service for the D18 SDK operations.

The socket is communication only. All authority is taken from the claimed
attempt retained here; requests never supply a Runtime context.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import stat
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from astrid.core._shared.capability_common import _validate_required_inputs
from astrid.sdk._child_bridge import _bridge_io_failure_frame, _write_bridge_io_failure, _strict_json, _validate_output
from .generic_host import HostError, _verify_admitted_source

_MAX_FRAME = 1048576
_MAX_OBJECT = 5 * 1024 ** 3
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z")


class ChildBridgeError(HostError):
    pass


def _child_wire_id(*, project_id: str | None, parent_task_id: str,
                   parent_attempt_id: str, logical_child_key: str) -> str:
    """Bind a logical SDK key to one parent attempt's Runtime namespace."""
    preimage = {"version": 1, "project_id": project_id, "parent_task_id": parent_task_id,
                "parent_attempt_id": parent_attempt_id, "logical_child_key": logical_child_key}
    return "child-v1-" + hashlib.sha256(_strict_json(preimage)).hexdigest()


@dataclass(frozen=True)
class AdmittedChildSnapshot:
    child_key: str
    admission_identity: str
    task_json: str
    inputs_json: str
    descriptors_json: str
    parent_context_json: str
    discovery_provenance_json: str | None = None


@dataclass(frozen=True)
class AdmittedChildrenSnapshot:
    children: tuple[AdmittedChildSnapshot, ...]
    closed: bool
    revoked: bool


def resource(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    raise ChildBridgeError("invalid Runtime resource")


def task_resource(value: Any) -> dict[str, Any]:
    result = resource(value)
    return resource(result.get("task", result))


def _snapshot_grant(task: Mapping[str, Any]) -> dict[str, Any] | None:
    """Validate only the Runtime-frozen grant retained by the trusted host."""
    spec = task.get("spec", {})
    grant = spec.get("delegated_recoverable_outputs") if isinstance(spec, Mapping) else None
    if grant is None:
        return None
    lineage = spec.get("delegated_parent")
    if (not isinstance(grant, Mapping) or set(grant) != {
            "capability_id", "capability_digest", "output_ports", "limits", "parent_attempt_id", "policy_digest"}
            or not isinstance(lineage, Mapping)
            or grant.get("capability_id") != task.get("capability_id", task.get("capability"))
            or grant.get("capability_digest") != task.get("capability_digest", spec.get("capability_digest"))
            or grant.get("parent_attempt_id") != lineage.get("parent_attempt_id")
            or not grant.get("parent_attempt_id") or not grant.get("policy_digest")
            or grant.get("policy_digest") != lineage.get("policy_digest")
            or type(grant.get("output_ports")) is not list or not grant["output_ports"]
            or any(type(port) is not str or not port or len(port) > 255
                   or any(ord(c) < 32 for c in port) for port in grant["output_ports"])
            or len(set(grant["output_ports"])) != len(grant["output_ports"])
            or not isinstance(grant.get("limits"), Mapping)
            or set(grant["limits"]) != {"max_snapshot_bytes", "max_recoverable_bytes", "max_recoverable_snapshots"}
            or any(type(value) is not int or value <= 0 for value in grant["limits"].values())):
        raise ChildBridgeError("invalid Runtime-frozen snapshot grant")
    return dict(grant)


def _verify_snapshot_row(row: Mapping[str, Any], *, task: Mapping[str, Any],
                         attempt_id: str, project_id: str, grant: Mapping[str, Any],
                         revision: int, output_port: str) -> None:
    provenance, lifecycle = row.get("provenance"), row.get("lifecycle")
    oid, size = row.get("object_id"), row.get("size")
    if (not isinstance(row.get("association_id"), str) or not row["association_id"]
            or row.get("task_id") != task.get("task_id", task.get("id"))
            or row.get("run_id") != task.get("run_id") or row.get("attempt_id") != attempt_id
            or row.get("project_id") != project_id or row.get("output_port") != output_port
            or output_port not in grant["output_ports"] or row.get("role") != "recoverable_snapshot"
            or row.get("durability") != "durable" or not isinstance(lifecycle, Mapping)
            or lifecycle.get("state") not in {"available", "promoted"}
            or row.get("state") != lifecycle.get("state")
            or not isinstance(provenance, Mapping) or type(revision) is not int
            or not 0 < revision <= 9007199254740991
            or type(provenance.get("revision")) is not int or provenance["revision"] != revision
            or provenance.get("parent_attempt_id") != grant["parent_attempt_id"]
            or provenance.get("task_id") != row.get("task_id")
            or provenance.get("attempt_id") != attempt_id
            or provenance.get("capability_id") != grant["capability_id"]
            or row.get("group_key") != "recoverable-" + attempt_id
            or row.get("variant_key") != str(revision)
            or not isinstance(oid, str) or not _DIGEST.fullmatch(oid) or row.get("digest") != oid
            or type(size) is not int or not 0 <= size <= min(_MAX_OBJECT, grant["limits"]["max_snapshot_bytes"])):
        raise ChildBridgeError("snapshot custody failed verification")


def _filename(value: Any) -> str:
    if (not isinstance(value, str) or not value or "\\" in value or "\0" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or PurePosixPath(value).is_absolute() or ".." in value.split("/")
            or value != str(PurePosixPath(value)) or value == "."):
        raise ChildBridgeError("invalid attempt filename")
    return value


def _producer_registration_filename(source_filename: str) -> str:
    """Project a confined local source path into a flat Runtime transport name."""
    source_filename = _filename(source_filename)
    path = PurePosixPath(source_filename)
    filename = (source_filename if len(path.parts) == 1 else
                "producer-" + hashlib.sha256(source_filename.encode("utf-8")).hexdigest()
                + "".join(path.suffixes))
    if len(filename) > 512 or PurePosixPath(_filename(filename)).name != filename:
        raise ChildBridgeError("invalid producer registration filename")
    return filename


def _snapshot(root: Path, filename: str, maximum: int, *, attempt_fd: int | None = None) -> bytes:
    """Walk via directory handles: no symlink component or reopen for upload."""
    parts = PurePosixPath(_filename(filename)).parts
    directory = os.open(root.name if attempt_fd is not None else root,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=attempt_fd)
    try:
        for part in parts[:-1]:
            next_directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                     dir_fd=directory)
            os.close(directory)
            directory = next_directory
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_size > maximum or before.st_uid != os.geteuid()):
                raise ChildBridgeError("producer input must be a bounded owned regular file")
            chunks = []
            total = 0
            while True:
                chunk = os.read(fd, min(1048576, maximum - total + 1))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum:
                    raise ChildBridgeError("producer input exceeds byte bound")
            after = os.fstat(fd)
            current = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if identity(before) != identity(after) or identity(after) != identity(current):
                raise ChildBridgeError("producer input changed during snapshot")
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    result = {"task_id": task.get("task_id", task.get("id")),
              "run_id": task.get("run_id"), "attempt_id": task.get("attempt_id")}
    if not all(isinstance(result[key], str) and result[key] for key in ("task_id", "run_id")):
        raise ChildBridgeError("incomplete child task identity")
    state = task.get("state", task.get("status"))
    if state is not None:
        result["state"] = state
    if isinstance(task.get("waiting_reason"), str):
        result["waiting_reason"] = task["waiting_reason"]
    settled = task.get("result")
    if isinstance(settled, Mapping):
        payload = settled.get("payload", settled)
        # Only the action ABI's result crosses the channel. Private Runtime
        # lineage, admission, lease and settlement diagnostics stay in host.
        result["result"] = {"payload": {"action_result": payload["action_result"]}
                            if isinstance(payload, Mapping) and "action_result" in payload else {},
                            "outputs": []}
    return result


class HostChildBridge:
    def __init__(self, host: Any, task: Mapping[str, Any], *, attempt_id: str,
                 lease_id: str, fence: int, runtime_epoch: int, output_root: Path,
                 cancelled: Any):
        self.host = host
        self.client = host.client
        self.task = dict(task)
        self.context = {"task_id": task.get("id", task.get("task_id")),
                        "run_id": task.get("run_id"), "project_id": task.get("project_id"),
                        "attempt_id": attempt_id, "lease_id": lease_id,
                        "fence": fence, "runtime_epoch": runtime_epoch}
        spec = task.get("spec", {})
        self.policy = spec.get("child_delegation") if isinstance(spec, Mapping) else None
        self.snapshot_grant = _snapshot_grant(self.task)
        if self.policy is not None and (not isinstance(self.policy, Mapping)
                                       or not isinstance(self.policy.get("limits"), Mapping)):
            raise ChildBridgeError("missing persisted finite child policy")
        if self.policy is None and self.snapshot_grant is None:
            raise ChildBridgeError("missing persisted finite child policy")
        if type(runtime_epoch) is not int or runtime_epoch < 1:
            raise ChildBridgeError("missing claimed epoch")
        self.output_root = output_root
        self._attempt_fd = os.open(output_root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._attempt_identity = self._directory_identity(os.fstat(self._attempt_fd))
            # GenericPackHost constructs this bridge before its later output
            # setup. Create the directory through the retained attempt handle.
            try:
                os.mkdir(output_root.name, mode=0o700, dir_fd=self._attempt_fd)
            except FileExistsError:
                pass
            root_fd = os.open(output_root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=self._attempt_fd)
            try:
                self._output_identity = self._directory_identity(os.fstat(root_fd))
            finally:
                os.close(root_fd)
        except BaseException:
            os.close(self._attempt_fd)
            raise
        self.cancelled = cancelled
        from ._child_pipe import pipe_pair
        self.channel, self.child_channel = pipe_pair()
        self.channel.settimeout(0.1)
        self._revoked = threading.Event()
        self._fd_lock = threading.Lock()
        self._channel_closed = False
        self._admission_lock = threading.Lock()
        self._admitted_children: dict[str, AdmittedChildSnapshot] = {}
        self._children: dict[str, dict[str, Any]] = {}
        self._snapshots: dict[tuple[str, int], dict[str, Any]] = {}
        self._verified_outputs: dict[tuple[str, str], dict[str, Any]] = {}
        self._materializations: dict[str, dict[str, Any]] = {}
        self._discovery_preparations: dict[str, dict[str, Any]] = {}
        self._materialization_lock = threading.Lock()
        self._materialization_blocked = False
        self._sequence = 0
        # Host-installed capability-specific custody adapter; never socket input.
        self._iteration_video_submit = None
        self._thread = threading.Thread(target=self._serve, name="astrid-child-bridge", daemon=True)
        self._launched = False
        self._launch_binding = None

    def launch_binding(self):
        from ._child_pipe import _ChildBridgeLaunchBinding
        if self._launch_binding is not None:
            raise ChildBridgeError("child pipe launch binding cannot be reused")
        self._launch_binding = _ChildBridgeLaunchBinding(self)
        return self._launch_binding

    def launched(self, process: Any) -> None:
        from ._child_pipe import VERSION, encode, pipe_identity, read_frame
        from .process_group import process_birth_identity
        try:
            self._live()
            custody = getattr(process, "_astrid_custody", None)
            birth = getattr(process, "_astrid_process_birth", None)
            if (self._launch_binding is None or self._launch_binding.custody is not custody or self._launched or self._channel_closed
                    or custody is None or custody.process is not process
                    or not birth or process_birth_identity(process.pid) != birth
                    or custody._handles.get(process.pid, (None,))[0] != birth
                    or process.poll() is not None):
                raise ChildBridgeError("child pipe lacks original live launch custody")
            self._launch_binding.verify()
            self._launch_binding.assert_running()
            self._launched = True
            authority = getattr(self.host, "_attempt_claim_authority", None)
            executor = self.host.executor_id
            if authority is not None:
                fields = dict(authority.fields)
                if (any(fields.get(k) != v for k, v in self.context.items() if k != "run_id")
                        or fields.get("executor_id") != executor):
                    raise ChildBridgeError("child pipe claim differs from authenticated host attempt")
            binding = {"protocol": VERSION, "context": {k: v for k, v in self.context.items() if k != "lease_id"}, "executor_id": executor,
                       "pid": process.pid, "birth": birth, "parent_pid": os.getpid(),
                       "launch_digest": hashlib.sha256(encode(self._launch_binding.wire)).hexdigest(),
                       "pipes": [pipe_identity(self.child_channel.read_fd, os.O_RDONLY),
                                 pipe_identity(self.child_channel.write_fd, os.O_WRONLY)]}
            self.child_channel.close()
            digest = hashlib.sha256(encode(binding)).hexdigest()
            self.channel.settimeout(5.0)
            self.channel.sendall(encode({"binding": binding, "digest": digest}))
            if read_frame(self.channel) != {"ready": digest}:
                raise ChildBridgeError("child pipe bootstrap acknowledgement differs")
            self._live()
            self.channel.sendall(encode({"accepted": digest}))
            self.channel.settimeout(0.1)
            self._thread.start()
        except BaseException:
            self.revoke()
            raise

    def close_channel(self) -> None:
        with self._admission_lock:
            self._channel_closed = True
        for channel in (self.channel, self.child_channel):
            try:
                channel.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            channel.close()
        if threading.current_thread() is not self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)

    def revoke(self) -> None:
        with self._admission_lock:
            self._revoked.set()
        self.close_channel()
        # Revocation can race the monitor and caller finalizer.
        with self._fd_lock:
            fd, self._attempt_fd = self._attempt_fd, None
            if fd is not None:
                os.close(fd)

    def admitted_children_snapshot(self) -> AdmittedChildrenSnapshot:
        """Copy host-owned admission evidence without Runtime calls or callbacks.

        JSON strings keep nested inputs, descriptors and lineage immutable too.
        Incomplete admissions are never published here.
        """
        with self._admission_lock:
            return AdmittedChildrenSnapshot(tuple(self._admitted_children.values()),
                                            self._channel_closed, self._revoked.is_set())

    def _live(self) -> None:
        if self._revoked.is_set() or self.cancelled():
            self.revoke()
            raise ChildBridgeError("parent authority ended")
        task = task_resource(self.client.task(self.context["task_id"]))
        expires = task.get("lease_expires_at")
        if isinstance(expires, str):
            try:
                if datetime.fromisoformat(expires.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                    self.revoke()
                    raise ChildBridgeError("parent lease ended")
            except ValueError as exc:
                self.revoke()
                raise ChildBridgeError("invalid parent lease deadline") from exc
        if (task.get("attempt_id") != self.context["attempt_id"]
                or task.get("state", task.get("status")) != "running"
                or task.get("runtime_epoch", self.context["runtime_epoch"]) != self.context["runtime_epoch"]):
            self.revoke()
            raise ChildBridgeError("parent authority ended")

    def _submit(self, request: dict[str, Any], *, prepared: Mapping[str, Any] | None = None,
                authority: Any = None) -> dict[str, Any]:
        def live() -> None:
            if authority is None:
                self._live()
            else:
                if self._revoked.is_set() or self.cancelled():
                    raise ChildBridgeError("prepared submission parent authority ended")
                authority()

        if prepared is not None:
            if not callable(authority) or not prepared:
                raise ChildBridgeError("prepared submission requires private live authority")
            live()
        if self.policy is None:
            raise ChildBridgeError("child submission requires persisted child policy")
        child, inputs, bindings = request["child"], request["inputs"], request["input_descriptors"]
        if (type(child) is not dict or set(child) != {"capability_id", "capability_digest"}
                or type(inputs) is not dict or type(bindings) is not list):
            raise ChildBridgeError("invalid child request")
        record = self.host.capabilities.get(child["capability_id"])
        if record is None or record.capability_digest != child["capability_digest"]:
            raise ChildBridgeError("child capability digest disagrees with admitted source")
        if not record.definition.metadata.get("action_invocation"):
            raise ChildBridgeError("child bridge requires a declared action")
        _, admission = self.host.admit("executor", record.id)
        _verify_admitted_source(admission)
        if child not in self.policy.get("capabilities", []):
            raise ChildBridgeError("child capability outside policy")
        _validate_required_inputs(record.id, record.definition.inputs, inputs,
                                  noun="executor", error_cls=ChildBridgeError)
        from astrid.sdk.actions import validate_action_inputs_definition
        validate_action_inputs_definition(record.definition, inputs)
        file_ports = {port.name: port for port in record.definition.inputs if port.type == "file"}
        names = [name for name in file_ports if name in inputs]
        if [b.get("name") for b in bindings if isinstance(b, dict)] != names or len(bindings) != len(names):
            raise ChildBridgeError("file descriptors disagree with declared port order")
        if len(bindings) > self.policy["limits"]["max_child_inputs"]:
            raise ChildBridgeError("child input count exceeds persisted limit")
        key = request["child_key"]
        wire_id = _child_wire_id(project_id=self.context["project_id"],
                                 parent_task_id=self.context["task_id"],
                                 parent_attempt_id=self.context["attempt_id"], logical_child_key=key)
        bound = self._children.get(key)
        if bound is None and len(self._children) >= self.policy["limits"]["max_children"]:
            raise ChildBridgeError("child bridge count exceeds persisted limit")
        refs, ordered = [], []
        uploads = []
        snapshot_bytes = 0
        child_bytes = 0
        for binding in bindings:
            name = binding["name"]
            if binding.get("kind") == "producer_file":
                if set(binding) != {"name", "kind", "filename", "output_port", "media_type"}:
                    raise ChildBridgeError("invalid producer file binding")
                if inputs[name] != {k: binding[k] for k in ("filename", "output_port", "media_type")}:
                    raise ChildBridgeError("producer binding disagrees with input")
                if not all(isinstance(binding[k], str) and binding[k] for k in ("output_port", "media_type")):
                    raise ChildBridgeError("invalid producer metadata")
                maximum = min(_MAX_OBJECT, self.policy["limits"]["max_child_bytes"],
                              self.policy["limits"]["max_derived_bytes"])
                data = _snapshot(self.output_root, binding["filename"], maximum, attempt_fd=self._attempt_fd)
                if prepared is not None and name in prepared:
                    self._verify_prepared_snapshot(prepared[name], binding, data)
                snapshot_bytes += len(data)
                child_bytes += len(data)
                if snapshot_bytes > min(self.policy["limits"]["max_child_bytes"], self.policy["limits"]["max_derived_bytes"]):
                    raise ChildBridgeError("producer snapshots exceed persisted byte limit")
                ref = {k: binding[k] for k in ("name", "filename", "output_port", "media_type")}
                ref["filename"] = _producer_registration_filename(binding["filename"])
                ref.update(object_id="sha256:" + hashlib.sha256(data).hexdigest(), size=len(data))
                refs.append(ref)
                uploads.append((data, ref))
                # Requiredness is verified host metadata, not an upload or
                # authority field. Keep this private copy separate from refs.
                ordered.append({**ref, "required": file_ports[name].required})
            elif binding.get("kind") == "object":
                if set(binding) != {"name", "kind", "object_id"}:
                    raise ChildBridgeError("invalid object binding")
                oid = binding["object_id"]
                if not isinstance(oid, str) or not _DIGEST.fullmatch(oid) or oid not in self.policy["input_object_ids"]:
                    raise ChildBridgeError("object outside admitted parent custody")
                value = inputs[name]
                if value != oid and (not isinstance(value, dict) or set(value) - {"object_id", "digest", "filename"}
                        or value.get("object_id", value.get("digest")) != oid
                        or value.get("digest", oid) != oid):
                    raise ChildBridgeError("object binding disagrees with input")
                data = self.client.get_object(oid.removeprefix("sha256:"))
                if len(data) > _MAX_OBJECT or "sha256:" + hashlib.sha256(data).hexdigest() != oid:
                    raise ChildBridgeError("object bytes failed verification")
                child_bytes += len(data)
                filename = _filename(value.get("filename", name) if isinstance(value, dict) else name)
                ordered.append({"name": name, "object_id": oid, "filename": filename,
                                "required": file_ports[name].required})
            else:
                raise ChildBridgeError("unsupported input binding")
        if prepared is not None:
            if (set(prepared) - {ref["name"] for ref in refs}
                    or len(refs) > self.policy["limits"]["max_derived_objects"]
                    or child_bytes > self.policy["limits"]["max_child_bytes"]):
                raise ChildBridgeError("prepared child or derived input budget exceeded")
            grant = self.policy.get("discovery_grant")
            if (not isinstance(grant, Mapping) or len(prepared) > grant["limits"]["max_child_media_bindings"]
                    or sum(value["size"] for value in prepared.values()) > grant["limits"]["max_child_media_bytes"]):
                raise ChildBridgeError("prepared child media budget exceeded")
        if len({ref["filename"] for ref in ordered}) != len(ordered):
            raise ChildBridgeError("child input registration filenames collide")
        identity_values = {"child": child, "inputs": inputs, "ordered": ordered}
        if prepared is not None:
            identity_values["discovery_provenance"] = dict(prepared)
        identity = hashlib.sha256(_strict_json(identity_values)).hexdigest()
        if bound is not None and bound["identity"] != identity:
            raise ChildBridgeError("child key was already bound to different inputs or bytes")
        # Reserve even an interrupted upload/admission. Reuse cannot change
        # the identity after a partially successful Runtime mutation.
        if bound is None:
            bound = {"identity": identity, "task": None, "deadline": None}
            self._children[key] = bound
        if bound["task"] is not None:
            if prepared is not None:
                live()
            return _public_task(bound["task"])
        for data, ref in uploads:
            live()
            if prepared is not None and ref["name"] in prepared:
                source = next(binding for binding in bindings if binding["name"] == ref["name"])
                self._verify_prepared_snapshot(prepared[ref["name"]], source, data)
                if ref["object_id"] != prepared[ref["name"]]["object_id"] or ref["size"] != prepared[ref["name"]]["size"]:
                    raise ChildBridgeError("prepared upload snapshot identity changed")
            self.client.upload_child_input(data, descriptor=ref, **self.context)
        live()
        admitted = self.client.admit_child(
            child={"child_id": wire_id, **child}, inputs=inputs, ordered_inputs=ordered,
            derived_inputs=refs, **self.context)
        task = task_resource(admitted)
        self._assert_lineage(task)
        if (task.get("capability_id", task.get("capability")) != child["capability_id"]
                or task.get("capability_digest") != child["capability_digest"]
                or task.get("idempotency_key") != wire_id):
            raise ChildBridgeError("admitted child identity disagrees with request")
        bound["task"] = task
        bound["deadline"] = time.monotonic() + request["timeout_seconds"]
        live()
        snapshot = AdmittedChildSnapshot(key, identity, _strict_json(task).decode(),
                                        _strict_json(inputs).decode(), _strict_json(ordered).decode(),
                                        _strict_json(self.context).decode(),
                                        _strict_json(dict(prepared)).decode() if prepared is not None else None)
        with self._admission_lock:
            if self._channel_closed or self._revoked.is_set():
                raise ChildBridgeError("child bridge closed during admission")
            self._admitted_children[key] = snapshot
        return _public_task(task)

    def _publish_snapshot(self, request: dict[str, Any]) -> dict[str, Any]:
        grant = self.snapshot_grant
        port, revision = request["output_port"], request["revision"]
        if (grant is None or type(port) is not str or port not in grant["output_ports"]
                or type(revision) is not int or not 0 < revision <= 9007199254740991):
            raise ChildBridgeError("snapshot outside admitted grant")
        maximum = min(_MAX_OBJECT, grant["limits"]["max_snapshot_bytes"])
        data = _snapshot(self.output_root, request["filename"], maximum, attempt_fd=self._attempt_fd)
        descriptor = {"name": port, "output_port": port, "filename": request["filename"],
                      "object_id": "sha256:" + hashlib.sha256(data).hexdigest(),
                      "size": len(data), "media_type": "application/json"}
        key = (port, revision)
        bound = self._snapshots.get(key)
        if bound is not None and bound["descriptor"] != descriptor:
            raise ChildBridgeError("snapshot revision was bound to different bytes or filename")
        if bound is None:
            # Retain bounded immutable bytes across uncertain upload/publication.
            if (len(self._snapshots) >= grant["limits"]["max_recoverable_snapshots"]
                    or sum(len(value["data"]) for value in self._snapshots.values()) + len(data)
                    > grant["limits"]["max_recoverable_bytes"]):
                raise ChildBridgeError("snapshot host retention exceeds admitted bounds")
            bound = {"descriptor": descriptor, "data": data,
                     "key": "snapshot-" + hashlib.sha256(_strict_json({
                         "attempt_id": self.context["attempt_id"], "revision": revision,
                         "output": descriptor})).hexdigest()}
            self._snapshots[key] = bound
        self._live()
        self.client.upload_child_input(bound["data"], descriptor=bound["descriptor"], **self.context)
        self._live()
        result = self.client.generated.publish_recoverable_snapshot(
            self.context["attempt_id"], **{k: self.context[k] for k in ("lease_id", "fence", "runtime_epoch")},
            revision=revision, output=bound["descriptor"], idempotency_key=bound["key"])
        row = resource(result)
        _verify_snapshot_row(row, task=self.task, attempt_id=self.context["attempt_id"],
                             project_id=self.context["project_id"], grant=grant, revision=revision, output_port=port)
        receipt = row.get("receipt")
        if (row["object_id"] != descriptor["object_id"] or row["size"] != len(data)
                or row.get("filename") != descriptor["filename"] or row.get("media_type") != "application/json"
                or not isinstance(receipt, Mapping) or type(receipt.get("receipt_id")) is not str
                or not receipt["receipt_id"] or receipt.get("command_kind") != "attempt.recoverable_snapshot.publish"
                or receipt.get("idempotency_key") != bound["key"]
                or receipt.get("project_id") != self.context["project_id"]
                or not isinstance(receipt.get("result"), Mapping)
                or any(receipt["result"].get(k) != v for k, v in row.items() if k != "receipt")):
            raise ChildBridgeError("invalid durable snapshot publication receipt")
        return {"association_id": row["association_id"], "receipt_id": receipt["receipt_id"],
                "revision": revision, "output_port": port, "digest": row["digest"],
                "size": row["size"], "durability": "durable"}

    def _assert_lineage(self, task: dict[str, Any]) -> None:
        spec = task.get("spec", {})
        lineage = spec.get("delegated_parent") if isinstance(spec, Mapping) else None
        expected = {"parent_task_id": self.context["task_id"], "parent_attempt_id": self.context["attempt_id"],
                    "parent_lease_id": self.context["lease_id"], "parent_fence": self.context["fence"],
                    "runtime_epoch": self.context["runtime_epoch"], "project_id": self.context["project_id"],
                    "executor_id": self.host.executor_id}
        if not isinstance(lineage, Mapping) or any(lineage.get(k) != v for k, v in expected.items()):
            raise ChildBridgeError("foreign child lineage")

    def _child_task(self, key: str, task_id: str) -> dict[str, Any]:
        bound = self._children.get(key)
        if bound is None or bound["task"] is None or _public_task(bound["task"])["task_id"] != task_id:
            raise ChildBridgeError("foreign child key or task")
        task = task_resource(self.client.task(task_id))
        if _public_task(task)["run_id"] != _public_task(bound["task"])["run_id"]:
            raise ChildBridgeError("foreign child run")
        self._assert_lineage(task)
        if any(task.get(k) != bound["task"].get(k) for k in ("capability_id", "capability_digest", "idempotency_key")):
            raise ChildBridgeError("child identity changed after admission")
        return task

    def _outputs(self, task: dict[str, Any], *, capture: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if task.get("state", task.get("status")) not in {"completed", "succeeded"}:
            raise ChildBridgeError("child is not successfully settled")
        public = _public_task(task)
        rows = self.client.child_outputs(public["task_id"])
        if type(rows) is not list or len(rows) > 256:
            raise ChildBridgeError("child output page exceeds bound")
        settled = task.get("result", {})
        expected_outputs = settled.get("outputs") if isinstance(settled, Mapping) else None
        if type(expected_outputs) is not list:
            raise ChildBridgeError("child output associations disagree with settlement")
        remaining = list(expected_outputs)
        outputs = []
        seen = set()
        for raw in rows:
            row = resource(raw)
            association = row.get("association_id")
            if not isinstance(association, str) or not association or association in seen:
                raise ChildBridgeError("invalid child output association")
            seen.add(association)
            verified = resource(self.client.child_output(association))
            if (row != verified or row.get("task_id") != public["task_id"]
                    or row.get("run_id") != public["run_id"]
                    or row.get("project_id") != self.context["project_id"]
                    or row.get("durability") != "durable"
                    or row.get("lifecycle", {}).get("state") not in {"available", "promoted"}):
                raise ChildBridgeError("child output custody failed verification")
            if row.get("role") != "recoverable_snapshot" and row.get("attempt_id") != public["attempt_id"]:
                raise ChildBridgeError("child final output attempt failed verification")
            oid = row.get("object_id")
            size = row.get("size")
            if (not isinstance(oid, str) or not _DIGEST.fullmatch(oid) or row.get("digest") != oid
                    or type(size) is not int or not 0 <= size <= _MAX_OBJECT):
                raise ChildBridgeError("invalid child output byte identity")
            if row.get("role") == "recoverable_snapshot":
                grant = _snapshot_grant(task)
                if grant is None:
                    raise ChildBridgeError("snapshot has no Runtime-frozen grant")
                provenance = row.get("provenance")
                revision = provenance.get("revision") if isinstance(provenance, Mapping) else None
                historical_attempt = row.get("attempt_id")
                if type(historical_attempt) is not str or not historical_attempt:
                    raise ChildBridgeError("snapshot has no historical attempt identity")
                # Retries retain Runtime-owned snapshot associations. Verify
                # their historical custody before excluding them; ordinary
                # final outputs remain exact to the current settled attempt.
                _verify_snapshot_row(row, task=task, attempt_id=historical_attempt,
                                     project_id=self.context["project_id"], grant=grant,
                                     revision=revision, output_port=row.get("output_port"))
                data = self.client.get_object(oid[7:])
                if len(data) != size or "sha256:" + hashlib.sha256(data).hexdigest() != oid:
                    raise ChildBridgeError("snapshot bytes failed verification")
                if resource(self.client.child_output(association)) != row:
                    raise ChildBridgeError("snapshot association changed during readback")
                continue
            matches = [expected for expected in remaining if isinstance(expected, Mapping)
                       and expected.get("kind") == "object" and expected.get("digest") == oid
                       and expected.get("size") == size and expected.get("media_type") == row.get("media_type")
                       and expected.get("output_port", expected.get("name", "output")) == row.get("output_port")
                       and all(k not in expected or expected[k] == row.get(k)
                               for k in ("filename", "ordinal", "group_key", "variant_key"))]
            if not matches:
                raise ChildBridgeError("child output descriptor disagrees with settlement")
            remaining.remove(matches[0])
            data = self.client.get_object(oid[7:])
            if len(data) != size or "sha256:" + hashlib.sha256(data).hexdigest() != oid:
                raise ChildBridgeError("child output bytes failed verification")
            if resource(self.client.child_output(association)) != row:
                raise ChildBridgeError("child output association changed during readback")
            # Return managed CAS descriptors, never private lineage or lease.
            descriptor = _validate_output({k: row[k] for k in (
                "association_id", "run_id", "task_id", "attempt_id", "object_id", "digest",
                "size", "filename", "media_type", "output_port", "ordinal")},
                task_id=public["task_id"], run_id=public["run_id"], attempt_id=public["attempt_id"])
            binding = (public["task_id"], association)
            original = self._verified_outputs.get(binding)
            if original is not None and original != descriptor:
                raise ChildBridgeError("child output descriptor changed after verified readback")
            outputs.append(descriptor)
            if capture is not None and capture["association_id"] == association:
                capture["data"] = data
        if remaining:
            raise ChildBridgeError("child output associations disagree with settlement")
        self._live()
        for descriptor in outputs:
            self._verified_outputs.setdefault((public["task_id"], descriptor["association_id"]), dict(descriptor))
        return outputs

    @staticmethod
    def _directory_identity(info: Any) -> tuple[int, int]:
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ChildBridgeError("materialization requires owned attempt directories")
        return info.st_dev, info.st_ino

    @staticmethod
    def _file_identity(info: Any) -> tuple[int, int, int, int, int]:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise ChildBridgeError("materialization requires an owned regular file")
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def _materialization_directories(self, filename: str) -> list[tuple[int, str, int]]:
        """Retain every parent handle so renames/replacements can be detected."""
        with self._fd_lock:
            if self._attempt_fd is None:
                raise ChildBridgeError("parent authority ended")
            anchor = os.dup(self._attempt_fd)
        directories = [(anchor, "", anchor)]
        try:
            if self._directory_identity(os.stat(self.output_root.parent, follow_symlinks=False)) != self._attempt_identity:
                raise ChildBridgeError("parent attempt directory changed")
            for name in (self.output_root.name, *PurePosixPath(_filename(filename)).parts[:-1]):
                parent = directories[-1][2]
                if name != self.output_root.name or len(directories) != 1:
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                directories.append((parent, name, fd))
                identity = self._directory_identity(os.fstat(fd))
                if len(directories) == 2 and identity != self._output_identity:
                    raise ChildBridgeError("parent output directory changed")
            self._verify_materialization_directories(directories)
            return directories
        except BaseException:
            for _, _, fd in reversed(directories):
                os.close(fd)
            raise

    def _verify_materialization_directories(self, directories: list[tuple[int, str, int]]) -> None:
        if self._directory_identity(os.stat(self.output_root.parent, follow_symlinks=False)) != self._attempt_identity:
            raise ChildBridgeError("parent attempt directory changed")
        for parent, name, fd in directories[1:]:
            if self._directory_identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != self._directory_identity(os.fstat(fd)):
                raise ChildBridgeError("materialization directory changed")

    def _verify_materialized_file(self, directory: int, filename: str,
                                  entry: dict[str, Any]) -> tuple[int, int, int, int, int]:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = self._file_identity(os.fstat(fd))
            if entry.get("file_identity") is not None and before != entry["file_identity"]:
                raise ChildBridgeError("materialized file replaced or changed")
            descriptor = entry["output"]
            if before[2] != descriptor["size"]:
                raise ChildBridgeError("materialized file size changed")
            total, sha = 0, hashlib.sha256()
            while True:
                chunk = os.read(fd, min(1048576, descriptor["size"] - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > descriptor["size"]:
                    raise ChildBridgeError("materialized file exceeds descriptor size")
                sha.update(chunk)
            after = self._file_identity(os.fstat(fd))
            current = self._file_identity(os.stat(filename, dir_fd=directory, follow_symlinks=False))
            if (before != after or after != current or total != descriptor["size"]
                    or "sha256:" + sha.hexdigest() != descriptor["digest"]):
                raise ChildBridgeError("materialized file failed verification")
            return after
        finally:
            os.close(fd)

    def _write_materialized_file(self, directories: list[tuple[int, str, int]],
                                 entry: dict[str, Any], data: bytes, *, authority: Any = None) -> None:
        check_authority = authority if authority is not None else self._live
        directory, filename = directories[-1][2], PurePosixPath(entry["filename"]).name
        temporary = ".materialize-" + uuid.uuid4().hex
        fd = None
        published = False
        inode = None
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            info = os.fstat(fd)
            inode = (info.st_dev, info.st_ino)
            view = memoryview(data)
            while view:
                written = os.write(fd, view[:1048576])
                if written <= 0:
                    raise ChildBridgeError("materialization write made no progress")
                view = view[written:]
            os.fsync(fd)
            check_authority()
            self._verify_materialization_directories(directories)
            # Atomic create without overwriting any caller/replacement file.
            os.link(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory,
                    follow_symlinks=False)
            published = True
            os.unlink(temporary, dir_fd=directory)
            os.fsync(directory)
            check_authority()
            self._verify_materialization_directories(directories)
            identity = self._verify_materialized_file(directory, filename, entry)
            if identity[:2] != inode:
                raise ChildBridgeError("materialized file replaced during publication")
            entry["file_identity"] = identity
            with self._materialization_lock:
                entry["state"] = "retained"
        except BaseException:
            clean = True
            for name in (temporary, filename) if published else (temporary,):
                try:
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) != inode:
                        clean = False
                        continue
                    os.unlink(name, dir_fd=directory)
                except FileNotFoundError:
                    pass
                except OSError:
                    clean = False
            if not clean:
                with self._materialization_lock:
                    self._materialization_blocked = True
                entry["cleanup_uncertain"] = True
            raise
        finally:
            if fd is not None:
                os.close(fd)

    def _verify_prepared_snapshot(self, prepared: Mapping[str, Any], binding: Mapping[str, Any],
                                   data: bytes) -> None:
        """Reauthenticate retained inode and exact bytes within submission."""
        oid = prepared.get("object_id")
        entry = self._discovery_preparations.get(oid)
        if (entry is None or entry["state"] != "retained" or self._materialization_blocked
                or any(prepared.get(key) != value for key, value in entry["descriptor"].items())
                or prepared.get("parent_task_id") != self.context["task_id"]
                or prepared.get("parent_attempt_id") != self.context["attempt_id"]
                or binding.get("filename") != prepared.get("filename")
                or binding.get("media_type") != prepared.get("media_type")
                or type(data) is not bytes or len(data) != prepared.get("size")
                or "sha256:" + hashlib.sha256(data).hexdigest() != oid):
            raise ChildBridgeError("prepared immutable snapshot identity changed")
        directories = self._materialization_directories(entry["filename"])
        try:
            self._verify_materialized_file(directories[-1][2], "object", entry)
            self._verify_materialization_directories(directories)
        finally:
            for _, _, fd in reversed(directories):
                os.close(fd)

    def _stage_discovery_object(self, data: bytes, *, object_id: str, size: int,
                                media_type: str, expected_context: Mapping[str, Any],
                                authority: Any) -> dict[str, Any]:
        """Private current-attempt staging; never a socket operation or receipt.

        Reuse the existing owned-directory writer and verify every replay. A
        source/caller filename never determines this host-generated destination.
        """
        if (self.context != expected_context or not callable(authority)
                or type(object_id) is not str or not _DIGEST.fullmatch(object_id)
                or type(size) is not int or not 0 <= size <= _MAX_OBJECT
                or type(data) is not bytes or len(data) != size
                or "sha256:" + hashlib.sha256(data).hexdigest() != object_id
                or type(media_type) is not str or not media_type):
            raise ChildBridgeError("invalid discovery staging identity")
        def live() -> None:
            if self._revoked.is_set() or self.cancelled():
                raise ChildBridgeError("discovery staging parent authority ended")
            authority()

        live()
        descriptor = {"object_id": object_id, "digest": object_id, "size": size,
                      "media_type": media_type,
                      "filename": "discovery-objects/" + object_id[7:] + "/object",
                      "parent_task_id": self.context["task_id"],
                      "parent_attempt_id": self.context["attempt_id"]}
        with self._materialization_lock:
            if self._materialization_blocked:
                raise ChildBridgeError("materialization cleanup is uncertain")
            entry = self._discovery_preparations.get(object_id)
            repeated = entry is not None
            if repeated:
                if entry["descriptor"] != descriptor or entry["state"] != "retained":
                    raise ChildBridgeError("discovery staging identity is pending or changed")
            else:
                entry = {"descriptor": descriptor, "output": {"digest": object_id, "size": size},
                         "filename": descriptor["filename"], "state": "pending"}
                self._discovery_preparations[object_id] = entry
        directories = []
        try:
            directories = self._materialization_directories(entry["filename"])
            live()
            if repeated:
                self._verify_materialized_file(directories[-1][2], "object", entry)
            else:
                self._write_materialized_file(directories, entry, data, authority=live)
            live()
            self._verify_materialization_directories(directories)
            self._verify_materialized_file(directories[-1][2], "object", entry)
            return dict(descriptor)
        except BaseException:
            # Retained or uncertain entries stay reserved and replays fail
            # closed. A definitely unpublished reservation can be released.
            if not repeated and entry["state"] == "pending" and not entry.get("cleanup_uncertain"):
                with self._materialization_lock:
                    self._discovery_preparations.pop(object_id, None)
            raise
        finally:
            for _, _, fd in reversed(directories):
                os.close(fd)

    def _materialize_output(self, request: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        association = request["association_id"]
        if not isinstance(association, str) or not _KEY.fullmatch(association):
            raise ChildBridgeError("invalid child output association")
        original = self._verified_outputs.get((request["task_id"], association))
        if original is None:
            raise ChildBridgeError("association has no verified child result binding")
        if self.policy is None:
            raise ChildBridgeError("materialization requires persisted finite child policy")
        limits = self.policy["limits"]
        with self._materialization_lock:
            if self._materialization_blocked:
                raise ChildBridgeError("materialization cleanup is uncertain")
            entry = self._materializations.get(association)
            repeated = entry is not None
            if repeated:
                if entry["output"] != original or entry["state"] != "retained":
                    raise ChildBridgeError("materialization association is pending or changed")
            else:
                if (len(self._materializations) >= limits["max_derived_objects"]
                        or sum(value["output"]["size"] for value in self._materializations.values())
                        + original["size"] > limits["max_derived_bytes"]):
                    raise ChildBridgeError("materialization retention exceeds persisted limits")
                entry = {"output": dict(original), "state": "pending", "filename":
                         "child-outputs/" + association + "/" + _filename(original["filename"])}
                self._materializations[association] = entry
        directories = []
        try:
            capture = {"association_id": association}
            rows = self._outputs(task, capture=capture)
            if [row for row in rows if row["association_id"] == association] != [original]:
                raise ChildBridgeError("child output association no longer matches verified result")
            data = capture["data"]
            # _outputs verifies actual size, digest, custody and stable association.
            self._live()
            directories = self._materialization_directories(entry["filename"])
            if repeated:
                self._verify_materialized_file(directories[-1][2], PurePosixPath(entry["filename"]).name, entry)
            else:
                self._write_materialized_file(directories, entry, data)
                with self._materialization_lock:
                    entry["state"] = "retained"
            self._live()
            self._verify_materialization_directories(directories)
            return {"output": dict(original), "filename": entry["filename"]}
        except BaseException:
            # A published object remains charged even if authority ends after
            # publication. Only confirmed pre-publication cleanup releases it.
            if not repeated and entry["state"] == "pending" and not entry.get("cleanup_uncertain"):
                with self._materialization_lock:
                    self._materializations.pop(association, None)
            raise
        finally:
            for _, _, fd in reversed(directories):
                os.close(fd)

    def dispatch(self, request: dict[str, Any]) -> Any:
        self._live()
        common = {"v", "request_id", "op"}
        op = request.get("op")
        expected = common | ({"filename", "output_port", "revision"} if op == "publish_snapshot" else
                             {"child_key", "child", "inputs", "input_descriptors", "wait", "timeout_seconds", "poll_seconds"}
                             if op == "submit" else {"child_key", "task_id", "association_id"}
                             if op == "materialize_output" else {"child_key", "task_id"})
        if (set(request) != expected or request.get("v") != 1 or type(request.get("v")) is not int
                or type(request.get("request_id")) is not int or request["request_id"] <= self._sequence
                or op not in {"submit", "status", "outputs", "publish_snapshot", "materialize_output"}
                or op != "publish_snapshot" and (not isinstance(request.get("child_key"), str)
                                                  or not _KEY.fullmatch(request["child_key"]))):
            self.revoke()
            raise ChildBridgeError("invalid child bridge frame")
        self._sequence = request["request_id"]
        if op == "publish_snapshot":
            return self._publish_snapshot(request)
        if op == "submit":
            if type(request["wait"]) is not bool or any(type(request[k]) not in (int, float)
                    or not math.isfinite(request[k]) or request[k] <= 0 for k in ("timeout_seconds", "poll_seconds")):
                raise ChildBridgeError("invalid bounded wait preferences")
            handler = getattr(self, "_iteration_video_submit", None)
            if (handler is not None and isinstance(request["child"], Mapping)
                    and request["child"].get("capability_id") == "rendering.render"):
                return handler(request)
            return self._submit(request)
        task = self._child_task(request["child_key"], request["task_id"])
        if op == "outputs":
            return self._outputs(task)
        if op == "materialize_output":
            return self._materialize_output(request, task)
        public = _public_task(task)
        if public.get("state") in {"completed", "succeeded"}:
            public["result"]["outputs"] = self._outputs(task)
        return public

    def finish(self) -> None:
        """No new admissions after process exit; fence success on every child."""
        self.close_channel()
        if self._thread.is_alive():
            self.revoke()
            raise ChildBridgeError("child bridge service did not stop")
        while True:
            self._live()
            pending = False
            for key, bound in self._children.items():
                if bound["task"] is None:
                    raise ChildBridgeError("child admission did not finish")
                task = self._child_task(key, _public_task(bound["task"])["task_id"])
                state = task.get("state", task.get("status"))
                if state in {"completed", "succeeded"}:
                    self._outputs(task)
                elif state in {"failed", "cancelled", "cancel_requested"} or time.monotonic() >= bound["deadline"]:
                    if state in {"queued", "waiting"}:
                        raise ChildBridgeError("required child remained queued with no serving capacity observed within its bounded wait")
                    raise ChildBridgeError("required child did not settle within its bounded wait")
                else:
                    pending = True
            if not pending:
                return
            time.sleep(0.01)

    def _serve(self) -> None:
        buffered = bytearray()
        phase, operation = "receive", "unknown"
        sequence, frame_bytes = 0, 0
        started = time.monotonic()
        dispatch_started = None
        dispatch_elapsed = None
        diagnostic = None
        try:
            while not self._channel_closed and not self._revoked.is_set():
                self._live()
                try:
                    chunk = self.channel.recv(min(65536, _MAX_FRAME + 1 - len(buffered)))
                except socket.timeout:
                    continue
                if not chunk:
                    return
                if not buffered:
                    started = time.monotonic()
                    phase, operation = "receive", "unknown"
                    dispatch_started = dispatch_elapsed = None
                buffered.extend(chunk)
                frame_bytes = len(buffered)
                if len(buffered) > _MAX_FRAME:
                    raise ChildBridgeError("oversized child bridge frame")
                if b"\n" not in buffered:
                    continue
                line, rest = bytes(buffered).split(b"\n", 1)
                if rest:  # no pipelining
                    raise ChildBridgeError("pipelined child bridge frame")
                buffered.clear()
                sequence += 1
                phase = "decode"
                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ChildBridgeError("duplicate child bridge key")
                        result[key] = value
                    return result
                request = json.loads(line, object_pairs_hook=unique)
                if type(request) is not dict:
                    raise ChildBridgeError("invalid child bridge frame")
                _strict_json(request)
                operation = request.get("op")
                try:
                    phase = "dispatch"
                    dispatch_started = time.monotonic()
                    if self._launch_binding is not None:
                        self._launch_binding.assert_running()
                    data = self.dispatch(request)
                    dispatch_elapsed = time.monotonic() - dispatch_started
                    reply = {"v": 1, "request_id": request["request_id"], "ok": True, "data": data}
                except Exception as exc:
                    dispatch_elapsed = time.monotonic() - dispatch_started
                    diagnostic = _bridge_io_failure_frame(endpoint="host", phase=phase, operation=operation,
                                            exception=exc, request_sequence=sequence,
                                            frame_bytes=frame_bytes,
                                            elapsed_seconds=time.monotonic() - started,
                                            budget_seconds=None,
                                            dispatch_elapsed_seconds=dispatch_elapsed)
                    # Runtime exceptions can contain signed tokens or private
                    # identity. Never serialize their text/details to pack code.
                    # A deliberately opt-in local diagnostic keeps the
                    # redacted wire contract while making disposable
                    # qualification failures debuggable without blind retries.
                    if os.environ.get("ASTRID_DEBUG_CHILD_BRIDGE_ERRORS") == "1":
                        print(f"child bridge rejected {type(exc).__name__}: {str(exc)[:512]}",
                              file=sys.stderr, flush=True)
                    reply = {"v": 1, "request_id": request.get("request_id"), "ok": False,
                             "error": {"code": "authorization_error", "message": "child bridge rejected request", "details": {}}}
                self.channel.settimeout(1.0)
                phase = "encode"
                encoded = _strict_json(reply) + b"\n"
                phase = "send"
                self.channel.sendall(encoded)
                self.channel.settimeout(0.1)
                phase, operation = "receive", "unknown"
                frame_bytes = 0
                started = time.monotonic()
                dispatch_started = dispatch_elapsed = None
        except Exception as exc:
            diagnostic = _bridge_io_failure_frame(endpoint="host", phase=phase, operation=operation,
                                    exception=exc, request_sequence=sequence,
                                    frame_bytes=frame_bytes, elapsed_seconds=time.monotonic() - started,
                                    budget_seconds=1.0 if phase == "send" else None,
                                    dispatch_elapsed_seconds=dispatch_elapsed)
            if not self._channel_closed:
                with self._admission_lock:
                    self._revoked.set()
        finally:
            self.close_channel()
            _write_bridge_io_failure(diagnostic)
